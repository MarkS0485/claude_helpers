# claude_helpers

Small utilities for working with Claude / Claude Code.

## claude_usage.py — live usage monitor

A terminal dashboard showing your Claude subscription usage — session (5-hour)
and weekly limits, with colour-coded bars and reset countdowns — refreshed
every 30 seconds. No more checking the website.

```
Claude usage  updated 13:51:48

  Session (5h)   ████░░░░░░░░░░░░░░░░░░░░░░░░░░  15.0%
                 Fri 05 Jun 16:10  (resets in 2h 18m)

  Weekly (all)   ████████████████░░░░░░░░░░░░░░  54.0%
                 Sun 07 Jun 12:00  (resets in 1d 22h)

  Weekly Sonnet  ████░░░░░░░░░░░░░░░░░░░░░░░░░░  12.0%
                 Sun 07 Jun 12:00  (resets in 1d 22h)
```

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
