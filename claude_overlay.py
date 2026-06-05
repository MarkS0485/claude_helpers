#!/usr/bin/env python3
"""Neon Claude usage overlay for the Windows taskbar (or any screen edge).

A strip a few pixels thick pinned to a screen edge - by default the bottom,
sitting just above the taskbar - always on top, split into three equal
segments:

    Session (5h)        |  Weekly (all)        |  Weekly Opus

Each segment is a full-brightness RGB gradient - neon green at 0% sweeping
through yellow and orange into pure red at 100% - revealed along the strip as
the limit is used, so the colour at the tip of the bar reads as how close to
the limit you are. Any segment at >=90% pulses. Hover for the exact numbers
and reset time; right-click to refresh or quit. Uses the same endpoint and
credentials as claude_usage.py - stdlib only, no pip installs.

--edge top|bottom|left|right pins it to any display edge, Samsung-Edge
style. Side strips stack the segments bottom-up and their bars fill
upwards; the top strip is the bottom one mirrored (bottom-left segment
lands top-right, the middle stays the middle).

The launched process is a tiny supervisor: the actual overlay runs as a
child that gets restarted automatically (with backoff) if it ever dies.
Right-click -> Quit stops both. Crashes and callback errors are appended to
~/.claude_overlay.log so failures leave a trail.

Usage:  python claude_overlay.py [--interval SECONDS] [--thickness PIXELS]
                                 [--edge bottom|top|left|right]
        (use pythonw instead of python to run without a console window)
"""

import argparse
import colorsys
import ctypes
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
from datetime import datetime, timezone

try:
    import tkinter as tk
except ImportError:  # headless box / CI - pure helpers stay importable
    tk = None

from claude_usage import BUCKETS, fetch_usage, fmt_left, read_token

SEGMENTS = 3
GAP = 2               # separator between segments, px
BG = "#000000"        # separator / empty strip
TRACK = "#16161d"     # unfilled part of a segment
GREEN_HUE = 120.0 / 360.0  # gradient start; sweeps down to red at 0
GRADIENT_STEPS = 128  # colour bands per segment - smooth at any width
ALERT_AT = 90.0       # >=90% pulses
PULSE_DIM = 0.55      # alert pulse alternates full and this brightness
STALE_BRIGHTNESS = 1.0 / 3.0  # missed-polls look
STALE_POLLS = 3       # missed polls before the colours dim

EDGES = ("bottom", "top", "left", "right")

LOG_PATH = os.path.expanduser("~/.claude_overlay.log")
LOG_MAX_BYTES = 256 * 1024  # rotate to .1 beyond this
RESTART_DELAY = 2     # seconds before the first respawn...
RESTART_DELAY_MAX = 60  # ...doubling up to this cap
HEALTHY_SECS = 600    # uptime that earns the backoff a reset


def log_line(text):
    """Append a timestamped line to the crash log. Never raises - logging
    must not be the thing that takes the overlay down."""
    try:
        if (os.path.exists(LOG_PATH)
                and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES):
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("{:%Y-%m-%d %H:%M:%S} {}\n".format(datetime.now(), text))
    except OSError:
        pass


# --- pure helpers (unit tested, no GUI) -----------------------------------

def pick_buckets(data):
    """First three buckets present and non-null, in BUCKETS order."""
    out = []
    for key, label in BUCKETS:
        bucket = (data or {}).get(key)
        if bucket and bucket.get("utilization") is not None:
            out.append((key, label, bucket))
    return out[:SEGMENTS]


def segment_bounds(span, count=SEGMENTS, gap=GAP):
    """[(a0, a1), ...] dividing `span` px into `count` equal slices with a
    `gap` px separator drawn out of the leading edge of slices 1..n."""
    edges = [round(i * span / count) for i in range(count + 1)]
    return [(edges[i] + (gap if i else 0), edges[i + 1]) for i in range(count)]


def fill_width(pct, seg_width):
    return round(seg_width * min(max(pct, 0.0), 100.0) / 100.0)


def gradient_colour(frac, brightness=1.0):
    """Colour at `frac` (0..1) along the neon green -> yellow -> orange ->
    red sweep: a full-saturation HSV hue walk from 120 degrees down to 0."""
    frac = min(max(frac, 0.0), 1.0)
    hue = (1.0 - frac) * GREEN_HUE
    r, g, b = colorsys.hsv_to_rgb(hue, 1.0, min(max(brightness, 0.0), 1.0))
    return "#{:02x}{:02x}{:02x}".format(
        round(r * 255), round(g * 255), round(b * 255))


def gradient_runs(seg_width, fill, brightness=1.0, steps=GRADIENT_STEPS):
    """[(a0, a1, colour), ...] painting the first `fill` px of a segment.

    The gradient always spans the whole `seg_width` - the fill just reveals
    it - so the colour at the tip of the bar is the severity readout: green
    bar barely used, tip into orange getting warm, red bar nearly bust.
    Adjacent same-colour pixels are merged into runs (at most `steps` of
    them) to keep the canvas item count down.
    """
    runs = []
    if seg_width <= 0 or fill <= 0:
        return runs
    steps = min(steps, seg_width)
    edges = [round(i * seg_width / steps) for i in range(steps + 1)]
    for i in range(steps):
        if edges[i] >= fill:
            break
        a0, a1 = edges[i], min(edges[i + 1], fill)
        frac = (a0 + a1) / 2.0 / seg_width
        runs.append((a0, a1, gradient_colour(frac, brightness)))
    return runs


def axis_to_canvas(edge, span, a0, a1):
    """Map an interval on the strip's fill axis to canvas coordinates.

    The fill axis runs the way the bars grow: left->right along the bottom
    edge; right->left along the top (the bottom strip mirrored, so the
    bottom-left segment sits top-right and the middle stays the middle);
    bottom->top on the sides, so side bars fill upwards. Canvas x grows
    right and y grows down, hence the flip for everything but `bottom`.
    """
    if edge == "bottom":
        return a0, a1
    return span - a1, span - a0


def geometry_for(edge, area, thickness):
    """(width, height, x, y) pinning a `thickness` px strip to one edge of
    the (left, top, right, bottom) work area."""
    left, top, right, bottom = area
    if edge == "bottom":
        return right - left, thickness, left, bottom - thickness
    if edge == "top":
        return right - left, thickness, left, top
    if edge == "left":
        return thickness, bottom - top, left, top
    if edge == "right":
        return thickness, bottom - top, right - thickness, top
    raise ValueError("unknown edge: {!r}".format(edge))


# --- Windows plumbing ------------------------------------------------------

class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


SPI_GETWORKAREA = 0x0030


def make_dpi_aware():
    """Match Windows' physical pixels so the strip lands exactly on the
    taskbar edge on scaled (125% / 150%) displays."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def work_area(root):
    """Primary monitor minus the taskbar -> (left, top, right, bottom)."""
    if sys.platform == "win32":
        rect = _Rect()
        ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


# --- the overlay -----------------------------------------------------------

class Overlay:
    def __init__(self, root, interval, thickness, edge):
        self.root = root
        self.interval = interval
        self.thickness = thickness
        self.edge = edge
        self.horizontal = edge in ("top", "bottom")
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.data = None
        self.fetched_ts = 0.0
        self.status = None
        self.geometry = None
        self.span = 0   # strip length along its long axis
        self.render_key = None
        self.hits = []  # [(a0, a1, label, bucket)] for the hover tooltip

        root.overrideredirect(True)          # no border, no title bar
        root.attributes("-topmost", True)
        try:
            root.attributes("-toolwindow", True)  # keep out of Alt-Tab
        except tk.TclError:
            pass
        # callback errors otherwise vanish (pythonw has no stderr) - log them
        root.report_callback_exception = self.on_callback_error

        self.canvas = tk.Canvas(root, highlightthickness=0, bd=0, bg=BG)
        self.canvas.pack(fill="both", expand=True)

        self.tip = None
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", lambda e: self.hide_tip())
        menu = tk.Menu(root, tearoff=0)
        menu.add_command(label="Refresh now", command=self.wake.set)
        menu.add_separator()
        menu.add_command(label="Quit overlay", command=root.destroy)
        self.canvas.bind("<Button-3>",
                         lambda e: menu.tk_popup(e.x_root, e.y_root))

        threading.Thread(target=self.poll_loop, daemon=True).start()
        self.place()
        self.tick()

    def on_callback_error(self, exc_type, exc, tb):
        log_line("callback error: " + "".join(
            traceback.format_exception(exc_type, exc, tb)).strip())

    # -- polling (background thread) --

    def poll_loop(self):
        while True:
            status = None
            try:
                # re-read each poll: Claude Code rotates the token
                data = fetch_usage(read_token())
                with self.lock:
                    self.data = data
                    self.fetched_ts = time.time()
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    status = ("token expired - open Claude Code once "
                              "to refresh it")
            except FileNotFoundError:
                status = "no credentials - is Claude Code logged in?"
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                pass  # network blip - the bar dims if it goes on too long
            except Exception:  # anything else must not kill the thread
                log_line("poll error: " + traceback.format_exc())
            with self.lock:
                self.status = status
            self.wake.wait(self.interval)
            self.wake.clear()

    # -- geometry / drawing (UI thread) --

    def place(self):
        """Pin to the chosen edge of the work area (so the bottom edge sits
        just above the taskbar). Re-checked every tick to follow a moved or
        resized taskbar."""
        w, h, x, y = geometry_for(self.edge, work_area(self.root),
                                  self.thickness)
        geometry = "{}x{}+{}+{}".format(w, h, x, y)
        if geometry != self.geometry:
            self.geometry = geometry
            self.span = w if self.horizontal else h
            self.root.geometry(geometry)

    def tick(self):
        """1s heartbeat. Must survive anything: if this loop dies the strip
        stops re-asserting topmost and quietly disappears behind other
        windows - which is a crash as far as the user can tell."""
        try:
            self.place()
            self.redraw()
            if self.root.state() != "normal":
                self.root.deiconify()  # something hid us - come back
            self.root.lift()
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass  # Tk mid-shutdown or in a bad state; next tick retries
        except Exception:
            log_line("tick error: " + traceback.format_exc())
        finally:
            try:
                self.root.after(1000, self.tick)
            except tk.TclError:
                pass  # window destroyed - we're quitting

    def redraw(self):
        with self.lock:
            data, fetched_ts = self.data, self.fetched_ts
        stale = bool(fetched_ts) and (
            time.time() - fetched_ts > self.interval * STALE_POLLS)
        picked = pick_buckets(data)
        pcts = tuple(b.get("utilization") or 0.0 for _, _, b in picked)
        # the 1s tick drives the alert pulse; otherwise hold the phase still
        phase = int(time.time()) % 2 if any(p >= ALERT_AT for p in pcts) else 0
        render_key = (self.span, stale, phase, pcts)
        if render_key == self.render_key:
            return  # nothing moved - skip repainting the gradient runs
        self.render_key = render_key

        self.canvas.delete("all")
        self.hits = []
        for i, (a0, a1) in enumerate(segment_bounds(self.span)):
            self.rect(a0, a1, TRACK)
            if i >= len(picked):
                continue
            _, label, bucket = picked[i]
            pct = pcts[i]
            if stale:
                brightness = STALE_BRIGHTNESS
            elif pct >= ALERT_AT and phase:
                brightness = PULSE_DIM
            else:
                brightness = 1.0
            fw = fill_width(pct, a1 - a0)
            for r0, r1, colour in gradient_runs(a1 - a0, fw, brightness):
                self.rect(a0 + r0, a0 + r1, colour)
            self.hits.append((a0, a1, label, bucket))

    def rect(self, a0, a1, colour):
        """Filled rectangle spanning [a0, a1) on the strip's fill axis."""
        c0, c1 = axis_to_canvas(self.edge, self.span, a0, a1)
        if self.horizontal:
            self.canvas.create_rectangle(c0, 0, c1, self.thickness,
                                         fill=colour, width=0)
        else:
            self.canvas.create_rectangle(0, c0, self.thickness, c1,
                                         fill=colour, width=0)

    # -- hover tooltip --

    def on_motion(self, event):
        raw = event.x if self.horizontal else event.y
        info = self.tip_info(
            raw if self.edge == "bottom" else self.span - raw)
        if info:
            self.show_tip(event, *info)
        else:
            self.hide_tip()

    def tip_info(self, pos):
        """(text, colour) for the segment under `pos` - text in the same
        green->red severity colour as the tip of that segment's bar."""
        with self.lock:
            fetched_ts, status = self.fetched_ts, self.status
        for a0, a1, label, bucket in self.hits:
            if a0 <= pos < a1:
                pct = bucket.get("utilization") or 0.0
                when = datetime.fromisoformat(bucket["resets_at"]).astimezone()
                left = when - datetime.now(timezone.utc).astimezone()
                line = "{}  {:.1f}%  -  resets {:%a %H:%M} (in {})".format(
                    label, pct, when, fmt_left(left.total_seconds()))
                if status:
                    line += "\n" + status
                elif fetched_ts:
                    line += "\nupdated {:%H:%M:%S}".format(
                        datetime.fromtimestamp(fetched_ts))
                return line, gradient_colour(pct / 100.0)
        if not self.hits:
            return (status or "waiting for first response...",
                    gradient_colour(0.0))
        return None

    def show_tip(self, event, text, fg):
        if self.tip is None:
            self.tip = tk.Toplevel(self.root)
            self.tip.overrideredirect(True)
            self.tip.attributes("-topmost", True)
            self.tip_label = tk.Label(
                self.tip, bg="#0a0a0a", font=("Consolas", 10),
                justify="left", padx=8, pady=4, bd=1, relief="solid")
            self.tip_label.pack()
        self.tip_label.config(text=text, fg=fg)
        self.tip.update_idletasks()
        w = self.tip.winfo_reqwidth()
        h = self.tip.winfo_reqheight()
        margin = self.thickness + 6
        if self.edge == "bottom":
            x, y = event.x_root - w // 2, self.root.winfo_rooty() - h - 6
        elif self.edge == "top":
            x, y = event.x_root - w // 2, self.root.winfo_rooty() + margin
        elif self.edge == "left":
            x, y = self.root.winfo_rootx() + margin, event.y_root - h // 2
        else:  # right
            x, y = self.root.winfo_rootx() - w - 6, event.y_root - h // 2
        x = min(max(x, 0), max(self.root.winfo_screenwidth() - w, 0))
        y = min(max(y, 0), max(self.root.winfo_screenheight() - h, 0))
        self.tip.geometry("+{}+{}".format(x, y))
        self.tip.deiconify()

    def hide_tip(self):
        if self.tip is not None:
            self.tip.withdraw()


# --- supervisor ------------------------------------------------------------

def supervise():
    """Run the actual overlay as a child process and bring it back if it
    dies. A clean exit (right-click -> Quit) stops the supervisor too; any
    other exit is logged and the child is relaunched with exponential
    backoff, reset after a healthy stretch of uptime."""
    delay = RESTART_DELAY
    cmd = [sys.executable, os.path.abspath(__file__)] + sys.argv[1:] + ["--child"]
    while True:
        started = time.time()
        try:
            code = subprocess.call(cmd)
        except KeyboardInterrupt:
            return 0
        if code == 0:
            return 0  # user quit
        if time.time() - started >= HEALTHY_SECS:
            delay = RESTART_DELAY
        log_line("overlay exited code {} after {:.0f}s - restart in {}s"
                 .format(code, time.time() - started, delay))
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return 0
        delay = min(delay * 2, RESTART_DELAY_MAX)


def main():
    parser = argparse.ArgumentParser(
        description="Neon Claude usage overlay pinned to a screen edge")
    parser.add_argument("--interval", type=int, default=60,
                        help="refresh seconds (default 60 - the API is rate "
                             "limited, be polite)")
    parser.add_argument("--thickness", type=int, default=4,
                        help="bar thickness in pixels (default 4)")
    parser.add_argument("--edge", choices=EDGES, default="bottom",
                        help="screen edge to pin to (default bottom, i.e. "
                             "just above the taskbar)")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if tk is None:
        sys.exit("tkinter is not available in this Python install")
    if not args.child:
        sys.exit(supervise())

    make_dpi_aware()
    try:
        root = tk.Tk()
        root.title("Claude usage overlay")
        Overlay(root, args.interval, max(args.thickness, 1), args.edge)
        root.mainloop()
    except Exception:
        log_line("fatal: " + traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
