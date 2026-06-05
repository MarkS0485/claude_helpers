#!/usr/bin/env python3
"""Neon Claude usage overlay for the Windows taskbar.

A strip a few pixels thick that sits just above the taskbar, always on top,
spanning the full screen width in three equal segments:

    Session (5h)        |  Weekly (all)        |  Weekly Opus

Each segment is a full-brightness RGB gradient - neon green at 0% sweeping
through yellow and orange into pure red at 100% - revealed left-to-right as
the limit is used, so the colour at the tip of the bar reads as how close to
the limit you are. Any segment at >=90% pulses. Hover for the exact numbers
and reset time; right-click to refresh or quit. Uses the same endpoint and
credentials as claude_usage.py - stdlib only, no pip installs.

Usage:  python claude_overlay.py [--interval SECONDS] [--thickness PIXELS]
        (use pythonw instead of python to run without a console window)
"""

import argparse
import colorsys
import ctypes
import sys
import threading
import time
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


# --- pure helpers (unit tested, no GUI) -----------------------------------

def pick_buckets(data):
    """First three buckets present and non-null, in BUCKETS order."""
    out = []
    for key, label in BUCKETS:
        bucket = (data or {}).get(key)
        if bucket and bucket.get("utilization") is not None:
            out.append((key, label, bucket))
    return out[:SEGMENTS]


def segment_bounds(width, count=SEGMENTS, gap=GAP):
    """[(x0, x1), ...] dividing `width` into `count` equal slices with a
    `gap` px separator drawn out of the left edge of slices 1..n."""
    edges = [round(i * width / count) for i in range(count + 1)]
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
    """[(x0, x1, colour), ...] painting the first `fill` px of a segment.

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
        x0, x1 = edges[i], min(edges[i + 1], fill)
        frac = (x0 + x1) / 2.0 / seg_width
        runs.append((x0, x1, gradient_colour(frac, brightness)))
    return runs


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
    def __init__(self, root, interval, thickness):
        self.root = root
        self.interval = interval
        self.thickness = thickness
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.data = None
        self.fetched_ts = 0.0
        self.status = None
        self.geometry = None
        self.width = 0
        self.render_key = None
        self.hits = []  # [(x0, x1, label, bucket)] for the hover tooltip

        root.overrideredirect(True)          # no border, no title bar
        root.attributes("-topmost", True)
        try:
            root.attributes("-toolwindow", True)  # keep out of Alt-Tab
        except tk.TclError:
            pass

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
            with self.lock:
                self.status = status
            self.wake.wait(self.interval)
            self.wake.clear()

    # -- geometry / drawing (UI thread) --

    def place(self):
        """Pin to the bottom of the work area, i.e. just above the taskbar.
        Re-checked every tick so a moved/resized taskbar is followed."""
        left, _, right, bottom = work_area(self.root)
        geometry = "{}x{}+{}+{}".format(
            right - left, self.thickness, left, bottom - self.thickness)
        if geometry != self.geometry:
            self.geometry = geometry
            self.width = right - left
            self.root.geometry(geometry)

    def tick(self):
        self.place()
        self.redraw()
        self.root.lift()
        self.root.attributes("-topmost", True)  # re-assert against new windows
        self.root.after(1000, self.tick)

    def redraw(self):
        with self.lock:
            data, fetched_ts = self.data, self.fetched_ts
        stale = bool(fetched_ts) and (
            time.time() - fetched_ts > self.interval * STALE_POLLS)
        picked = pick_buckets(data)
        pcts = tuple(b.get("utilization") or 0.0 for _, _, b in picked)
        # the 1s tick drives the alert pulse; otherwise hold the phase still
        phase = int(time.time()) % 2 if any(p >= ALERT_AT for p in pcts) else 0
        render_key = (self.width, stale, phase, pcts)
        if render_key == self.render_key:
            return  # nothing moved - skip repainting the gradient runs
        self.render_key = render_key

        c = self.canvas
        c.delete("all")
        self.hits = []
        for i, (x0, x1) in enumerate(segment_bounds(self.width)):
            c.create_rectangle(x0, 0, x1, self.thickness, fill=TRACK, width=0)
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
            fw = fill_width(pct, x1 - x0)
            for rx0, rx1, colour in gradient_runs(x1 - x0, fw, brightness):
                c.create_rectangle(x0 + rx0, 0, x0 + rx1, self.thickness,
                                   fill=colour, width=0)
            self.hits.append((x0, x1, label, bucket))

    # -- hover tooltip --

    def on_motion(self, event):
        info = self.tip_info(event.x)
        if info:
            self.show_tip(event.x_root, *info)
        else:
            self.hide_tip()

    def tip_info(self, x):
        """(text, colour) for the segment under x - text in the same
        green->red severity colour as the tip of that segment's bar."""
        with self.lock:
            fetched_ts, status = self.fetched_ts, self.status
        for x0, x1, label, bucket in self.hits:
            if x0 <= x < x1:
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

    def show_tip(self, x_root, text, fg):
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
        x = min(max(x_root - w // 2, 0), max(self.width - w, 0))
        y = self.root.winfo_rooty() - h - 6
        self.tip.geometry("+{}+{}".format(x, y))
        self.tip.deiconify()

    def hide_tip(self):
        if self.tip is not None:
            self.tip.withdraw()


def main():
    parser = argparse.ArgumentParser(
        description="Neon Claude usage overlay above the taskbar")
    parser.add_argument("--interval", type=int, default=60,
                        help="refresh seconds (default 60 - the API is rate "
                             "limited, be polite)")
    parser.add_argument("--thickness", type=int, default=4,
                        help="bar height in pixels (default 4)")
    args = parser.parse_args()

    if tk is None:
        sys.exit("tkinter is not available in this Python install")
    make_dpi_aware()
    root = tk.Tk()
    root.title("Claude usage overlay")
    Overlay(root, args.interval, max(args.thickness, 1))
    root.mainloop()


if __name__ == "__main__":
    main()
