#!/usr/bin/env python3
"""Live Claude usage monitor.

Reads the Claude Code OAuth token from ~/.claude/.credentials.json and polls
the usage endpoint every 30 seconds, showing session / weekly limits, when
they reset, usage over the last 15/30/60 minutes, and a burn-rate forecast:
will the current pace bust a limit before it resets, and how much to slow
down to ride it out. Stdlib only - no pip installs needed.

Samples (timestamp + utilization %, nothing sensitive) are kept in
~/.claude_usage_history.json so the windows survive a restart.

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
HISTORY_PATH = os.path.expanduser("~/.claude_usage_history.json")
HISTORY_RETENTION_HOURS = 26  # covers the 60m window with plenty of slack
WINDOWS_MIN = (15, 30, 60)

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


def load_history():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            history = json.load(f)
            return history if isinstance(history, list) else []
    except (OSError, ValueError):
        return []


def save_history(history):
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f)
    os.replace(tmp, HISTORY_PATH)


def record_sample(history, data, now_ts):
    """Append one {ts, bucket: pct, ...} sample and drop expired ones."""
    sample = {"ts": now_ts}
    for key, _ in BUCKETS:
        bucket = data.get(key)
        if bucket and bucket.get("utilization") is not None:
            sample[key] = bucket["utilization"]
    history.append(sample)
    cutoff = now_ts - HISTORY_RETENTION_HOURS * 3600
    return [s for s in history if s.get("ts", 0) >= cutoff]


def used_in_window(history, key, minutes, now_ts):
    """Percentage points consumed over the last N minutes, or None if the
    history doesn't reach back far enough yet.

    Sums positive increments between consecutive samples, so a limit reset
    mid-window (utilization dropping back to 0) doesn't go negative.
    """
    start = now_ts - minutes * 60
    points = [(s["ts"], s[key]) for s in history if key in s and s["ts"] <= now_ts]
    before = [p for p in points if p[0] <= start]
    inside = [p for p in points if p[0] > start]
    if before:
        series = [before[-1]] + inside  # anchor just before the window opens
    elif inside and inside[0][0] <= start + minutes * 6:  # within 10% of start
        series = inside
    else:
        return None
    if len(series) < 2:
        return None
    return sum(max(b[1] - a[1], 0.0) for a, b in zip(series, series[1:]))


def pace_per_hour(history, key, now_ts):
    """Current burn rate in %/hour, from the longest window with data."""
    for minutes in (60, 30, 15):
        used = used_in_window(history, key, minutes, now_ts)
        if used is not None:
            return used * 60.0 / minutes
    return None


def fmt_left(secs):
    secs = max(int(secs), 0)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def forecast(pct, pace, reset_ts, now_ts):
    """One-line verdict: does the current pace bust the limit before reset?"""
    if pace is None:
        return f"{DIM}pace: gathering data...{RESET}"
    if pace < 0.1:
        return f"{GREEN}pace ~0%/h - idle, coasting to reset{RESET}"
    hours_left = max((reset_ts - now_ts) / 3600.0, 1 / 60)
    headroom = max(100.0 - pct, 0.0)
    at_reset = pct + pace * hours_left
    if at_reset <= 100.0:
        return (f"{GREEN}on pace{RESET} {DIM}{pace:.1f}%/h -> "
                f"~{at_reset:.0f}% at reset{RESET}")
    bust_secs = headroom / pace * 3600.0
    bust_at = datetime.fromtimestamp(now_ts + bust_secs).astimezone()
    early = (reset_ts - now_ts) - bust_secs
    sustainable = headroom / hours_left
    cut = (1.0 - sustainable / pace) * 100.0
    return (f"{RED}{BOLD}LIMIT BUST ~{bust_at:%H:%M}{RESET} "
            f"{RED}({fmt_left(early)} before reset) - "
            f"slow to <={sustainable:.1f}%/h (cut {cut:.0f}%){RESET}")


def windows_line(history, key, now_ts):
    """'last 15m +1.0%  30m +2.5%  60m -' usage summary for one bucket."""
    parts = []
    for minutes in WINDOWS_MIN:
        used = used_in_window(history, key, minutes, now_ts)
        parts.append(f"{minutes}m {'+' + format(used, '.1f') + '%' if used is not None else '-'}")
    return f"{DIM}last {'  '.join(parts)}{RESET}"


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
    return f"{when:%a %d %b %H:%M}  {DIM}(resets in {fmt_left(delta.total_seconds())}){RESET}"


def render(data, history=None, now_ts=None):
    lines = [f"{BOLD}Claude usage{RESET}  {DIM}updated {datetime.now():%H:%M:%S}{RESET}", ""]
    for key, label in BUCKETS:
        bucket = data.get(key)
        if not bucket:
            continue
        pct = bucket.get("utilization") or 0.0
        lines.append(f"  {label:<14} {bar(pct)} {colour_for(pct)}{pct:5.1f}%{RESET}")
        lines.append(f"  {'':<14} {fmt_reset(bucket['resets_at'])}")
        if history is not None and now_ts is not None:
            lines.append(f"  {'':<14} {windows_line(history, key, now_ts)}")
            reset_ts = datetime.fromisoformat(bucket["resets_at"]).timestamp()
            pace = pace_per_hour(history, key, now_ts)
            lines.append(f"  {'':<14} {forecast(pct, pace, reset_ts, now_ts)}")
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
    history = load_history()

    while True:
        try:
            # re-read each poll: Claude Code rotates this token when it refreshes
            data = fetch_usage(read_token())
            now_ts = time.time()
            history = record_sample(history, data, now_ts)
            save_history(history)
            sys.stdout.write("\x1b[2J\x1b[H" + render(data, history, now_ts) + "\n")
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
