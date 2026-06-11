#!/usr/bin/env python3
r"""Bug-list collector for the D:\claude estate.

A durable, machine-readable backlog of bug reports gathered from chat
(primarily Slack) so they can be triaged, grouped by similarity, fixed and
pushed in batches. Claude does the talking to Slack (via the Slack MCP tools)
and the semantic grouping; this helper is the *store* underneath - it persists
the list across sessions, lets bugs be bulk-imported in one shot, labelled into
similarity groups, and marked resolved once their fix ships. Stdlib only - no
pip installs needed.

Duplicates are kept on purpose: the same bug surfacing in three channels is
signal, and de-duping is a judgement call left to triage, not to ingest. `add`
and `import` therefore never reject a record for looking familiar.

Typical flow ("update bugs from Slack"):
  1. Claude reads every Slack channel and builds a JSON array of records.
  2. `import` loads them all in one go (ids auto-assigned, never reused).
  3. `list --status open --json` feeds triage; Claude clusters similar ones.
  4. `group --label <name> <id...>` records each similarity cluster.
  5. fix + push the cluster, then `resolve --group <name>`.

The store defaults to env CLAUDE_BUGS_STORE, else
`<CLAUDE_ESTATE_ROOT|D:\claude>\_claude\bugs\bugs.json`.

Usage:
  python claude_bugs.py add TEXT [--channel C] [--author A] [--ts T]
                                 [--permalink U] [--source S] [--group G]
  python claude_bugs.py import [--file PATH]      # JSON array from file or stdin
  python claude_bugs.py list [--status open|resolved|all] [--channel C]
                             [--group G | --ungrouped] [--json] [--full]
  python claude_bugs.py group --label NAME ID [ID ...]
  python claude_bugs.py ungroup ID [ID ...]
  python claude_bugs.py groups [--json]
  python claude_bugs.py resolve (ID ... | --group NAME | --all)
  python claude_bugs.py reopen  (ID ... | --group NAME | --all)
  python claude_bugs.py remove  (ID ... | --resolved | --all [--yes])
  python claude_bugs.py stats [--json]
"""

import argparse
import json
import os
import sys
from datetime import datetime

DEFAULT_ROOT = os.environ.get("CLAUDE_ESTATE_ROOT", "D:\\claude")

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
VALID_STATUS = (STATUS_OPEN, STATUS_RESOLVED)


# ----------------------------------------------------------- path helpers ---

def default_store():
    env = os.environ.get("CLAUDE_BUGS_STORE")
    if env:
        return env
    return os.path.join(DEFAULT_ROOT, "_claude", "bugs", "bugs.json")


def today():
    return datetime.now().strftime("%Y-%m-%d")


# --------------------------------------------------------------- file I/O ---

def empty_store():
    return {"schema": "claude-bugs/v1", "updated": today(), "seq": 0,
            "bugs": []}


def load_store(path):
    """Load the store, or a fresh empty one if it does not exist yet."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty_store()
    except ValueError as e:
        sys.exit(f"{path} is not valid JSON ({e}) - fix it first.")
    if not isinstance(data, dict) or "bugs" not in data:
        sys.exit(f"{path} is not a bug store (no 'bugs').")
    data.setdefault("seq", max((b.get("id", 0) for b in data["bugs"]), default=0))
    return data


def save_store(data, path):
    """Write atomically, keeping a one-deep .bak of the previous version."""
    data["updated"] = today()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
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


# --------------------------------------------------- pure: mutate the store ---

def normalize_record(raw):
    """Coerce one inbound dict into a stored bug shape (id assigned later).

    Accepts loose field names so a Slack scrape can be passed straight in:
    `text`/`message`/`body` for the text, `channel`/`chan`, `author`/`user`,
    `ts`/`timestamp`/`time`, `permalink`/`url`/`link`.
    """
    def pick(*names):
        for n in names:
            v = raw.get(n)
            if v not in (None, ""):
                return v
        return None

    text = pick("text", "message", "body", "msg")
    if not text:
        raise ValueError("record has no text/message")
    status = raw.get("status", STATUS_OPEN)
    if status not in VALID_STATUS:
        raise ValueError(f"bad status {status!r}")
    return {
        "text": str(text).strip(),
        "channel": pick("channel", "chan"),
        "author": pick("author", "user", "from"),
        "ts": pick("ts", "timestamp", "time"),
        "permalink": pick("permalink", "url", "link"),
        "source": raw.get("source", "slack"),
        "status": status,
        "group": raw.get("group"),
    }


def add_bug(data, record):
    """Append a normalized record, assigning the next never-reused id."""
    data["seq"] += 1
    bug = dict(record)
    bug["id"] = data["seq"]
    bug["added"] = today()
    data["bugs"].append(bug)
    return bug


def import_records(data, records):
    """Bulk-add a list of raw dicts. Returns (added_bugs, errors)."""
    added, errors = [], []
    for i, raw in enumerate(records):
        try:
            added.append(add_bug(data, normalize_record(raw)))
        except (ValueError, AttributeError) as e:
            errors.append((i, str(e)))
    return added, errors


def select(data, ids=None, group=None, all_=False):
    """Resolve a selector to a list of bug dicts."""
    if all_:
        return list(data["bugs"])
    if group is not None:
        return [b for b in data["bugs"] if b.get("group") == group]
    idset = set(ids or [])
    return [b for b in data["bugs"] if b["id"] in idset]


def set_status(bugs, status):
    for b in bugs:
        b["status"] = status
    return len(bugs)


def assign_group(bugs, label):
    for b in bugs:
        b["group"] = label
    return len(bugs)


def remove_bugs(data, predicate):
    """Drop bugs where predicate(bug) is true. Returns count removed."""
    before = len(data["bugs"])
    data["bugs"] = [b for b in data["bugs"] if not predicate(b)]
    return before - len(data["bugs"])


# ------------------------------------------------------------ pure: views ---

def filter_bugs(data, status="all", channel=None, group=None, ungrouped=False):
    out = []
    for b in data["bugs"]:
        if status != "all" and b.get("status", STATUS_OPEN) != status:
            continue
        if channel is not None and b.get("channel") != channel:
            continue
        if ungrouped and b.get("group"):
            continue
        if group is not None and b.get("group") != group:
            continue
        out.append(b)
    return out


def summarize(data):
    bugs = data["bugs"]
    by_status, by_channel, by_group = {}, {}, {}
    for b in bugs:
        by_status[b.get("status", STATUS_OPEN)] = \
            by_status.get(b.get("status", STATUS_OPEN), 0) + 1
        ch = b.get("channel") or "(none)"
        by_channel[ch] = by_channel.get(ch, 0) + 1
        g = b.get("group") or "(ungrouped)"
        by_group[g] = by_group.get(g, 0) + 1
    return {"total": len(bugs), "by_status": by_status,
            "by_channel": by_channel, "by_group": by_group}


# ------------------------------------------------------------ formatting ---

def fmt_bug(b, full=False):
    head = f"#{b['id']:<4} [{b.get('status', STATUS_OPEN)}]"
    grp = f" {{{b['group']}}}" if b.get("group") else ""
    chan = b.get("channel") or "?"
    text = b["text"] if full else (b["text"].replace("\n", " ")[:100])
    meta = f"  ({chan}"
    if b.get("author"):
        meta += f" · {b['author']}"
    meta += ")"
    return f"{head}{grp}{meta}\n      {text}"


# --------------------------------------------------------------- commands ---

def cmd_add(args):
    data = load_store(args.store)
    rec = normalize_record({
        "text": args.text, "channel": args.channel, "author": args.author,
        "ts": args.ts, "permalink": args.permalink,
        "source": args.source, "group": args.group,
    })
    bug = add_bug(data, rec)
    save_store(data, args.store)
    print(f"added bug #{bug['id']}")


def cmd_import(args):
    if args.file:
        # utf-8-sig tolerates the BOM that Windows PowerShell's Out-File writes.
        with open(args.file, encoding="utf-8-sig") as f:
            payload = f.read()
    else:
        payload = sys.stdin.read().lstrip("﻿")
    try:
        records = json.loads(payload)
    except ValueError as e:
        sys.exit(f"input is not valid JSON ({e}).")
    if isinstance(records, dict):
        records = records.get("bugs", [records])
    if not isinstance(records, list):
        sys.exit("expected a JSON array of records (or {'bugs': [...]}).")
    data = load_store(args.store)
    added, errors = import_records(data, records)
    save_store(data, args.store)
    print(f"imported {len(added)} bug(s)"
          + (f" (ids #{added[0]['id']}-#{added[-1]['id']})" if added else ""))
    for i, msg in errors:
        print(f"  skipped record {i}: {msg}", file=sys.stderr)


def cmd_list(args):
    data = load_store(args.store)
    bugs = filter_bugs(data, status=args.status, channel=args.channel,
                       group=args.group, ungrouped=args.ungrouped)
    if args.json:
        json.dump(bugs, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    if not bugs:
        print("no bugs match.")
        return
    for b in bugs:
        print(fmt_bug(b, full=args.full))
    print(f"\n{len(bugs)} bug(s).")


def cmd_group(args):
    data = load_store(args.store)
    bugs = select(data, ids=args.ids)
    missing = set(args.ids) - {b["id"] for b in bugs}
    n = assign_group(bugs, args.label)
    save_store(data, args.store)
    print(f"grouped {n} bug(s) into {args.label!r}.")
    if missing:
        print(f"  no such id: {sorted(missing)}", file=sys.stderr)


def cmd_ungroup(args):
    data = load_store(args.store)
    bugs = select(data, ids=args.ids)
    n = assign_group(bugs, None)
    save_store(data, args.store)
    print(f"ungrouped {n} bug(s).")


def cmd_groups(args):
    data = load_store(args.store)
    groups = {}
    for b in data["bugs"]:
        g = b.get("group") or "(ungrouped)"
        groups.setdefault(g, {"open": 0, "resolved": 0, "ids": []})
        groups[g][b.get("status", STATUS_OPEN)] += 1
        groups[g]["ids"].append(b["id"])
    if args.json:
        json.dump(groups, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    if not groups:
        print("no bugs yet.")
        return
    for g in sorted(groups):
        info = groups[g]
        print(f"{g:<28} {info['open']:>3} open  {info['resolved']:>3} resolved"
              f"   ids={info['ids']}")


def _select_for_mutation(data, args):
    if getattr(args, "all", False):
        return select(data, all_=True)
    if getattr(args, "group", None) is not None:
        return select(data, group=args.group)
    return select(data, ids=args.ids)


def cmd_resolve(args):
    data = load_store(args.store)
    n = set_status(_select_for_mutation(data, args), STATUS_RESOLVED)
    save_store(data, args.store)
    print(f"resolved {n} bug(s).")


def cmd_reopen(args):
    data = load_store(args.store)
    n = set_status(_select_for_mutation(data, args), STATUS_OPEN)
    save_store(data, args.store)
    print(f"reopened {n} bug(s).")


def cmd_remove(args):
    data = load_store(args.store)
    if args.all:
        if not args.yes:
            sys.exit("refusing to wipe every bug without --yes.")
        n = remove_bugs(data, lambda b: True)
    elif args.resolved:
        n = remove_bugs(data, lambda b: b.get("status") == STATUS_RESOLVED)
    else:
        idset = set(args.ids)
        n = remove_bugs(data, lambda b: b["id"] in idset)
    save_store(data, args.store)
    print(f"removed {n} bug(s).")


def cmd_stats(args):
    data = load_store(args.store)
    s = summarize(data)
    if args.json:
        json.dump(s, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    print(f"total: {s['total']}")
    print("by status:  " + ", ".join(f"{k}={v}" for k, v in
                                      sorted(s["by_status"].items())))
    print("by channel: " + ", ".join(f"{k}={v}" for k, v in
                                      sorted(s["by_channel"].items())))
    print("by group:   " + ", ".join(f"{k}={v}" for k, v in
                                      sorted(s["by_group"].items())))


# --------------------------------------------------------------- CLI wiring ---

def build_parser():
    parser = argparse.ArgumentParser(
        prog="claude_bugs.py",
        description="Durable bug-list collector for the D:\\claude estate.")
    parser.add_argument("--store", default=default_store(),
                        help="path to the bug store JSON (default: estate store)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add", help="add a single bug")
    p.add_argument("text")
    p.add_argument("--channel")
    p.add_argument("--author")
    p.add_argument("--ts")
    p.add_argument("--permalink")
    p.add_argument("--source", default="slack")
    p.add_argument("--group")

    p = sub.add_parser("import", help="bulk-add a JSON array (file or stdin)")
    p.add_argument("--file", help="read JSON from this path instead of stdin")

    p = sub.add_parser("list", help="list bugs")
    p.add_argument("--status", choices=["open", "resolved", "all"],
                   default="all")
    p.add_argument("--channel")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--group")
    g.add_argument("--ungrouped", action="store_true")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--full", action="store_true", help="don't truncate text")

    p = sub.add_parser("group", help="label bugs into a similarity group")
    p.add_argument("--label", required=True)
    p.add_argument("ids", nargs="+", type=int)

    p = sub.add_parser("ungroup", help="clear the group label on bugs")
    p.add_argument("ids", nargs="+", type=int)

    p = sub.add_parser("groups", help="summarize similarity groups")
    p.add_argument("--json", action="store_true")

    for name, helptext in (("resolve", "mark bugs resolved"),
                           ("reopen", "mark bugs open again")):
        p = sub.add_parser(name, help=helptext)
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("ids", nargs="*", type=int, default=[])
        g.add_argument("--group")
        g.add_argument("--all", action="store_true")

    p = sub.add_parser("remove", help="delete bugs from the store")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("ids", nargs="*", type=int, default=[])
    g.add_argument("--resolved", action="store_true",
                   help="purge every resolved bug")
    g.add_argument("--all", action="store_true", help="wipe everything")
    p.add_argument("--yes", action="store_true", help="confirm --all")

    p = sub.add_parser("stats", help="counts by status/channel/group")
    p.add_argument("--json", action="store_true")

    return parser


DISPATCH = {
    "add": cmd_add, "import": cmd_import, "list": cmd_list, "group": cmd_group,
    "ungroup": cmd_ungroup, "groups": cmd_groups, "resolve": cmd_resolve,
    "reopen": cmd_reopen, "remove": cmd_remove, "stats": cmd_stats,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
