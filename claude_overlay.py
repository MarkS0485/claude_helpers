#!/usr/bin/env python3
"""Neon Claude usage overlay for the Windows taskbar (or any screen edges).

Strips a few pixels thick pinned to one or more screen edges - by default
the bottom, sitting just above the taskbar - always on top, each split into
three equal segments:

    Session (5h)        |  Weekly (all)        |  Weekly Opus

Each segment is a full-brightness RGB gradient - neon green at 0% sweeping
through yellow and orange into pure red at 100% - revealed along the strip as
the limit is used, so the colour at the tip of the bar reads as how close to
the limit you are. Any segment at >=90% pulses. Hover for the exact numbers
and reset time; right-click to refresh or quit. Uses the same endpoint and
credentials as claude_usage.py - stdlib only, no pip installs.

--edge picks which edges to draw, Samsung-Edge style:

    0 bottom   1 top   2 left   3 right
    4 all four   5 left+right   6 top+bottom

(the names bottom/top/left/right are also accepted). Side strips stack the
segments bottom-up and their bars fill upwards; the top strip is the bottom
one mirrored (bottom-left segment lands top-right, the middle stays the
middle). When sides and top/bottom are shown together the sides give way at
the corners.

The launched process is a tiny supervisor: the actual overlay runs as a
child that gets restarted automatically (with backoff) if it ever dies.
Right-click -> Quit stops both. Crashes and callback errors are appended to
~/.claude_overlay.log so failures leave a trail.

Usage:  python claude_overlay.py [--interval SECONDS] [--thickness PIXELS]
                                 [--edge 0-6]
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
EDGE_MODES = {
    "0": ("bottom",),
    "1": ("top",),
    "2": ("left",),
    "3": ("right",),
    "4": ("bottom", "top", "left", "right"),
    "5": ("left", "right"),
    "6": ("top", "bottom"),
}

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

def parse_edges(value):
    """'0'..'6' preset (or a bare edge name) -> tuple of edges to draw."""
    v = str(value).strip().lower()
    if v in EDGE_MODES:
        return EDGE_MODES[v]
    if v in EDGES:
        return (v,)
    raise ValueError("unknown edge mode: {!r}".format(value))


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


def parse_reset(value):
    """`resets_at` -> aware datetime, or None if missing/unparseable. The API
    occasionally hands back a null reset time alongside a live utilisation
    (notably around rate limits and timeouts), so the tooltip must never
    assume it is always a parseable string."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        return None


def freshness_line(fetched_ts, status, interval, now=None):
    """One-line freshness note for the tooltip. An explicit status (token
    expired, no credentials) wins; otherwise show when the figures were last
    fetched - flagged as cached once they are older than a refresh and a half,
    so a hover during a timeout still shows the last value with a hint that
    it's stale rather than vanishing."""
    if status:
        return status
    if not fetched_ts:
        return ""
    now = time.time() if now is None else now
    age = now - fetched_ts
    if age >= interval * 1.5:
        mins = max(1, round(age / 60.0))
        return "cached {} min ago".format(mins)
    return "updated {:%H:%M:%S}".format(datetime.fromtimestamp(fetched_ts))


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


def inset_area(area, edges, edge, thickness):
    """Work area available to `edge`'s strip: when top/bottom strips are
    also being drawn, the side strips give way at the corners so the strips
    never overlap."""
    left, top, right, bottom = area
    if edge in ("left", "right"):
        if "top" in edges:
            top += thickness
        if "bottom" in edges:
            bottom -= thickness
    return left, top, right, bottom


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


SWP_NOZORDER_NOACTIVATE = 0x0004 | 0x0010


def window_rect(hwnd):
    """Where the OS says the window actually is -> (x, y, w, h)."""
    rect = _Rect()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (rect.left, rect.top,
            rect.right - rect.left, rect.bottom - rect.top)


# --- one strip on one edge --------------------------------------------------

class Strip:
    """A single borderless always-on-top window on one screen edge."""

    def __init__(self, app, edge):
        self.app = app
        self.edge = edge
        self.horizontal = edge in ("top", "bottom")
        self._hwnd = None
        self.span = 0   # strip length along its fill axis
        self.render_key = None
        self.hits = []  # [(a0, a1, label, bucket)] for the hover tooltip
        self.tip = None

        win = tk.Toplevel(app.root)
        win.overrideredirect(True)           # no border, no title bar
        win.attributes("-topmost", True)
        try:
            win.attributes("-toolwindow", True)  # keep out of Alt-Tab
        except tk.TclError:
            pass
        self.win = win

        self.canvas = tk.Canvas(win, highlightthickness=0, bd=0, bg=BG)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", lambda e: self.hide_tip())
        self.canvas.bind("<Button-3>",
                         lambda e: app.menu.tk_popup(e.x_root, e.y_root))

    # -- geometry / drawing --

    def os_hwnd(self):
        """HWND of the real top-level window (Tk's wrapper), once mapped."""
        if self._hwnd is None and sys.platform == "win32":
            self.win.update_idletasks()
            inner = self.win.winfo_id()
            self._hwnd = ctypes.windll.user32.GetParent(inner) or inner
        return self._hwnd

    def place(self, area):
        """Pin to this strip's edge of the work area (so the bottom strip
        sits just above the taskbar). Re-checked every tick against where
        the OS says the window ACTUALLY is, not where we last put it:
        resolution changes, docking and undocking make Windows shuffle
        windows around without the computed target moving, and after such
        an external move Tk's own idea of its geometry can't be trusted -
        so both the check (GetWindowRect) and the correction
        (SetWindowPos) go straight to Win32."""
        area = inset_area(area, self.app.edges, self.edge,
                          self.app.thickness)
        w, h, x, y = geometry_for(self.edge, area, self.app.thickness)
        self.span = w if self.horizontal else h
        hwnd = self.os_hwnd()
        if hwnd:
            if window_rect(hwnd) != (x, y, w, h):
                ctypes.windll.user32.SetWindowPos(
                    hwnd, 0, x, y, w, h, SWP_NOZORDER_NOACTIVATE)
        else:  # non-Windows: Tk's view is all there is
            actual = (self.win.winfo_x(), self.win.winfo_y(),
                      self.win.winfo_width(), self.win.winfo_height())
            if actual != (x, y, w, h):
                self.win.geometry("{}x{}+{}+{}".format(w, h, x, y))

    def redraw(self, picked, pcts, stale, phase):
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
            self.canvas.create_rectangle(c0, 0, c1, self.app.thickness,
                                         fill=colour, width=0)
        else:
            self.canvas.create_rectangle(0, c0, self.app.thickness, c1,
                                         fill=colour, width=0)

    def assert_visible(self):
        if self.win.state() != "normal":
            self.win.deiconify()  # something hid us - come back
        self.win.lift()
        self.win.attributes("-topmost", True)

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
        fetched_ts, status = self.app.snapshot_status()
        for a0, a1, label, bucket in self.hits:
            if a0 <= pos < a1:
                pct = bucket.get("utilization") or 0.0
                line = "{}  {:.1f}%".format(label, pct)
                when = parse_reset(bucket.get("resets_at"))
                if when is not None:
                    left = when - datetime.now(timezone.utc).astimezone()
                    line += "  -  resets {:%a %H:%M} (in {})".format(
                        when, fmt_left(left.total_seconds()))
                note = freshness_line(fetched_ts, status, self.app.interval)
                if note:
                    line += "\n" + note
                return line, gradient_colour(pct / 100.0)
        if not self.hits:
            return (status or "waiting for first response...",
                    gradient_colour(0.0))
        return None

    def show_tip(self, event, text, fg):
        if self.tip is None:
            self.tip = tk.Toplevel(self.win)
            self.tip.overrideredirect(True)
            self.tip.attributes("-topmost", True)
            self.tip_label = tk.Label(
                self.tip, bg="#0a0a0a", font=("Consolas", 13, "bold"),
                justify="left", padx=10, pady=6, bd=1, relief="solid")
            self.tip_label.pack()
        self.tip_label.config(text=text, fg=fg)
        self.tip.update_idletasks()
        w = self.tip.winfo_reqwidth()
        h = self.tip.winfo_reqheight()
        margin = self.app.thickness + 6
        if self.edge == "bottom":
            x, y = event.x_root - w // 2, self.win.winfo_rooty() - h - 6
        elif self.edge == "top":
            x, y = event.x_root - w // 2, self.win.winfo_rooty() + margin
        elif self.edge == "left":
            x, y = self.win.winfo_rootx() + margin, event.y_root - h // 2
        else:  # right
            x, y = self.win.winfo_rootx() - w - 6, event.y_root - h // 2
        x = min(max(x, 0), max(self.win.winfo_screenwidth() - w, 0))
        y = min(max(y, 0), max(self.win.winfo_screenheight() - h, 0))
        self.tip.geometry("+{}+{}".format(x, y))
        self.tip.deiconify()

    def hide_tip(self):
        if self.tip is not None:
            self.tip.withdraw()


# --- the overlay app --------------------------------------------------------

class Overlay:
    """Shared poll loop + heartbeat driving one Strip per configured edge."""

    def __init__(self, root, interval, thickness, edges):
        self.root = root
        self.interval = interval
        self.thickness = thickness
        self.edges = edges
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.data = None
        self.fetched_ts = 0.0
        self.status = None

        root.withdraw()  # the strips are Toplevels; the root stays hidden
        # callback errors otherwise vanish (pythonw has no stderr) - log them
        root.report_callback_exception = self.on_callback_error

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="Refresh now", command=self.wake.set)
        self.menu.add_separator()
        self.menu.add_command(label="Quit overlay", command=root.destroy)

        self.strips = [Strip(self, edge) for edge in edges]

        threading.Thread(target=self.poll_loop, daemon=True).start()
        self.tick()

    def on_callback_error(self, exc_type, exc, tb):
        log_line("callback error: " + "".join(
            traceback.format_exception(exc_type, exc, tb)).strip())

    def snapshot_status(self):
        with self.lock:
            return self.fetched_ts, self.status

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
                pass  # network blip - the bars dim if it goes on too long
            except Exception:  # anything else must not kill the thread
                log_line("poll error: " + traceback.format_exc())
            with self.lock:
                self.status = status
            self.wake.wait(self.interval)
            self.wake.clear()

    # -- heartbeat (UI thread) --

    def tick(self):
        """1s heartbeat. Must survive anything: if this loop dies the strips
        stop re-asserting topmost and quietly disappear behind other
        windows - which is a crash as far as the user can tell."""
        try:
            with self.lock:
                data, fetched_ts = self.data, self.fetched_ts
            stale = bool(fetched_ts) and (
                time.time() - fetched_ts > self.interval * STALE_POLLS)
            picked = pick_buckets(data)
            pcts = tuple(b.get("utilization") or 0.0 for _, _, b in picked)
            # this tick drives the alert pulse; otherwise hold the phase
            phase = (int(time.time()) % 2
                     if any(p >= ALERT_AT for p in pcts) else 0)
            area = work_area(self.root)
            for strip in self.strips:
                strip.place(area)
                strip.redraw(picked, pcts, stale, phase)
                strip.assert_visible()
        except tk.TclError:
            pass  # Tk mid-shutdown or in a bad state; next tick retries
        except Exception:
            log_line("tick error: " + traceback.format_exc())
        finally:
            try:
                self.root.after(1000, self.tick)
            except tk.TclError:
                pass  # window destroyed - we're quitting


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


def edge_arg(value):
    try:
        return parse_edges(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "use 0=bottom 1=top 2=left 3=right 4=all 5=left+right "
            "6=top+bottom (or an edge name)")


def main():
    parser = argparse.ArgumentParser(
        description="Neon Claude usage overlay pinned to screen edges")
    parser.add_argument("--interval", type=int, default=60,
                        help="refresh seconds (default 60 - the API is rate "
                             "limited, be polite)")
    parser.add_argument("--thickness", type=int, default=4,
                        help="bar thickness in pixels (default 4)")
    parser.add_argument("--edge", type=edge_arg, default=("bottom",),
                        metavar="MODE",
                        help="edges to draw: 0=bottom (default, just above "
                             "the taskbar) 1=top 2=left 3=right 4=all "
                             "5=left+right 6=top+bottom")
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
