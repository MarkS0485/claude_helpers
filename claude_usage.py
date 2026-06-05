#!/usr/bin/env python3
"""Live Claude usage monitor.

Reads the Claude Code OAuth token from ~/.claude/.credentials.json and polls
the usage endpoint every 30 seconds, showing session / weekly limits and
when they reset. Stdlib only - no pip installs needed.

Usage:  python claude_usage.py [--interval SECONDS]
Stop:   Ctrl+C
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # block chars on cp1252 consoles

CREDS_PATH = os.path.expanduser("~/.claude/.credentials.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# (json key, display label) - only shown if present/non-null in the response
BUCKETS = [
    ("five_hour", "Session (5h)"),
    ("seven_day", "Weekly (all)"),
    ("seven_day_opus", "Weekly Opus"),
    ("seven_day_sonnet", "Weekly Sonnet"),
]

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"


def read_token():
    with open(CREDS_PATH, encoding="utf-8") as f:
        return json.load(f)["claudeAiOauth"]["accessToken"]


def fetch_usage(token):
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-usage-monitor",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def colour_for(pct):
    if pct >= 80:
        return RED
    if pct >= 50:
        return YELLOW
    return GREEN


def bar(pct, width=30):
    filled = round(width * min(pct, 100) / 100)
    return f"{colour_for(pct)}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET}"


def fmt_reset(iso):
    """'2026-06-07T10:59:59+00:00' -> 'Sun 07 Jun 11:59  (in 1d 23h)'"""
    when = datetime.fromisoformat(iso).astimezone()
    delta = when - datetime.now(timezone.utc).astimezone()
    secs = max(int(delta.total_seconds()), 0)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        left = f"{d}d {h}h"
    elif h:
        left = f"{h}h {m:02d}m"
    else:
        left = f"{m}m"
    return f"{when:%a %d %b %H:%M}  {DIM}(resets in {left}){RESET}"


def render(data):
    lines = [f"{BOLD}Claude usage{RESET}  {DIM}updated {datetime.now():%H:%M:%S}{RESET}", ""]
    for key, label in BUCKETS:
        bucket = data.get(key)
        if not bucket:
            continue
        pct = bucket.get("utilization") or 0.0
        lines.append(f"  {label:<14} {bar(pct)} {colour_for(pct)}{pct:5.1f}%{RESET}")
        lines.append(f"  {'':<14} {fmt_reset(bucket['resets_at'])}")
        lines.append("")

    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled"):
        used = extra.get("used_credits") or 0.0
        limit = extra.get("monthly_limit") or 0
        cur = extra.get("currency", "")
        lines.append(f"  {DIM}Extra usage: {used:.2f} / {limit} {cur} this month{RESET}")
        lines.append("")

    lines.append(f"  {DIM}Ctrl+C to quit{RESET}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Live Claude usage monitor")
    parser.add_argument("--interval", type=int, default=30, help="refresh seconds (default 30)")
    args = parser.parse_args()

    os.system("")  # enable ANSI escape codes on Windows consoles

    while True:
        try:
            # re-read each poll: Claude Code rotates this token when it refreshes
            data = fetch_usage(read_token())
            sys.stdout.write("\x1b[2J\x1b[H" + render(data) + "\n")
            sys.stdout.flush()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("\nToken expired/invalid. Open Claude Code once (it refreshes "
                      "the token automatically), then this will recover.")
            else:
                print(f"\nHTTP {e.code} from usage endpoint - retrying in {args.interval}s")
        except FileNotFoundError:
            print(f"\nNo credentials at {CREDS_PATH} - is Claude Code logged in?")
        except (urllib.error.URLError, TimeoutError):
            print(f"\nNetwork error - retrying in {args.interval}s")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print()
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
