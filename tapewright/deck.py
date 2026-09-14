# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""The tape deck: a cassette whose reels turn while a conversion runs, beside a VCR display.

The tape moves the way real tape does. Its length is fixed, so the tape on the two reels always
adds up to the same area, and each reel's radius is the square root of what it holds. The tape
runs at one speed, so a reel turns at that speed divided by its radius: the emptying reel spins
up and the filling one slows down.

Canvas y grows downwards, so an angle that increases turns clockwise on screen.
"""

import math
import time
import tkinter as tk

from tapewright import theme

FRAME_MS = 33
TAPE_SPEED = 2.4       # in full-reel radii per second
REWIND_SECONDS = 0.9

# mode -> (words on the display, reel speed: 0 is still, negative winds backwards)
MODES = {
    "idle": ("STOP", 0),
    "load": ("LOADING", 0.5),
    "play": ("PLAY", 1),
    "record": ("REC", 1),
    "rewind": ("REW", -3),
    "stop": ("STOP", 0),
    "error": ("ERROR", 0),
}

# Seven-segment digits. a top, b upper right, c lower right, d bottom, e lower left, f upper left,
# g middle.
SEGMENTS = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg",
    "5": "acdfg", "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg",
}


def reel_radii(fraction, hub, full):
    """(supply, take-up) radii once `fraction` of the tape has wound onto the take-up reel."""
    f = min(max(fraction, 0.0), 1.0)
    tape = full * full - hub * hub
    return math.sqrt(hub * hub + tape * (1 - f)), math.sqrt(hub * hub + tape * f)


def reel_speeds(fraction, hub, full):
    """(supply, take-up) turning speeds in radians a second. Positive is clockwise."""
    supply, take = reel_radii(fraction, hub, full)
    linear = TAPE_SPEED * full
    return linear / supply, linear / take


def counter_text(seconds):
    """H:MM:SS with a single hour digit that wraps round, as a VCR counter does."""
    s = int(max(seconds, 0))
    return f"{s // 3600 % 10}:{s // 60 % 60:02d}:{s % 60:02d}"


class TapeDeck(tk.Canvas):
    WIDTH, HEIGHT = 560, 136

    def __init__(self, master):
        super().__init__(master, width=self.WIDTH, height=self.HEIGHT, highlightthickness=0,
                         borderwidth=0, background=theme.C["chassis"])
        self.mode = "idle"
        self.shown = 0.0           # tape on the take-up reel as drawn, 0..1
        self.target = 0.0          # tape that should be there, from the job's progress
        self.angles = [0.0, 0.0]
        self.label = "TAPEWRIGHT"
        self.percent = ""
        self.started = None        # time.monotonic() when the job began, while it runs
        self.elapsed = 0.0
        self._after_rewind = None
        self._after = None
        self._last = 0.0
        self._size = None
        self._items = {}
        self._digits = None
        self.bind("<Configure>", self._on_configure)
        self.bind("<Destroy>", lambda e: self._cancel() if e.widget is self else None)

    # ------------------------------------------------------------ what the tab calls

    def load(self, label):
        """A new job. Rewind first if the last one left tape on the take-up reel."""
        self.set_label(label)
        self.target, self.percent = 0.0, ""
        self.started, self.elapsed = time.monotonic(), 0.0
        if self.shown > 0.01:
            self.mode, self._after_rewind = "rewind", "load"
        else:
            self.mode, self._after_rewind = "load", None
        self._run()

    def set_mode(self, mode):
        if mode not in MODES or self.started is None:
            return
        if self.mode == "rewind":
            self._after_rewind = mode  # play it once the tape is back at the start
        else:
            self.mode = mode
        self._run()

    def set_progress(self, fraction):
        """0..1 from the job, or None when there is no way to know.

        The tape never winds back during a job. An MP4 download fetches the video and then the
        audio, and the second restarts at zero; tape running backwards reads as starting over.
        """
        if fraction is None:
            self.percent = ""
        else:
            fraction = min(max(fraction, 0.0), 1.0)
            # A figure the tape isn't showing (the audio starting again at 0%) is left to the status
            # line under the deck, rather than printed beside a nearly full reel.
            self.percent = f"{fraction * 100:.0f}%" if fraction >= self.target else ""
            self.target = max(self.target, fraction)
        self._run()

    def set_label(self, text):
        self.label = text or "TAPEWRIGHT"
        if "label" in self._items:
            self.itemconfigure(self._items["label"], text=self._fit_label())

    def finish(self, ok, cancelled):
        if self.started is not None:
            self.elapsed = time.monotonic() - self.started
        outcome = "stop" if ok or cancelled else "error"
        self.started, self.percent = None, ""
        if ok:
            self.target, self.mode, self._after_rewind = 1.0, "stop", None
        elif self.mode == "rewind":
            # The job ended before the rewind did: finish rewinding, then show how it ended.
            self.target, self._after_rewind = 0.0, outcome
        else:
            self.target, self.mode, self._after_rewind = self.shown, outcome, None
        self._run()

    # ------------------------------------------------------------ animation

    def _run(self):
        self._draw(time.monotonic())  # show the change now, not a frame later
        if self._after is None and self._size:
            self._last = time.monotonic()
            self._after = self.after(FRAME_MS, self._tick)

    def _cancel(self):
        if self._after is not None:
            self.after_cancel(self._after)
            self._after = None

    def _tick(self):
        self._after = None
        now = time.monotonic()
        dt = min(now - self._last, 0.1)  # a late frame must not jump the tape and reels in one step
        self._last = now
        if self.mode == "rewind":
            self.shown = max(self.shown - dt / REWIND_SECONDS, 0.0)
            if self.shown == 0.0:
                self.mode, self._after_rewind = self._after_rewind or "load", None
        else:
            self.shown += (self.target - self.shown) * min(1.0, dt * 3.0)
            # Easing alone never arrives, and the loop below only stops once shown equals target.
            if abs(self.target - self.shown) < 0.0005:
                self.shown = self.target
        speed = MODES[self.mode][1]
        if speed:
            for i, radians in enumerate(reel_speeds(self.shown, self.hub, self.full)):
                self.angles[i] = (self.angles[i] + radians * speed * dt) % math.tau
        if self.started is not None:
            self.elapsed = now - self.started
        self._draw(now)
        # Only while something moves: an idle deck costs nothing.
        if speed or self.shown != self.target:
            self._after = self.after(FRAME_MS, self._tick)

    # ------------------------------------------------------------ drawing

    def _on_configure(self, event):
        size = (event.width, event.height)
        if size != self._size:
            self._size = size
            self._build(*size)
            if MODES[self.mode][1] or self.shown != self.target:
                self._run()  # draws as well
            else:
                self._draw(time.monotonic())

    def _build(self, width, height):
        c = theme.C
        self.delete("all")
        items = self._items = {}
        x0, y0 = 8, 6
        ch = height - 12
        cw = round(ch * 1.75)
        wx1, wy1, wx2, wy2 = x0 + 0.13 * cw, y0 + 0.36 * ch, x0 + 0.87 * cw, y0 + 0.86 * ch
        cy = (wy1 + wy2) / 2
        self.centers = ((x0 + 0.31 * cw, cy), (x0 + 0.69 * cw, cy))
        self.full = (self.centers[1][0] - self.centers[0][0]) * 0.47
        self.hub = self.full * 0.34

        # The reels go down first, and the shell drawn over them hides whatever is outside the
        # window, the way a real cassette only shows part of each reel.
        self.create_rectangle(wx1, wy1, wx2, wy2, fill=c["window"], outline="")
        items["packs"] = [self.create_oval(0, 0, 0, 0, fill=c["tape"], outline=c["tape_edge"])
                          for _ in range(2)]
        items["marks"] = [self.create_line(0, 0, 0, 0, fill=c["tape_edge"], width=2) for _ in range(2)]
        items["spokes"] = []
        for cx, cy in self.centers:
            h = self.hub
            self.create_oval(cx - h, cy - h, cx + h, cy + h, fill=c["hub"], outline="")
            items["spokes"].append([self.create_line(0, 0, 0, 0, fill=c["hub_hole"], width=3)
                                    for _ in range(3)])
            hole = h * 0.3
            self.create_oval(cx - hole, cy - hole, cx + hole, cy + hole, fill=c["hub_hole"], outline="")

        sx1, sy1, sx2, sy2 = x0, y0, x0 + cw, y0 + ch
        for band in ((sx1, sy1, sx2, wy1), (sx1, wy2, sx2, sy2), (sx1, wy1, wx1, wy2), (wx2, wy1, sx2, wy2)):
            self.create_rectangle(*band, fill=c["shell"], outline="")
        self.create_rectangle(sx1, sy1, sx2, sy2, outline=c["shell_edge"], width=2)
        self.create_rectangle(wx1, wy1, wx2, wy2, outline=c["window_edge"], width=2)
        for sx in (sx1 + 7, sx2 - 13):
            self.create_oval(sx, sy2 - 13, sx + 6, sy2 - 7, fill=c["screw"], outline=c["shell_edge"])
        grip_y = (wy2 + sy2) / 2
        for offset in (-4, 0, 4):
            self.create_line(x0 + 0.38 * cw, grip_y + offset, x0 + 0.62 * cw, grip_y + offset,
                             fill=c["shell_edge"], width=1)

        lx1, ly1, lx2, ly2 = x0 + 0.08 * cw, y0 + 0.08 * ch, x0 + 0.92 * cw, y0 + 0.28 * ch
        self.create_rectangle(lx1, ly1, lx2, ly2, fill=c["paper"], outline="")
        self.label_width = lx2 - lx1 - 12
        items["label"] = self.create_text((lx1 + lx2) / 2, (ly1 + ly2) / 2, text=self._fit_label(),
                                          fill=c["ink"], font=theme.FONTS["hand"])

        dx1 = sx2 + 18
        dx2 = min(width - 8, dx1 + 310)
        self.has_display = dx2 - dx1 >= 180
        if not self.has_display:
            return
        dy1, dy2 = y0 + 8, sy2 - 8
        self.create_rectangle(dx1, dy1, dx2, dy2, fill=c["vfd_bg"], outline=c["edge"], width=2)
        ix, iy = self.icon_at = (dx1 + 22, dy1 + 22)
        # The rewind's second triangle, the stop square and the record dot never move. The first
        # triangle points forwards or backwards with the mode, so _draw places it.
        items["triangles"] = [
            self.create_polygon(0, 0, 0, 0, 0, 0, fill=c["vfd"], outline=""),
            self.create_polygon(ix + 9, iy - 7, ix + 9, iy + 7, ix + 1, iy, fill=c["vfd"], outline=""),
        ]
        items["square"] = self.create_rectangle(ix - 6, iy - 6, ix + 6, iy + 6, fill=c["vfd"], outline="")
        items["dot"] = self.create_oval(ix - 7, iy - 7, ix + 7, iy + 7, fill=c["rec"], outline="")
        items["mode"] = self.create_text(dx1 + 42, dy1 + 22, text="", anchor="w", fill=c["vfd"],
                                         font=theme.FONTS["osd"])
        items["percent"] = self.create_text(dx2 - 14, dy1 + 22, text="", anchor="e", fill=c["vfd"],
                                            font=theme.FONTS["small"])
        self.create_text(dx2 - 14, dy2 - 12, text="TAPEWRIGHT", anchor="se", fill=c["vfd_dim"],
                         font=theme.FONTS["small"])
        items["digits"] = self._build_counter(dx1 + 18, dy2 - 14, 34)
        self._digits = None

    def _build_counter(self, x, bottom, height):
        """Five seven-segment digits and two colons, leaning like a VFD's. Returns per-digit items."""
        c = theme.C
        width, thick = height * 0.55, max(height * 0.12, 2.0)
        half = thick / 2
        top = bottom - height

        def lean(px, py):
            return px + (bottom - py) * 0.12, py

        def horizontal(x1, x2, y):
            return [lean(*p) for p in ((x1, y), (x1 + half, y - half), (x2 - half, y - half), (x2, y),
                                        (x2 - half, y + half), (x1 + half, y + half))]

        def vertical(xc, y1, y2):
            return [lean(*p) for p in ((xc, y1), (xc + half, y1 + half), (xc + half, y2 - half), (xc, y2),
                                        (xc - half, y2 - half), (xc - half, y1 + half))]

        digits, cursor, gap = [], x, 1.5
        for char in "0:00:00":
            if char == ":":
                for level in (0.32, 0.72):
                    py = top + height * level
                    points = [lean(*p) for p in ((cursor, py - half), (cursor + thick, py - half),
                                                 (cursor + thick, py + half), (cursor, py + half))]
                    self.create_polygon(points, fill=c["vfd"], outline="")
                cursor += thick + 8
                continue
            mid = top + height / 2
            shapes = {
                "a": horizontal(cursor + gap, cursor + width - gap, top + half),
                "g": horizontal(cursor + gap, cursor + width - gap, mid),
                "d": horizontal(cursor + gap, cursor + width - gap, bottom - half),
                "f": vertical(cursor + half, top + gap, mid - gap),
                "b": vertical(cursor + width - half, top + gap, mid - gap),
                "e": vertical(cursor + half, mid + gap, bottom - gap),
                "c": vertical(cursor + width - half, mid + gap, bottom - gap),
            }
            digits.append({name: self.create_polygon(points, fill=c["vfd_dim"], outline="")
                           for name, points in shapes.items()})
            cursor += width + 7
        return digits

    def _fit_label(self):
        font = theme.FONTS["hand"]
        text = self.label
        if font.measure(text) <= self.label_width:
            return text
        while text and font.measure(text + "…") > self.label_width:
            text = text[:-1]
        return text.rstrip() + "…"

    def _draw(self, now):
        if not self._items:
            return
        c = theme.C
        for i, radius in enumerate(reel_radii(self.shown, self.hub, self.full)):
            cx, cy = self.centers[i]
            angle = self.angles[i]
            cos, sin = math.cos(angle), math.sin(angle)
            self.coords(self._items["packs"][i], cx - radius, cy - radius, cx + radius, cy + radius)
            outer, inner = radius - 3, max(radius - 10, self.hub + 1)
            self.coords(self._items["marks"][i], cx + outer * cos, cy + outer * sin,
                        cx + inner * cos, cy + inner * sin)
            reach = self.hub * 0.82
            for k, spoke in enumerate(self._items["spokes"][i]):
                a = angle + k * math.pi / 3
                dx, dy = reach * math.cos(a), reach * math.sin(a)
                self.coords(spoke, cx - dx, cy - dy, cx + dx, cy + dy)
        if not self.has_display:
            return

        items, (ix, iy) = self._items, self.icon_at
        words, speed = MODES[self.mode]
        error = self.mode == "error"
        self.itemconfigure(items["mode"], text=words, fill=c["rec"] if error else c["vfd"])
        self.itemconfigure(items["percent"], text=self.percent if speed > 0 else "")
        first, second = items["triangles"]
        if self.mode in ("play", "load"):
            self.coords(first, ix - 7, iy - 8, ix - 7, iy + 8, ix + 7, iy)
            self.itemconfigure(first, state="normal", fill=c["vfd"] if self.mode == "play" else c["vfd_dim"])
        elif self.mode == "rewind":
            self.coords(first, ix + 1, iy - 7, ix + 1, iy + 7, ix - 7, iy)
            self.itemconfigure(first, state="normal", fill=c["vfd"])
        else:
            self.itemconfigure(first, state="hidden")
        self.itemconfigure(second, state="normal" if self.mode == "rewind" else "hidden")
        self.itemconfigure(items["square"], state="hidden" if speed else "normal",
                           fill=c["rec"] if error else c["vfd"])
        blink_on = self.mode == "record" and int(now * 2) % 2 == 0
        self.itemconfigure(items["dot"], state="normal" if blink_on else "hidden")

        digits = counter_text(self.elapsed).replace(":", "")
        if digits != self._digits:
            self._digits = digits
            for segments, char in zip(items["digits"], digits):
                lit = SEGMENTS[char]
                for name, item in segments.items():
                    self.itemconfigure(item, fill=c["vfd"] if name in lit else c["vfd_dim"])
