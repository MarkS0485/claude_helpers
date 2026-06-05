# claude_helpers

Small utilities for working with Claude / Claude Code.

## claude_config.py — configure Claude Code from the command line

Manages `~/.claude/settings.json`: permission bypass mode, environment
variables injected into every Claude Code session (GitHub PAT etc.), the git
SSH identity, and SSH host entries.

```sh
python claude_config.py show                    # current config, secrets masked
python claude_config.py bypass on               # run without permission prompts (confirms first)
python claude_config.py bypass off
python claude_config.py env GH_TOKEN            # set a session env var - value typed hidden
python claude_config.py env GH_TOKEN --from-env # ...or copied from the current environment
python claude_config.py env GH_TOKEN --delete
python claude_config.py ssh-key C:\Users\me\.ssh\id_ed25519   # git ssh identity
python claude_config.py ssh list
python claude_config.py ssh add --id ovh --host ubuntu@1.2.3.4 --key C:\Users\me\.ssh\ovh_ed25519
python claude_config.py ssh remove ovh
```

Secret handling: values are entered with hidden input (or pulled from your
environment), written **only** to your local `settings.json`, and echoed back
masked (`ghp_********`). `show` masks anything whose name looks secret
(TOKEN/KEY/PAT/SECRET/...). Every write keeps a `settings.json.bak` of the
previous version and other settings are preserved untouched. Use
`--settings PATH` to operate on a different file (e.g. a test copy).

> **Note:** `bypass on` lets Claude Code run commands and edit files without
> asking. Only enable it if you understand what that means; the command asks
> for confirmation unless you pass `--yes`.

## claude_usage.py — live usage monitor

A terminal dashboard showing your Claude subscription usage — session (5-hour)
and weekly limits, with colour-coded bars, reset countdowns, recent-usage
windows, and a burn-rate forecast — refreshed every minute. No more
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
python claude_usage.py                 # refresh every 60s
python claude_usage.py --interval 120  # refresh every 2 minutes
```

Ctrl+C to quit. Reset times are shown in your local timezone. The default
interval is a polite once-a-minute — the endpoint is rate limited, so don't
crank it down hard. Transient HTTP/network errors are silent: the last good
display stays up (the `updated` timestamp shows its age) and the next
successful poll refreshes it. Only an expired token shows a hint, since that
needs you to open Claude Code once.

### Notes

- The numbers come from the same endpoint claude.ai uses, so they match the
  website and Claude Code's `/usage` exactly — no token-count estimating.
- The credentials file is re-read on every poll, so token rotation by
  Claude Code is picked up automatically. If the token has fully expired
  (Claude Code not run for a long time), open Claude Code once and the
  monitor recovers by itself.
- The usage endpoint is not officially documented and could change shape
  in a future Claude Code release.

## claude_overlay.py — neon taskbar overlay (Windows)

The usage monitor as ambient UI: an always-on-top strip a few pixels thick
sitting just above the taskbar, spanning the full screen width in three equal
segments — **Session (5h) | Weekly (all) | Weekly Opus** — each one an
RGB-gamer-grade gradient. The bar fills left-to-right as the limit is used,
revealing a full-brightness sweep from neon green through yellow and orange
into pure red, so the colour at the tip of the bar *is* the severity readout:
green tip = relax, orange tip = getting warm, red = nearly bust. Any segment
at ≥90% pulses. If a bucket is missing from the API response (e.g. no Opus
limit on your plan), the next available one (Weekly Sonnet) takes the slot.

```sh
python  claude_overlay.py                  # refresh every 60s, 4px thick
pythonw claude_overlay.py --thickness 2    # no console window, thinner bar
python  claude_overlay.py --edge 4        # full Samsung-Edge: all four edges
```

- **Hover** a segment for the exact percentage, reset time, and last-update
  timestamp; **right-click** for refresh-now / quit.
- `--edge MODE` picks the edges: `0` bottom (default) · `1` top · `2` left ·
  `3` right · `4` all four · `5` left+right · `6` top+bottom (edge names
  also accepted). Side strips stack the segments bottom-up and fill
  upwards; the top strip is the bottom one mirrored (bottom-left segment
  lands top-right, middle stays the middle), and side strips give way to
  the top/bottom ones at the corners.
- Self-healing: the launched process is a tiny supervisor that restarts the
  overlay (with backoff) if it ever dies, and the 1s UI heartbeat survives
  internal errors instead of silently vanishing behind other windows.
  Crashes leave a trail in `~/.claude_overlay.log`.
- Follows the taskbar if you move or resize it, stays put across DPI
  scaling (125% / 150% displays), and snaps back to its edges within a
  second when docking, undocking, or resolution changes shuffle windows
  around.
- If polling fails for a few minutes the colours dim to a third brightness —
  a bright bar always means live data.
- Same endpoint, credentials, requirements, and polite once-a-minute default
  as `claude_usage.py`; the two run happily side by side.
- To start it with Windows: `Win+R` → `shell:startup` → drop in a shortcut
  running `pythonw C:\path\to\claude_overlay.py`.
