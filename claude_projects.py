#!/usr/bin/env python3
"""Project registry, notes/plans and git-sync manager for the D:\\claude estate.

Reads and maintains `_claude\\projects.json` (the machine-readable registry,
the single source of truth) and re-renders `_claude\\REGISTRY.md` from it. Also
keeps per-project notes/plans, dated session logs, reports git status across
every repo node, and pushes/checkpoints them under the estate's no-attribution,
Conventional-Commits rules. Stdlib only - no pip installs needed.

The estate root defaults to env CLAUDE_ESTATE_ROOT or D:\\claude; the workspace
is `<root>\\_claude`. Logical data in projects.json (type/work/group/parent/
links/description/notes/flags/children) is hand-authored and is never destroyed
by `scan` - only discoverable git facts (remote, branch, on-disk presence) are
refreshed.

Usage:
  python claude_projects.py scan [--write]            reconcile registry with disk
  python claude_projects.py list [--tree|--work ID|--links]
  python claude_projects.py add ID --name N --type {work,code,assets} [...]
  python claude_projects.py remove ID
  python claude_projects.py link FROM TO --type T [--note N]
  python claude_projects.py unlink FROM TO [--type T]
  python claude_projects.py links ID
  python claude_projects.py note ID [TEXT] [--show]
  python claude_projects.py plan ID add PATH | plan ID list
  python claude_projects.py status [--all]
  python claude_projects.py push (ID | --all) [-m MSG]
  python claude_projects.py sync [--all]
  python claude_projects.py log [TEXT] [--show] [--resume]
  python claude_projects.py hooks (ID | --all)
  python claude_projects.py render
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime

DEFAULT_ROOT = os.environ.get("CLAUDE_ESTATE_ROOT", "D:\\claude")

# commit-msg hook that strips any AI attribution a subagent might add
HOOK_BODY = """#!/bin/sh
# Enforce the project's no-attribution rule on every commit (incl. subagent commits).
sed -i \\
  -e '/[Cc]o-[Aa]uthored-[Bb]y:.*[Cc]laude/d' \\
  -e '/noreply@anthropic\\.com/d' \\
  -e '/Generated with \\[Claude Code\\]/d' \\
  -e '/\U0001f916 Generated/d' \\
  "$1"
"""

COMMIT_AUTHOR_NAME = "MarkS0485"
COMMIT_AUTHOR_EMAIL = "MarkS0485@users.noreply.github.com"

# a Conventional Commits subject: type or type(scope), optional !, then ": "
CONVENTIONAL_RE = re.compile(r"^[a-z]+(\([^)]+\))?!?: .+")

# env vars the estate expects to exist for pushing / packing
EXPECTED_TOKENS = ("GH_TOKEN", "NUGET_API_KEY")


# ----------------------------------------------------------- path helpers ---

def workspace_dir(root, workspace=None):
    return workspace or os.path.join(root, "_claude")


def projects_path(workspace):
    return os.path.join(workspace, "projects.json")


def registry_path(workspace):
    return os.path.join(workspace, "REGISTRY.md")


def node_dir_on_disk(root, node):
    """Absolute on-disk path for a node, or None if it has no path."""
    if not node.get("path"):
        return None
    return os.path.join(root, node["path"].replace("/", os.sep))


# --------------------------------------------------------------- file I/O ---

def load_registry(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.exit(f"registry not found: {path}")
    except ValueError as e:
        sys.exit(f"{path} is not valid JSON ({e}) - fix it first.")
    if not isinstance(data, dict) or "nodes" not in data:
        sys.exit(f"{path} is not a projects registry (no 'nodes').")
    return data


def save_registry(data, path):
    """Write atomically, keeping a one-deep .bak of the previous version."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            previous = f.read()
        with open(path + ".bak", "w", encoding="utf-8") as f:
            f.write(previous)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


# ------------------------------------------------------- registry helpers ---

def node_by_id(data, node_id):
    for n in data["nodes"]:
        if n.get("id") == node_id:
            return n
    return None


def nodes_index(data):
    return {n["id"]: n for n in data["nodes"]}


# --------------------------------------------------- pure: classification ---

def classify_dir(path, is_git, child_dirs_git):
    """Classify an on-disk directory (pure function, unit-tested).

    - a dir with its own .git           => "code"
    - a dir with no .git but with one or
      more immediate child dirs that are
      git repos                          => "work"
    - otherwise                          => None (leave to registry / report)
    """
    if is_git:
        return "code"
    if child_dirs_git:
        return "work"
    return None


# ----------------------------------------------------- pure: rule checker ---

def code_contains_work_violations(data):
    """Return list of (code_id, work_id) pairs where a code node has a work
    descendant - violating "code never contains work". Checks both the parent
    chain and explicit children[] lists.
    """
    index = nodes_index(data)
    violations = []

    def is_code_ancestor_of(child):
        # walk parents upward; report the nearest code ancestor if child is work
        seen = set()
        cur = child
        while cur and cur.get("parent"):
            pid = cur["parent"]
            if pid in seen:
                break
            seen.add(pid)
            parent = index.get(pid)
            if parent is None:
                break
            if parent.get("type") == "code" and child.get("type") == "work":
                return parent["id"]
            cur = parent
        return None

    for n in data["nodes"]:
        if n.get("type") == "work":
            code_anc = is_code_ancestor_of(n)
            if code_anc:
                violations.append((code_anc, n["id"]))

    # also via explicit children lists (in case parent not set on the child)
    for n in data["nodes"]:
        if n.get("type") != "code":
            continue
        for child_id in n.get("children", []) or []:
            child = index.get(child_id)
            if child is not None and child.get("type") == "work":
                pair = (n["id"], child_id)
                if pair not in violations:
                    violations.append(pair)
    return violations


def integrity_problems(data):
    """Dangling parents, unresolved link targets, unresolved children."""
    index = nodes_index(data)
    problems = []
    for n in data["nodes"]:
        if n.get("parent") and n["parent"] not in index:
            problems.append(f"node '{n['id']}' has dangling parent '{n['parent']}'")
        for child_id in n.get("children", []) or []:
            if child_id not in index:
                problems.append(
                    f"node '{n['id']}' lists unresolved child '{child_id}'")
        for link in n.get("links", []) or []:
            if link.get("to") not in index:
                problems.append(
                    f"node '{n['id']}' links to unresolved '{link.get('to')}'")
    return problems


def validate_rule(data):
    """Raise SystemExit if the registry violates the code-never-contains-work
    rule. Returns the (empty) violation list when clean."""
    violations = code_contains_work_violations(data)
    if violations:
        lines = "\n".join(f"  code '{c}' contains work '{w}'" for c, w in violations)
        sys.exit("rule violation (code never contains work):\n" + lines)
    return violations


# ------------------------------------------------------------ git helpers ---

def run_git(args, cwd=None):
    """Run git, return (rc, stdout, stderr) with text decoded. Never raises."""
    try:
        proc = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        return 127, "", "git not found"


def is_git_repo(path):
    """A directory is a repo if it has a .git dir OR a .git file (worktree)."""
    if not path or not os.path.isdir(path):
        return False
    dotgit = os.path.join(path, ".git")
    return os.path.isdir(dotgit) or os.path.isfile(dotgit)


def git_remote_url(path):
    rc, out, _ = run_git(["config", "--get", "remote.origin.url"], cwd=path)
    return out if rc == 0 and out else None


def git_branch(path):
    rc, out, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    return out if rc == 0 and out else None


def git_dirty(path):
    rc, out, _ = run_git(["status", "--porcelain"], cwd=path)
    return bool(out) if rc == 0 else False


def git_ahead_behind(path):
    """(ahead, behind, upstream_missing). Counts vs the tracking branch."""
    rc, _, _ = run_git(["rev-parse", "--abbrev-ref", "@{u}"], cwd=path)
    if rc != 0:
        return 0, 0, True
    ra, ahead, _ = run_git(["rev-list", "--count", "@{u}..HEAD"], cwd=path)
    rb, behind, _ = run_git(["rev-list", "--count", "HEAD..@{u}"], cwd=path)
    a = int(ahead) if ra == 0 and ahead.isdigit() else 0
    b = int(behind) if rb == 0 and behind.isdigit() else 0
    return a, b, False


def resolve_git_dir(repo_path):
    """Return the .git directory, resolving a `gitdir:` pointer file."""
    dotgit = os.path.join(repo_path, ".git")
    if os.path.isdir(dotgit):
        return dotgit
    if os.path.isfile(dotgit):
        with open(dotgit, encoding="utf-8") as f:
            content = f.read().strip()
        if content.startswith("gitdir:"):
            target = content[len("gitdir:"):].strip()
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(repo_path, target))
            return target
    return None


# -------------------------------------------------------------------- scan ---

def discover_disk_repos(root, data):
    """Map of absolute-path -> {git, branch, remote} for candidate dirs:
    every top-level dir under root PLUS every node's path (to catch nested
    sub-projects). Only dirs that exist are included."""
    candidates = set()
    if os.path.isdir(root):
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if os.path.isdir(p):
                candidates.add(os.path.normpath(p))
    for n in data["nodes"]:
        p = node_dir_on_disk(root, n)
        if p and os.path.isdir(p):
            candidates.add(os.path.normpath(p))

    info = {}
    for p in candidates:
        git = is_git_repo(p)
        info[p] = {
            "git": git,
            "branch": git_branch(p) if git else None,
            "remote": git_remote_url(p) if git else None,
        }
    return info


def cmd_scan(args):
    root = args.root
    workspace = workspace_dir(root, args.workspace)
    ppath = projects_path(workspace)
    data = load_registry(ppath)

    disk = discover_disk_repos(root, data)
    index = nodes_index(data)

    # --- refresh discoverable facts on existing nodes -----------------------
    refreshed = []
    missing_on_disk = []
    for n in data["nodes"]:
        p = node_dir_on_disk(root, n)
        present = bool(p and os.path.isdir(p))
        if p is None:
            continue  # logical container with no path (e.g. tsgb work root)
        if not present:
            missing_on_disk.append(n["id"])
            continue
        d = disk.get(os.path.normpath(p))
        if d and d["git"]:
            changes = []
            if d["remote"] and d["remote"] != n.get("remote"):
                changes.append(f"remote {n.get('remote')} -> {d['remote']}")
                n["remote"] = d["remote"]
            if d["branch"] and d["branch"] != n.get("branch"):
                changes.append(f"branch {n.get('branch')} -> {d['branch']}")
                n["branch"] = d["branch"]
            if changes:
                refreshed.append((n["id"], changes))

    # --- on-disk repos not in the registry ----------------------------------
    known_paths = {os.path.normpath(node_dir_on_disk(root, n))
                   for n in data["nodes"] if node_dir_on_disk(root, n)}
    candidates_to_add = []
    for p, d in sorted(disk.items()):
        if d["git"] and p not in known_paths:
            rel = os.path.relpath(p, root)
            candidates_to_add.append((rel, d["remote"]))

    # --- classification of unclassified top-level dirs ----------------------
    unclassified = []
    for p, d in sorted(disk.items()):
        if os.path.dirname(p) != os.path.normpath(root):
            continue
        if p in known_paths:
            continue
        child_dirs_git = any(
            is_git_repo(os.path.join(p, c))
            for c in (os.listdir(p) if os.path.isdir(p) else [])
            if os.path.isdir(os.path.join(p, c)))
        kind = classify_dir(p, d["git"], child_dirs_git)
        unclassified.append((os.path.relpath(p, root), kind or "unclassified"))

    # --- validation ---------------------------------------------------------
    rule_viol = code_contains_work_violations(data)
    integrity = integrity_problems(data)

    # --- report -------------------------------------------------------------
    print(f"# scan {root}  (workspace {workspace})")
    print(f"  registry nodes: {len(data['nodes'])}  on-disk dirs inspected: {len(disk)}")
    print()
    if refreshed:
        print("refreshed facts:")
        for nid, changes in refreshed:
            print(f"  {nid}: " + "; ".join(changes))
    else:
        print("refreshed facts: none (registry matches disk)")
    print()
    if missing_on_disk:
        print("DRIFT - nodes whose path is missing on disk:")
        for nid in missing_on_disk:
            print(f"  {nid} -> {index[nid].get('path')}")
    else:
        print("all node paths present on disk")
    print()
    if candidates_to_add:
        print("on-disk git repos NOT in the registry (candidates to `add`):")
        for rel, remote in candidates_to_add:
            print(f"  {rel}  ({remote or 'no remote'})")
    else:
        print("no unregistered git repos found")
    print()
    if unclassified:
        print("unregistered top-level dirs (classified):")
        for rel, kind in unclassified:
            print(f"  {rel}  -> {kind}")
        print()
    if rule_viol:
        print("RULE VIOLATIONS (code never contains work):")
        for c, w in rule_viol:
            print(f"  code '{c}' contains work '{w}'")
    else:
        print("rule OK: no code node contains a work descendant")
    if integrity:
        print("INTEGRITY PROBLEMS:")
        for line in integrity:
            print(f"  {line}")
    else:
        print("integrity OK: no dangling parents / children / link targets")
    print()

    if args.write:
        if rule_viol:
            sys.exit("refusing to --write while rule violations exist; fix first")
        data["updated"] = datetime.now().strftime("%Y-%m-%d")
        save_registry(data, ppath)
        render_registry(data, registry_path(workspace))
        print(f"wrote {ppath} and re-rendered {registry_path(workspace)}")
    else:
        print("(dry run - nothing written; pass --write to persist)")


# -------------------------------------------------------------------- list ---

def work_nodes(data):
    """Every work-typed node gets its own `## WORK` section, in registry order
    (a sub-work like Firmware also still appears nested under its parent work).
    """
    return [n for n in data["nodes"] if n.get("type") == "work"]


def group_label(data, work_id, group_id):
    return (data.get("groupLabels", {}).get(work_id, {}) or {}).get(
        group_id, group_id)


def top_level_members(data, work_id):
    """Direct members of a work: nodes that belong to it with no parent (a
    sub-work like Firmware is itself a member here AND gets its own section).
    Sorted by name."""
    members = [n for n in data["nodes"]
               if n.get("work") == work_id and not n.get("parent")]
    return sorted(members, key=lambda n: n["name"].lower())


def child_nodes(data, parent_id):
    """Children of a node. Honour the parent's explicit children[] order if
    present (it encodes a meaningful lineage); else fall back to name order."""
    index = nodes_index(data)
    parent = index.get(parent_id)
    ordered = (parent.get("children") if parent else None) or []
    out = [index[c] for c in ordered if c in index
           and index[c].get("parent") == parent_id]
    seen = {c["id"] for c in out}
    extra = sorted((n for n in data["nodes"]
                    if n.get("parent") == parent_id and n["id"] not in seen),
                   key=lambda n: n["name"].lower())
    return out + extra


def print_node_tree(data, node, indent):
    pad = "  " * indent
    remote = node.get("remote") or "(no remote)"
    branch = node.get("branch") or "-"
    print(f"{pad}- {node['name']} [{node.get('type')}] {branch} {remote}")
    for child in child_nodes(data, node["id"]):
        print_node_tree(data, child, indent + 1)


def cmd_list(args):
    data = load_registry(projects_path(workspace_dir(args.root, args.workspace)))

    if args.links:
        for n in data["nodes"]:
            for link in n.get("links", []) or []:
                target = node_by_id(data, link.get("to"))
                tname = target["name"] if target else link.get("to")
                note = link.get("note", "")
                print(f"{n['name']}  -{link.get('type')}->  {tname} - {note}")
        return

    works = work_nodes(data)
    if args.work:
        works = [w for w in works if w["id"] == args.work]
        if not works:
            sys.exit(f"no top-level work with id '{args.work}'")

    for w in works:
        print(f"WORK - {w['name']}")
        # group members under their group labels
        members = top_level_members(data, w["id"])
        by_group = {}
        for m in members:
            by_group.setdefault(m.get("group"), []).append(m)
        for group_id in sorted(by_group, key=lambda g: (g is None, str(g))):
            label = group_label(data, w["id"], group_id) if group_id else "(no group)"
            print(f"  {label}")
            for m in sorted(by_group[group_id], key=lambda n: n["name"].lower()):
                print_node_tree(data, m, 2)
        print()


# --------------------------------------------------------------------- add ---

def add_node(data, node_id, name, ntype, work=None, group=None, path=None,
             remote=None, parent=None, description=""):
    if node_by_id(data, node_id) is not None:
        sys.exit(f"node id '{node_id}' already exists")
    if parent and node_by_id(data, parent) is None:
        sys.exit(f"parent node '{parent}' does not exist")
    node = {
        "id": node_id, "name": name, "type": ntype,
        "path": path, "remote": remote, "branch": None,
        "visibility": "private", "stack": "",
        "description": description,
        "work": work, "group": group, "parent": parent,
    }
    data["nodes"].append(node)
    if parent:
        p = node_by_id(data, parent)
        p.setdefault("children", [])
        if node_id not in p["children"]:
            p["children"].append(node_id)
    validate_rule(data)  # rejects code-contains-work; exits before save
    return node


def cmd_add(args):
    workspace = workspace_dir(args.root, args.workspace)
    ppath = projects_path(workspace)
    data = load_registry(ppath)
    add_node(data, args.id, args.name, args.type, work=args.work,
             group=args.group, path=args.path, remote=args.remote,
             parent=args.parent, description=args.description or "")
    save_registry(data, ppath)
    render_registry(data, registry_path(workspace))
    print(f"added node '{args.id}' ({args.type})")


# ------------------------------------------------------------------ remove ---

def remove_node(data, node_id):
    node = node_by_id(data, node_id)
    if node is None:
        sys.exit(f"no node with id '{node_id}'")
    data["nodes"].remove(node)
    # strip from any children[] lists
    for n in data["nodes"]:
        if node_id in (n.get("children") or []):
            n["children"].remove(node_id)
            if not n["children"]:
                del n["children"]
    # warn about now-dangling inbound links
    dangling = []
    for n in data["nodes"]:
        for link in n.get("links", []) or []:
            if link.get("to") == node_id:
                dangling.append((n["id"], link.get("type")))
    return dangling


def cmd_remove(args):
    workspace = workspace_dir(args.root, args.workspace)
    ppath = projects_path(workspace)
    data = load_registry(ppath)
    dangling = remove_node(data, args.id)
    save_registry(data, ppath)
    render_registry(data, registry_path(workspace))
    print(f"removed node '{args.id}'")
    for nid, ltype in dangling:
        print(f"  WARNING: '{nid}' -{ltype}-> '{args.id}' now dangles "
              "(use `unlink` to clean it up)")


# -------------------------------------------------------------------- links ---

def add_link(data, from_id, to_id, ltype, note=""):
    src = node_by_id(data, from_id)
    if src is None:
        sys.exit(f"no node with id '{from_id}'")
    if node_by_id(data, to_id) is None:
        sys.exit(f"no node with id '{to_id}'")
    if ltype not in (data.get("edgeTypes") or []):
        print(f"  WARNING: edge type '{ltype}' is not in edgeTypes "
              f"({', '.join(data.get('edgeTypes', []))})", file=sys.stderr)
    link = {"to": to_id, "type": ltype, "note": note}
    src.setdefault("links", []).append(link)
    return link


def remove_link(data, from_id, to_id, ltype=None):
    src = node_by_id(data, from_id)
    if src is None:
        sys.exit(f"no node with id '{from_id}'")
    links = src.get("links", [])
    kept = [l for l in links
            if not (l.get("to") == to_id and (ltype is None or l.get("type") == ltype))]
    removed = len(links) - len(kept)
    if removed == 0:
        sys.exit(f"no matching link {from_id} -> {to_id}"
                 + (f" of type '{ltype}'" if ltype else ""))
    if kept:
        src["links"] = kept
    else:
        src.pop("links", None)
    return removed


def cmd_link(args):
    workspace = workspace_dir(args.root, args.workspace)
    ppath = projects_path(workspace)
    data = load_registry(ppath)
    add_link(data, args.from_id, args.to_id, args.type, note=args.note or "")
    save_registry(data, ppath)
    render_registry(data, registry_path(workspace))
    print(f"linked {args.from_id} -{args.type}-> {args.to_id}")


def cmd_unlink(args):
    workspace = workspace_dir(args.root, args.workspace)
    ppath = projects_path(workspace)
    data = load_registry(ppath)
    n = remove_link(data, args.from_id, args.to_id, args.type)
    save_registry(data, ppath)
    render_registry(data, registry_path(workspace))
    print(f"removed {n} link(s) {args.from_id} -> {args.to_id}")


def cmd_links(args):
    data = load_registry(projects_path(workspace_dir(args.root, args.workspace)))
    node = node_by_id(data, args.id)
    if node is None:
        sys.exit(f"no node with id '{args.id}'")
    print(f"# links for {node['name']} ({args.id})")
    print("outbound:")
    out = node.get("links", []) or []
    if not out:
        print("  (none)")
    for link in out:
        target = node_by_id(data, link.get("to"))
        tname = target["name"] if target else link.get("to")
        print(f"  -{link.get('type')}-> {tname} - {link.get('note', '')}")
    print("inbound:")
    found = False
    for n in data["nodes"]:
        for link in n.get("links", []) or []:
            if link.get("to") == args.id:
                found = True
                print(f"  <-{link.get('type')}- {n['name']} - {link.get('note', '')}")
    if not found:
        print("  (none)")


# -------------------------------------------------------------------- note ---

def notes_file(workspace, node_id):
    return os.path.join(workspace, "notes", node_id, "NOTES.md")


def append_note(workspace, data, node_id, text):
    node = node_by_id(data, node_id)
    if node is None:
        sys.exit(f"no node with id '{node_id}'")
    path = notes_file(workspace, node_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new_file = not os.path.exists(path)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "a", encoding="utf-8") as f:
        if new_file:
            f.write(f"# {node['name']} notes\n\n")
        f.write(f"- [{stamp}] {text}\n")
    rel = os.path.relpath(path, workspace).replace(os.sep, "/")
    if not node.get("notes"):
        node["notes"] = rel
        return path, True
    return path, False


def cmd_note(args):
    workspace = workspace_dir(args.root, args.workspace)
    ppath = projects_path(workspace)
    data = load_registry(ppath)
    if node_by_id(data, args.id) is None:
        sys.exit(f"no node with id '{args.id}'")
    path = notes_file(workspace, args.id)
    if args.text and not args.show:
        _, set_field = append_note(workspace, data, args.id, args.text)
        if set_field:
            save_registry(data, ppath)
            render_registry(data, registry_path(workspace))
        print(f"appended note to {path}")
        return
    # show
    if not os.path.exists(path):
        print(f"(no notes yet for '{args.id}' - {path})")
        return
    with open(path, encoding="utf-8") as f:
        sys.stdout.write(f.read())


# -------------------------------------------------------------------- plan ---

def add_plan(data, node_id, plan_path):
    node = node_by_id(data, node_id)
    if node is None:
        sys.exit(f"no node with id '{node_id}'")
    plans = node.setdefault("plans", [])
    if plan_path not in plans:
        plans.append(plan_path)
        return True
    return False


def cmd_plan(args):
    workspace = workspace_dir(args.root, args.workspace)
    ppath = projects_path(workspace)
    data = load_registry(ppath)
    node = node_by_id(data, args.id)
    if node is None:
        sys.exit(f"no node with id '{args.id}'")
    if args.plan_command == "add":
        added = add_plan(data, args.id, args.path)
        if added:
            save_registry(data, ppath)
            render_registry(data, registry_path(workspace))
            print(f"added plan '{args.path}' to '{args.id}'")
        else:
            print(f"plan '{args.path}' already listed for '{args.id}'")
    else:  # list
        plans = node.get("plans", []) or []
        if not plans:
            print(f"(no plans for '{args.id}')")
        for p in plans:
            print(f"  {p}")


# ------------------------------------------------------------------ status ---

def repo_nodes(data):
    """Nodes that have a path AND a remote AND aren't flagged no-remote."""
    out = []
    for n in data["nodes"]:
        flags = n.get("flags", []) or []
        if not n.get("path") or not n.get("remote"):
            continue
        if "no-remote" in flags:
            continue
        out.append(n)
    return out


def cmd_status(args):
    root = args.root
    workspace = workspace_dir(root, args.workspace)
    data = load_registry(projects_path(workspace))

    rows = []
    for n in repo_nodes(data):
        path = node_dir_on_disk(root, n)
        if not is_git_repo(path):
            rows.append((n["name"], "missing", "-", "-", "-"))
            continue
        dirty = "dirty" if git_dirty(path) else "clean"
        ahead, behind, no_upstream = git_ahead_behind(path)
        up = "no-upstream" if no_upstream else "ok"
        rows.append((n["name"], dirty, str(ahead), str(behind), up))

    if not rows:
        print("no pushable repo nodes")
        return
    name_w = max(len(r[0]) for r in rows)
    print(f"{'repo'.ljust(name_w)}  {'tree':<7} {'ahead':>5} {'behind':>6}  upstream")
    for name, dirty, ahead, behind, up in rows:
        print(f"{name.ljust(name_w)}  {dirty:<7} {ahead:>5} {behind:>6}  {up}")


# -------------------------------------------------------------------- push ---

def is_conventional(msg):
    return bool(CONVENTIONAL_RE.match(msg.strip().splitlines()[0])) if msg else False


def push_url_for(remote):
    """For https github remotes, inject GH_TOKEN transiently if present.
    Returns (url, used_token). Never persists or prints the token."""
    token = os.environ.get("GH_TOKEN")
    if token and remote.startswith("https://github.com/"):
        rest = remote[len("https://"):]
        return f"https://x-access-token:{token}@{rest}", True
    return remote, False


def push_repo(node, root, msg=None):
    """Commit (if dirty, needs msg) then push one node. Returns a result str."""
    flags = node.get("flags", []) or []
    if "never-push" in flags or "no-remote" in flags:
        return f"{node['name']}: skipped (flag)"
    path = node_dir_on_disk(root, node)
    if not is_git_repo(path):
        return f"{node['name']}: skipped (not a git repo on disk)"
    remote = node.get("remote")
    if not remote:
        return f"{node['name']}: skipped (no remote)"

    if git_dirty(path):
        if not msg:
            return f"{node['name']}: DIRTY - needs -m MSG to commit"
        if not is_conventional(msg):
            print(f"  WARNING: '{msg}' is not a Conventional Commits subject "
                  "(type(scope): summary)", file=sys.stderr)
        rc, _, err = run_git(["add", "-A"], cwd=path)
        if rc != 0:
            return f"{node['name']}: git add failed - {err}"
        rc, _, err = run_git([
            "-c", f"user.name={COMMIT_AUTHOR_NAME}",
            "-c", f"user.email={COMMIT_AUTHOR_EMAIL}",
            "commit", "-m", msg], cwd=path)
        if rc != 0:
            return f"{node['name']}: commit failed - {err}"

    url, used_token = push_url_for(remote)
    branch = git_branch(path) or node.get("branch") or "HEAD"
    if used_token:
        rc, _, err = run_git(["push", url, branch], cwd=path)
    else:
        rc, _, err = run_git(["push", "origin", branch], cwd=path)
    if rc != 0:
        return f"{node['name']}: push failed - {err}"
    return f"{node['name']}: pushed {branch}"


def cmd_push(args):
    root = args.root
    workspace = workspace_dir(root, args.workspace)
    data = load_registry(projects_path(workspace))

    if args.all:
        targets = repo_nodes(data)
    else:
        node = node_by_id(data, args.id)
        if node is None:
            sys.exit(f"no node with id '{args.id}'")
        targets = [node]

    for node in targets:
        print(push_repo(node, root, msg=args.message))


# -------------------------------------------------------------------- sync ---

def cmd_sync(args):
    root = args.root
    workspace = workspace_dir(root, args.workspace)
    data = load_registry(projects_path(workspace))

    pushed, dirty, behind_repos, clean = [], [], [], []
    for n in repo_nodes(data):
        path = node_dir_on_disk(root, n)
        if not is_git_repo(path):
            continue
        if git_dirty(path):
            dirty.append(n["name"])
            continue
        ahead, behind, no_upstream = git_ahead_behind(path)
        if no_upstream:
            continue
        if ahead > 0:
            result = push_repo(n, root)  # already committed; just push
            pushed.append(result)
        if behind > 0:
            behind_repos.append(f"{n['name']} (behind {behind})")
        if ahead == 0 and behind == 0:
            clean.append(n["name"])

    print("# sync checkpoint")
    print("pushed (were ahead):")
    for r in pushed or ["  (none)"]:
        print(f"  {r}" if pushed else r)
    print("DIRTY - need an explicit `push -m MSG` (checkpoint won't invent messages):")
    for name in dirty or ["  (none)"]:
        print(f"  {name}" if dirty else name)
    if behind_repos:
        print("behind upstream (consider a pull):")
        for name in behind_repos:
            print(f"  {name}")
    print(f"in sync: {len(clean)} repo(s)")


# --------------------------------------------------------------------- log ---

def sessions_dir(workspace):
    return os.path.join(workspace, "sessions")


def latest_session_file(workspace):
    d = sessions_dir(workspace)
    if not os.path.isdir(d):
        return None
    files = sorted(f for f in os.listdir(d) if f.endswith(".md"))
    return os.path.join(d, files[-1]) if files else None


def cmd_log(args):
    workspace = workspace_dir(args.root, args.workspace)
    today = datetime.now().strftime("%Y-%m-%d")
    today_path = os.path.join(sessions_dir(workspace), f"{today}.md")

    if args.text and not (args.show or args.resume):
        os.makedirs(sessions_dir(workspace), exist_ok=True)
        new_file = not os.path.exists(today_path)
        stamp = datetime.now().strftime("%H:%M")
        with open(today_path, "a", encoding="utf-8") as f:
            if new_file:
                f.write(f"# Session {today}\n\n")
            f.write(f"- [{stamp}] {args.text}\n")
        print(f"logged to {today_path}")
        return

    target = today_path if (os.path.exists(today_path) and not args.resume) \
        else latest_session_file(workspace)
    if not target or not os.path.exists(target):
        print("(no session logs yet)")
        return
    with open(target, encoding="utf-8") as f:
        text = f.read()
    if args.resume:
        print(f"# resume from {os.path.basename(target)}")
        for line in text.splitlines():
            mark = "  <<<" if re.search(r"\b(next|resume)\b", line, re.I) else ""
            print(line + mark)
    else:
        sys.stdout.write(text)


# ------------------------------------------------------------------- hooks ---

def install_hook(repo_path):
    """Write the no-attribution commit-msg hook. Returns the hook path."""
    git_dir = resolve_git_dir(repo_path)
    if git_dir is None:
        raise FileNotFoundError(f"no .git for {repo_path}")
    hooks_dir = os.path.join(git_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "commit-msg")
    with open(hook_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(HOOK_BODY)
    os.chmod(hook_path, 0o755)  # cosmetic on Windows, correct on POSIX
    return hook_path


def cmd_hooks(args):
    root = args.root
    workspace = workspace_dir(root, args.workspace)
    data = load_registry(projects_path(workspace))

    if args.all:
        targets = [n for n in data["nodes"]
                   if n.get("path") and is_git_repo(node_dir_on_disk(root, n))]
    else:
        node = node_by_id(data, args.id)
        if node is None:
            sys.exit(f"no node with id '{args.id}'")
        targets = [node]

    for node in targets:
        path = node_dir_on_disk(root, node)
        if not is_git_repo(path):
            print(f"{node['name']}: skipped (not a git repo on disk)")
            continue
        try:
            hook = install_hook(path)
            print(f"{node['name']}: installed {hook}")
        except OSError as e:
            print(f"{node['name']}: FAILED - {e}")


# ------------------------------------------------------------------ render ---

def render_registry(data, path):
    """Regenerate REGISTRY.md from the registry data. Deterministic; members
    sorted by name. Returns the rendered text and writes it to `path`."""
    text = render_text(data)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return text


def render_node_bullet(node, lines):
    bits = [f"`{node.get('type')}`"]
    if node.get("stack"):
        bits.append(node["stack"])
    if node.get("visibility"):
        bits.append(node["visibility"])
    bits.append(f"`{node.get('branch') or '-'}`")
    bits.append(node.get("remote") or "(no remote)")
    lines.append(f"- **{node['name']}** · " + " · ".join(bits))
    if node.get("description"):
        lines.append(f"    {node['description']}")


def render_children(data, parent_id, lines):
    for child in child_nodes(data, parent_id):
        desc = child.get("description", "")
        lines.append(f"        - `{child['name']}` ({child.get('type')}) — {desc}")
        render_children(data, child["id"], lines)


def render_text(data):
    lines = []
    lines.append("# REGISTRY — the contiguous image of D:\\claude")
    lines.append("")
    lines.append("> Auto-rendered from `projects.json` by "
                 "`claude_helpers\\claude_projects.py render`. Do not hand-edit. "
                 "The **workspace is the whole `D:\\claude` estate** — one "
                 "interconnected body of work.")
    lines.append("")
    lines.append(f"_Updated {data.get('updated', '')}. "
                 f"Rule: {data.get('rule', '')}_")
    lines.append("")
    lines.append("")

    for w in work_nodes(data):
        lines.append(f"## WORK — {w['name']}")
        if w.get("note"):
            lines.append(f"_{w['note']}_")
        if w.get("description"):
            lines.append(w["description"])
        members = top_level_members(data, w["id"])
        if members:
            lines.append("")
        by_group = {}
        for m in members:
            by_group.setdefault(m.get("group"), []).append(m)
        # order groups by the groupLabels declaration, then any extras
        declared = list((data.get("groupLabels", {}).get(w["id"], {}) or {}).keys())
        ordered = [g for g in declared if g in by_group] + \
                  [g for g in by_group if g not in declared and g is not None] + \
                  ([None] if None in by_group else [])
        for group_id in ordered:
            label = group_label(data, w["id"], group_id) if group_id else "(ungrouped)"
            lines.append(f"### {label}")
            for m in sorted(by_group[group_id], key=lambda n: n["name"].lower()):
                render_node_bullet(m, lines)
                render_children(data, m["id"], lines)
        lines.append("")
        lines.append("")

    # ASSETS / UNCLASSIFIED: nodes with no work
    misc = sorted((n for n in data["nodes"]
                   if n.get("work") is None and n.get("type") != "work"),
                  key=lambda n: n["name"].lower())
    lines.append("## ASSETS / UNCLASSIFIED")
    for n in misc:
        flags = n.get("flags", []) or []
        lines.append(f"- **{n['name']}** · `{n.get('type')}` · "
                     f"{n.get('stack', '')} · flags={flags}")
        if n.get("description"):
            lines.append(f"    {n['description']}")
    lines.append("")

    # LINK GRAPH
    lines.append("## LINK GRAPH — everything links to something else")
    lines.append("Sourced from `TSGB 2026\\Software\\README.md` §interconnect "
                 "+ NuGet facts.")
    lines.append("")
    for n in data["nodes"]:
        for link in n.get("links", []) or []:
            target = node_by_id(data, link.get("to"))
            tname = target["name"] if target else link.get("to")
            lines.append(f"- `{n['name']}`  —{link.get('type')}→  "
                         f"`{tname}` — {link.get('note', '')}")

    return "\n".join(lines) + "\n"


def cmd_render(args):
    workspace = workspace_dir(args.root, args.workspace)
    data = load_registry(projects_path(workspace))
    rpath = registry_path(workspace)
    render_registry(data, rpath)
    print(f"rendered {rpath}")


# -------------------------------------------------------------------- CLI ---

def build_parser():
    parser = argparse.ArgumentParser(
        description="Project registry, notes/plans and git-sync for D:\\claude")
    parser.add_argument("--root", default=DEFAULT_ROOT,
                        help="estate root (default: %(default)s; "
                             "env CLAUDE_ESTATE_ROOT)")
    parser.add_argument("--workspace", default=None,
                        help="override <root>\\_claude")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="reconcile registry with on-disk repos")
    p.add_argument("--write", action="store_true",
                   help="persist refreshed facts and re-render REGISTRY.md")

    p = sub.add_parser("list", help="print the registry")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--tree", action="store_true", help="work/group/node tree (default)")
    g.add_argument("--work", help="only this work's subtree")
    g.add_argument("--links", action="store_true", help="the relationship graph")

    p = sub.add_parser("add", help="add a node")
    p.add_argument("id")
    p.add_argument("--name", required=True)
    p.add_argument("--type", required=True, choices=["work", "code", "assets"])
    p.add_argument("--work", help="top-level work id this belongs to")
    p.add_argument("--group")
    p.add_argument("--path")
    p.add_argument("--remote")
    p.add_argument("--parent", help="parent node id")
    p.add_argument("--description")

    p = sub.add_parser("remove", help="remove a node")
    p.add_argument("id")

    p = sub.add_parser("link", help="add a relationship edge")
    p.add_argument("from_id", metavar="from")
    p.add_argument("to_id", metavar="to")
    p.add_argument("--type", required=True)
    p.add_argument("--note")

    p = sub.add_parser("unlink", help="remove a relationship edge")
    p.add_argument("from_id", metavar="from")
    p.add_argument("to_id", metavar="to")
    p.add_argument("--type", help="only edges of this type")

    p = sub.add_parser("links", help="show inbound/outbound links for a node")
    p.add_argument("id")

    p = sub.add_parser("note", help="append/show per-project notes")
    p.add_argument("id")
    p.add_argument("text", nargs="?", help="note text to append")
    p.add_argument("--show", action="store_true", help="print the notes file")

    p = sub.add_parser("plan", help="manage a node's plan list")
    p.add_argument("id")
    plan_sub = p.add_subparsers(dest="plan_command", required=True)
    pa = plan_sub.add_parser("add")
    pa.add_argument("path")
    plan_sub.add_parser("list")

    p = sub.add_parser("status", help="git status across repo nodes")
    p.add_argument("--all", action="store_true",
                   help="every repo node (the default behaviour)")

    p = sub.add_parser("push", help="commit (with -m) and push repo node(s)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("id", nargs="?", help="node id to push")
    g.add_argument("--all", action="store_true", help="push every repo node")
    p.add_argument("-m", "--message", help="Conventional Commits subject (if dirty)")

    p = sub.add_parser("sync", help="push already-committed-but-unpushed repos")
    p.add_argument("--all", action="store_true",
                   help="every repo node (the default behaviour)")

    p = sub.add_parser("log", help="dated session status log")
    p.add_argument("text", nargs="?", help="line to append to today's log")
    p.add_argument("--show", action="store_true", help="print today's/latest log")
    p.add_argument("--resume", action="store_true",
                   help="print the latest log, highlighting next/resume lines")

    p = sub.add_parser("hooks", help="install the no-attribution commit-msg hook")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("id", nargs="?", help="node id")
    g.add_argument("--all", action="store_true", help="every git-repo node")

    sub.add_parser("render", help="regenerate REGISTRY.md from projects.json")

    return parser


DISPATCH = {
    "scan": cmd_scan, "list": cmd_list, "add": cmd_add, "remove": cmd_remove,
    "link": cmd_link, "unlink": cmd_unlink, "links": cmd_links, "note": cmd_note,
    "plan": cmd_plan, "status": cmd_status, "push": cmd_push, "sync": cmd_sync,
    "log": cmd_log, "hooks": cmd_hooks, "render": cmd_render,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
