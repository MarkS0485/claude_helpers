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

Machine-posted errors (forge/stack/server channels) are stored split: the
summary line becomes `text`, the full stack trace / server message is kept in
`detail`, and an error `signature` (volatile bits - ids, paths, line numbers,
timestamps - masked out) is computed so recurrences of one root cause cluster
together. `import` auto-groups by that signature by default; `regroup` does it
to bugs already in the store.

Usage:
  python claude_bugs.py add TEXT [--channel C] [--author A] [--ts T]
                                 [--permalink U] [--source S] [--group G]
                                 [--autogroup]
  python claude_bugs.py import [--file PATH] [--no-autogroup]
  python claude_bugs.py list [--status open|resolved|closed|all] [--channel C]
                             [--group G | --ungrouped] [--json] [--full]
  python claude_bugs.py group --label NAME ID [ID ...]
  python claude_bugs.py ungroup ID [ID ...]
  python claude_bugs.py groups [--json]
  python claude_bugs.py regroup [--overwrite]      # cluster by error signature
  python claude_bugs.py resolve (ID ... | --group NAME | --all)   # fix shipped
  python claude_bugs.py close   (ID ... | --group NAME | --all)   # won't-fix
  python claude_bugs.py reopen  (ID ... | --group NAME | --all)
  python claude_bugs.py remove  (ID ... | --resolved | --closed | --all [--yes])
  python claude_bugs.py stats [--json]             # open/resolved/closed + backlog
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime

DEFAULT_ROOT = os.environ.get("CLAUDE_ESTATE_ROOT", "D:\\claude")

STATUS_OPEN = "open"        # still needs fixing
STATUS_RESOLVED = "resolved"  # fix shipped
STATUS_CLOSED = "closed"    # dismissed / won't-fix / not a real bug
VALID_STATUS = (STATUS_OPEN, STATUS_RESOLVED, STATUS_CLOSED)

# Backlog heuristic: clearing ~900 bugs is reckoned at 30 days of work, i.e.
# 30 bugs/day. estimated_backlog_days(open) scales linearly off that.
BACKLOG_BUGS_PER_WINDOW = 900
BACKLOG_WINDOW_DAYS = 30

# Status emoji for human-readable output: a bug for everything still on the
# list, a big green tick once it's solved, a no-entry for dismissed.
STATUS_EMOJI = {STATUS_OPEN: "\N{BUG}",
                STATUS_RESOLVED: "\N{WHITE HEAVY CHECK MARK}",
                STATUS_CLOSED: "\N{NO ENTRY SIGN}"}

# Slack reaction names mirroring the same idea on the source messages.
SLACK_REACTION = {STATUS_OPEN: "bug",
                  STATUS_RESOLVED: "white_check_mark",
                  STATUS_CLOSED: "no_entry_sign"}


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


# ---------------------------------------------- pure: error fingerprinting ---

# Volatile bits to mask so the same root error fingerprints identically
# regardless of the coin id, path, line number, address or timestamp it carries.
_ISOTS = re.compile(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:\.\d+)?z?", re.I)
_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                   r"[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b0x[0-9a-f]+\b", re.I)
_QUOTED = re.compile(r"(['\"]).*?\1")
_PATH = re.compile(r"[a-z]:\\[^\s'\"]*|/[\w./%-]+", re.I)
_NUM = re.compile(r"\d+")


def first_line(text):
    """The first non-empty line of a message - the error summary."""
    for line in str(text).splitlines():
        if line.strip():
            return line.strip()
    return str(text).strip()


def error_signature(text):
    """A canonical fingerprint of an error's summary line, volatile parts masked.

    Same exception with a different id/path/line/timestamp collapses to the same
    string, so signature-based grouping clusters recurrences of one root cause.
    """
    s = first_line(text).lower()
    s = _ISOTS.sub(" ", s)
    s = _GUID.sub(" ", s)
    s = _HEX.sub(" ", s)
    s = _QUOTED.sub(" ", s)
    s = _PATH.sub(" ", s)
    s = _NUM.sub("#", s)                  # keep a marker so "code #" stays distinct
    s = re.sub(r"[^a-z0-9#]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def signature_label(sig):
    """A readable-but-unique group label for a signature: slug + short hash."""
    if not sig:
        return None
    tokens = [t for t in sig.replace("#", "").split() if t][:5]
    slug = "-".join(tokens)[:48] or "error"
    digest = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:4]
    return f"{slug}-{digest}"


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
    full = str(text).strip()
    summary = first_line(full)
    # keep the whole machine post (stack trace / server message) as detail when
    # it carries more than the one-line summary
    detail = pick("detail", "trace", "stack") or (full if full != summary
                                                   else None)
    return {
        "text": summary,
        "detail": detail,
        "channel": pick("channel", "chan"),
        "author": pick("author", "user", "from"),
        "ts": pick("ts", "timestamp", "time"),
        "permalink": pick("permalink", "url", "link"),
        "source": raw.get("source", "slack"),
        "status": status,
        "signature": error_signature(detail or full),
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


def import_records(data, records, autogroup=True):
    """Bulk-add a list of raw dicts. Returns (added_bugs, errors).

    When autogroup is on, any record without an explicit group is clustered by
    its error signature, so recurrences of one root cause land in one group as
    they arrive.
    """
    added, errors = [], []
    for i, raw in enumerate(records):
        try:
            bug = add_bug(data, normalize_record(raw))
            if autogroup and not bug.get("group") and bug.get("signature"):
                bug["group"] = signature_label(bug["signature"])
            added.append(bug)
        except (ValueError, AttributeError) as e:
            errors.append((i, str(e)))
    return added, errors


def regroup_by_signature(bugs, overwrite=False):
    """Assign signature-based group labels. Without overwrite, only touches
    bugs that have no group yet. Returns count relabelled.
    """
    n = 0
    for b in bugs:
        if b.get("group") and not overwrite:
            continue
        label = signature_label(b.get("signature") or error_signature(b["text"]))
        if label and b.get("group") != label:
            b["group"] = label
            n += 1
    return n


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


def estimated_backlog_days(open_count):
    """Days to clear `open_count` open bugs at 900 bugs / 30 days (30/day)."""
    return round(open_count / BACKLOG_BUGS_PER_WINDOW * BACKLOG_WINDOW_DAYS, 1)


def summarize(data):
    bugs = data["bugs"]
    by_status, by_channel, by_group = {}, {}, {}
    for b in bugs:
        st = b.get("status", STATUS_OPEN)
        by_status[st] = by_status.get(st, 0) + 1
        ch = b.get("channel") or "(none)"
        by_channel[ch] = by_channel.get(ch, 0) + 1
        g = b.get("group") or "(ungrouped)"
        by_group[g] = by_group.get(g, 0) + 1
    open_count = by_status.get(STATUS_OPEN, 0)
    return {
        "total": len(bugs),
        "open": open_count,
        "resolved": by_status.get(STATUS_RESOLVED, 0),
        "closed": by_status.get(STATUS_CLOSED, 0),
        "groups": len(by_group),
        "backlog_days": estimated_backlog_days(open_count),
        "by_status": by_status,
        "by_channel": by_channel,
        "by_group": by_group,
    }


# ------------------------------------------------------------ formatting ---

def fmt_bug(b, full=False):
    status = b.get("status", STATUS_OPEN)
    emoji = STATUS_EMOJI.get(status, STATUS_EMOJI[STATUS_OPEN])
    head = f"{emoji} #{b['id']:<4} [{status}]"
    grp = f" {{{b['group']}}}" if b.get("group") else ""
    chan = b.get("channel") or "?"
    text = b["text"] if full else (b["text"].replace("\n", " ")[:100])
    meta = f"  ({chan}"
    if b.get("author"):
        meta += f" · {b['author']}"
    meta += ")"
    out = f"{head}{grp}{meta}\n      {text}"
    if b.get("detail"):
        if full:
            indented = "\n".join("        " + ln
                                 for ln in b["detail"].splitlines())
            out += f"\n      detail:\n{indented}"
        else:
            out += "  [+detail]"
    return out


# --------------------------------------------------------------- commands ---

def cmd_add(args):
    data = load_store(args.store)
    rec = normalize_record({
        "text": args.text, "channel": args.channel, "author": args.author,
        "ts": args.ts, "permalink": args.permalink,
        "source": args.source, "group": args.group,
    })
    bug = add_bug(data, rec)
    if args.autogroup and not bug.get("group") and bug.get("signature"):
        bug["group"] = signature_label(bug["signature"])
    save_store(data, args.store)
    grp = f" (group {bug['group']})" if bug.get("group") else ""
    print(f"{STATUS_EMOJI[STATUS_OPEN]} added bug #{bug['id']}{grp}")


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
    added, errors = import_records(data, records, autogroup=not args.no_autogroup)
    save_store(data, args.store)
    line = f"imported {len(added)} bug(s)"
    if added:
        line += f" (ids #{added[0]['id']}-#{added[-1]['id']})"
        if not args.no_autogroup:
            line += f" into {len({b['group'] for b in added if b.get('group')})}" \
                    f" signature group(s)"
    print(f"{STATUS_EMOJI[STATUS_OPEN]} {line}")
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
        groups.setdefault(g, {"open": 0, "resolved": 0, "closed": 0, "ids": []})
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
        print(f"{g:<32} {STATUS_EMOJI[STATUS_OPEN]} {info['open']:>3}  "
              f"{STATUS_EMOJI[STATUS_RESOLVED]} {info['resolved']:>3}  "
              f"{STATUS_EMOJI[STATUS_CLOSED]} {info['closed']:>3}   "
              f"ids={info['ids']}")


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
    print(f"{STATUS_EMOJI[STATUS_RESOLVED]} resolved {n} bug(s).")


def cmd_close(args):
    data = load_store(args.store)
    n = set_status(_select_for_mutation(data, args), STATUS_CLOSED)
    save_store(data, args.store)
    print(f"closed {n} bug(s).")


def cmd_reopen(args):
    data = load_store(args.store)
    n = set_status(_select_for_mutation(data, args), STATUS_OPEN)
    save_store(data, args.store)
    print(f"reopened {n} bug(s).")


def cmd_regroup(args):
    data = load_store(args.store)
    n = regroup_by_signature(data["bugs"], overwrite=args.overwrite)
    save_store(data, args.store)
    print(f"regrouped {n} bug(s) by error signature.")


def cmd_remove(args):
    data = load_store(args.store)
    if args.all:
        if not args.yes:
            sys.exit("refusing to wipe every bug without --yes.")
        n = remove_bugs(data, lambda b: True)
    elif args.resolved:
        n = remove_bugs(data, lambda b: b.get("status") == STATUS_RESOLVED)
    elif args.closed:
        n = remove_bugs(data, lambda b: b.get("status") == STATUS_CLOSED)
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
    print(f"total:    {s['total']}   in {s['groups']} group(s)")
    print(f"{STATUS_EMOJI[STATUS_OPEN]} open:     {s['open']}")
    print(f"{STATUS_EMOJI[STATUS_RESOLVED]} resolved: {s['resolved']}")
    print(f"{STATUS_EMOJI[STATUS_CLOSED]} closed:   {s['closed']}")
    print(f"backlog:  ~{s['backlog_days']} days to clear "
          f"(at {BACKLOG_BUGS_PER_WINDOW} bugs / {BACKLOG_WINDOW_DAYS} days)")
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
    p.add_argument("--autogroup", action="store_true",
                   help="auto-assign a group from the error signature")

    p = sub.add_parser("import", help="bulk-add a JSON array (file or stdin)")
    p.add_argument("--file", help="read JSON from this path instead of stdin")
    p.add_argument("--no-autogroup", action="store_true",
                   help="don't cluster imports by error signature")

    p = sub.add_parser("list", help="list bugs")
    p.add_argument("--status", choices=["open", "resolved", "closed", "all"],
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

    p = sub.add_parser("regroup",
                       help="(re)cluster bugs by error signature")
    p.add_argument("--overwrite", action="store_true",
                   help="relabel bugs that already have a group too")

    for name, helptext in (("resolve", "mark bugs resolved (fix shipped)"),
                           ("close", "mark bugs closed (dismissed/won't-fix)"),
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
    g.add_argument("--closed", action="store_true",
                   help="purge every closed bug")
    g.add_argument("--all", action="store_true", help="wipe everything")
    p.add_argument("--yes", action="store_true", help="confirm --all")

    p = sub.add_parser("stats", help="counts by status/channel/group")
    p.add_argument("--json", action="store_true")

    return parser


DISPATCH = {
    "add": cmd_add, "import": cmd_import, "list": cmd_list, "group": cmd_group,
    "ungroup": cmd_ungroup, "groups": cmd_groups, "regroup": cmd_regroup,
    "resolve": cmd_resolve, "close": cmd_close, "reopen": cmd_reopen,
    "remove": cmd_remove, "stats": cmd_stats,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    DISPATCH[args.command](args)


if __name__ == "__main__":
    main()
