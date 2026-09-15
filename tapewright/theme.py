# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""The VCR look: every color and font in the window, defined once, and the code that applies them.

A charcoal deck, keys a shade lighter, an aqua display and a red record light. ttk's native
Windows theme draws with system colors and ignores most of this, so apply() moves ttk onto
"clam", the built-in theme that honours style colors. Classic Tk widgets (Text, Listbox, Canvas,
Label, Toplevel) never read ttk styles, so they get the same colors from the option database.

tests/test_core.py checks every pair in CONTRAST, and that no other module spells out a color.
"""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

C = {
    # The deck
    "chassis": "#18181b",
    "panel": "#222227",
    "edge": "#34343c",
    "key": "#2c2c33",
    "key_hi": "#3a3a44",
    "key_down": "#1f1f24",
    "key_off": "#222226",
    "field": "#0f0f12",
    "select": "#1f5c52",
    # Words
    "text": "#ecebe6",
    "text_dim": "#a9a79f",
    "text_off": "#6c6b67",
    "link": "#7fd8ff",
    "ok": "#6ee28a",
    "warn": "#f2c14e",
    "error": "#ff7a6b",
    # The display
    "vfd": "#5ff5d9",
    "vfd_dim": "#0e2824",
    "vfd_bg": "#070f0d",
    "rec": "#ff4d3d",
    # The red strip, the help tips and the key caps
    "banner": "#b3261e",
    "banner_text": "#ffffff",
    "banner_key": "#f2d4d1",
    "tip": "#3a3120",
    "tip_text": "#f5ead2",
    "keycap": "#3b3b45",
    # The cassette
    "shell": "#0c0c0e",
    "shell_edge": "#2e2e35",
    "window": "#1d1b19",
    "window_edge": "#46423b",
    "tape": "#3b2a1e",
    "tape_edge": "#5a4431",
    "hub": "#d8d3ca",
    "hub_hole": "#141416",
    "screw": "#2b2b31",
    "paper": "#e8dcc0",
    "ink": "#2a251d",
}

# (text color, background, minimum contrast ratio). 7:1 for body text, the display and the cassette
# label; 4.5:1 for dimmed hints and tab names, status colors, links, selections and the red strip.
CONTRAST = [
    ("text", "chassis", 7), ("text", "panel", 7), ("text", "field", 7), ("text", "key", 7),
    ("text", "keycap", 7), ("text", "select", 4.5), ("text_dim", "chassis", 4.5),
    ("text_dim", "key", 4.5), ("link", "panel", 4.5), ("ok", "chassis", 4.5),
    ("warn", "chassis", 4.5), ("error", "chassis", 4.5), ("vfd", "vfd_bg", 7),
    ("vfd", "chassis", 7), ("rec", "vfd_bg", 4.5), ("banner_text", "banner", 4.5),
    ("banner", "banner_key", 4.5), ("tip_text", "tip", 7), ("ink", "paper", 7),
    ("vfd", "panel", 7), ("link", "tip", 4.5),
]

# Filled by apply(). Fonts need a Tk root, so nothing can use them before the window exists.
FONTS = {}


def luminance(color):
    """WCAG relative luminance of a '#rrggbb' color."""
    channels = []
    for i in (1, 3, 5):
        value = int(color[i:i + 2], 16) / 255
        channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(foreground, background):
    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _pick(available, *families):
    return next((family for family in families if family in available), families[-1])


def apply(root):
    """Style every widget created after this call, so call it before the first one exists."""
    available = set(tkfont.families(root))
    ui = _pick(available, "Segoe UI", "Helvetica")
    display = _pick(available, "Bahnschrift SemiCondensed", "Bahnschrift", ui)
    mono = _pick(available, "Consolas", "Lucida Console", "Courier New", "Courier")
    hand = _pick(available, "Ink Free", "Segoe Print", ui)
    FONTS.update(
        ui=tkfont.Font(root, family=ui, size=9),
        display=tkfont.Font(root, family=display, size=11),
        mono=tkfont.Font(root, family=mono, size=9),
        osd=tkfont.Font(root, family=mono, size=13, weight="bold"),
        small=tkfont.Font(root, family=mono, size=9, weight="bold"),
        hand=tkfont.Font(root, family=hand, size=11, weight="bold"),
        # The setup screen is read by someone who has never seen the app, so it is bigger than the tabs.
        heading=tkfont.Font(root, family=display, size=18),
        body=tkfont.Font(root, family=ui, size=11),
    )

    c = C
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=c["chassis"], foreground=c["text"], bordercolor=c["edge"],
                    lightcolor=c["key_hi"], darkcolor=c["key_down"], troughcolor=c["field"],
                    fieldbackground=c["field"], selectbackground=c["select"],
                    selectforeground=c["text"], insertcolor=c["vfd"], arrowcolor=c["text"],
                    font=FONTS["ui"])
    style.map(".", background=[("disabled", c["chassis"]), ("active", c["chassis"])],
              foreground=[("disabled", c["text_off"])],
              selectbackground=[("!focus", c["select"])], selectforeground=[("!focus", c["text"])])
    style.configure("TFrame", background=c["chassis"])
    style.configure("TLabel", background=c["chassis"], foreground=c["text"])

    style.configure("TButton", background=c["key"], foreground=c["text"], bordercolor=c["edge"],
                    lightcolor=c["key_hi"], darkcolor=c["key_down"], padding=(10, 4),
                    font=FONTS["display"])
    style.map("TButton", background=[("disabled", c["key_off"]), ("pressed", c["key_down"]),
                                     ("active", c["key_hi"])],
              foreground=[("disabled", c["text_off"])], lightcolor=[("pressed", c["key_down"])],
              darkcolor=[("pressed", c["key_down"])], bordercolor=[("alternate", c["vfd"])])

    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, fieldbackground=c["field"], foreground=c["text"], background=c["key"],
                        bordercolor=c["edge"], lightcolor=c["field"], insertcolor=c["vfd"],
                        selectbackground=c["select"], selectforeground=c["text"],
                        arrowcolor=c["text"])
        style.map(name, fieldbackground=[("disabled", c["key_off"]), ("readonly", c["field"])],
                  foreground=[("disabled", c["text_off"]), ("readonly", "focus", c["text"])],
                  bordercolor=[("focus", c["vfd"])], lightcolor=[("focus", c["field"])],
                  background=[("active", c["key_hi"]), ("pressed", c["key_hi"])],
                  arrowcolor=[("disabled", c["text_off"])])

    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=c["chassis"], foreground=c["text"],
                        indicatorbackground=c["field"], indicatorforeground=c["vfd"],
                        upperbordercolor=c["edge"], lowerbordercolor=c["edge"])
        style.map(name, indicatorbackground=[("pressed", c["key_hi"]), ("disabled", c["key_off"])],
                  foreground=[("disabled", c["text_off"])], background=[("active", c["chassis"])])

    style.configure("TNotebook", background=c["chassis"], bordercolor=c["edge"],
                    lightcolor=c["edge"], darkcolor=c["edge"], tabmargins=(6, 6, 6, 0))
    style.configure("TNotebook.Tab", background=c["key"], foreground=c["text_dim"],
                    bordercolor=c["edge"], lightcolor=c["key"], darkcolor=c["key"],
                    padding=(16, 5), font=FONTS["display"])
    style.map("TNotebook.Tab", background=[("selected", c["chassis"])],
              foreground=[("selected", c["vfd"])], lightcolor=[("selected", c["chassis"])],
              padding=[("selected", (16, 7, 16, 5))])

    style.configure("TLabelframe", background=c["chassis"], bordercolor=c["edge"],
                    lightcolor=c["edge"], darkcolor=c["edge"])
    style.configure("TLabelframe.Label", background=c["chassis"], foreground=c["vfd"],
                    font=FONTS["display"])
    style.configure("TScrollbar", background=c["key"], troughcolor=c["field"], bordercolor=c["edge"],
                    lightcolor=c["key_hi"], darkcolor=c["key_down"], arrowcolor=c["text_dim"])
    style.map("TScrollbar", background=[("pressed", c["key_hi"]), ("active", c["key_hi"])])
    # The display's aqua filling a dark well. clam draws the bar in -background and the well in -troughcolor.
    style.configure("TProgressbar", background=c["vfd"], troughcolor=c["field"], bordercolor=c["edge"])

    # Patterns name classic widget classes. A bare "*Background" would also reach ttk widgets,
    # which read it as a widget option that overrides their style's state maps.
    for pattern, value in (
        ("*Toplevel.background", c["chassis"]),
        ("*Frame.background", c["chassis"]),
        ("*Label.background", c["chassis"]),
        ("*Label.foreground", c["text"]),
        ("*Canvas.background", c["chassis"]),
        ("*Canvas.highlightThickness", 0),
        ("*Text.background", c["field"]),
        ("*Text.foreground", c["text"]),
        ("*Text.insertBackground", c["vfd"]),
        ("*Text.selectBackground", c["select"]),
        ("*Text.selectForeground", c["text"]),
        ("*Text.highlightThickness", 0),
        ("*Listbox.background", c["field"]),
        ("*Listbox.foreground", c["text"]),
        ("*Listbox.selectBackground", c["select"]),
        ("*Listbox.selectForeground", c["text"]),
        ("*Listbox.highlightThickness", 0),
        ("*TCombobox*Listbox.font", str(FONTS["ui"])),
        ("*Menu.background", c["panel"]),
        ("*Menu.foreground", c["text"]),
        ("*Menu.activeBackground", c["select"]),
        ("*Menu.activeForeground", c["text"]),
    ):
        root.option_add(pattern, value)
    root.configure(background=c["chassis"])
    root._cassette_icon = cassette_icon(root)  # Tk drops an image nothing in Python refers to
    root.iconphoto(True, root._cassette_icon)


def cassette_icon(root):
    """A 48-pixel cassette drawn pixel by pixel for the title bar and taskbar, so no image file ships."""
    image = tk.PhotoImage(master=root, width=48, height=48)

    def box(x1, y1, x2, y2, color):
        image.put(color, to=(x1, y1, x2, y2))

    box(2, 10, 46, 38, C["shell_edge"])
    box(3, 11, 45, 37, C["shell"])
    box(7, 13, 41, 20, C["paper"])
    box(9, 22, 39, 34, C["window"])
    for center in (17, 31):
        for y in range(22, 34):
            for x in range(center - 6, center + 7):
                # Squared distance from the reel's middle, so the limits are radii of 1.1, 2.4 and 5.5 px.
                squared = (x + 0.5 - center) ** 2 + (y + 0.5 - 28) ** 2
                if squared <= 1.2:
                    box(x, y, x + 1, y + 1, C["hub_hole"])
                elif squared <= 6:
                    box(x, y, x + 1, y + 1, C["hub"])
                elif squared <= 30:
                    box(x, y, x + 1, y + 1, C["tape"])
    return image
