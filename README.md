# claude_helpers

Small utilities for working with Claude / Claude Code.

## claude_usage.py — live usage monitor

A terminal dashboard showing your Claude subscription usage — session (5-hour)
and weekly limits, with colour-coded bars, reset countdowns, recent-usage
windows, and a burn-rate forecast — refreshed every 30 seconds. No more
checking the website.

```
Claude usage  updated 14:05:14

  Session (5h)   ██████████████░░░░░░░░░░░░░░░░  46.5%
                 Fri 05 Jun 16:10  (resets in 2h 04m)
                 last 15m +6.8%  30m +13.5%  60m +27.0%
                 LIMIT BUST ~16:04 (5m before reset) - slow to <=25.7%/h (cut 5%)

  Weekly (all)   █████████████████░░░░░░░░░░░░░  55.4%
                 Sun 07 Jun 11:59  (resets in 1d 21h)
                 last 15m +0.3%  30m +0.6%  60m +1.2%
                 on pace 1.2%/h -> ~78% at reset
```

For each limit you get:

- **last 15m / 30m / 60m** — how many percentage points you actually burned
  in each window (limit resets mid-window are handled, so the numbers never
  go negative).
- **Limits bust forecast** — extrapolates your current pace to the reset
  time. If you'd cross 100% first, it tells you *when* you'd bust, how long
  before the reset, and the pace to slow to so the limit lasts exactly until
  it resets. Otherwise it shows where you'll land at reset, or that you're
  idle and coasting.

Windows need history: the script samples your utilization on every poll into
`~/.claude_usage_history.json` (timestamps and percentages only — never
tokens), so after an hour of running, all three windows are live. History
survives restarts and is trimmed after ~26 hours.

### Requirements

- Python 3.8+ (stdlib only — nothing to `pip install`)
- A logged-in [Claude Code](https://claude.com/claude-code) install
  (the script reuses the OAuth token Claude Code keeps in
  `~/.claude/.credentials.json`; no credentials are stored in this repo)

### Usage

```sh
python claude_usage.py                 # refresh every 30s
python claude_usage.py --interval 10   # refresh every 10s
```

Ctrl+C to quit. Reset times are shown in your local timezone.

### Notes

- The numbers come from the same endpoint claude.ai uses, so they match the
  website and Claude Code's `/usage` exactly — no token-count estimating.
- The credentials file is re-read on every poll, so token rotation by
  Claude Code is picked up automatically. If the token has fully expired
  (Claude Code not run for a long time), open Claude Code once and the
  monitor recovers by itself.
- The usage endpoint is not officially documented and could change shape
  in a future Claude Code release.
