# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Help tab: plain-language questions on the left, the answer on the right."""

import os
import re
import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from tkinter import ttk

from tapewright import deps, help_content, theme

# Past 16 pt the question list, which cannot wrap, leaves the answer a column a few words wide
# in the default window.
MIN_SIZE, MAX_SIZE = 9, 16
_MARKUP = re.compile(r"(\[[^\[\]]+\]|<[^<>]+>)")
# Where "A helper won't install" sends people to get a helper by hand.
FFMPEG_PAGE = "https://www.gyan.dev/ffmpeg/builds/"
DENO_PAGE = "https://github.com/denoland/deno/releases/latest"
SETUP_REFUSED = "Setup can't open while a conversion is running. Try again when it has finished."


class HelpTab(ttk.Frame):
    # Action key -> method name. tests/test_core.py checks it matches help_content.ACTION_LABELS.
    HANDLERS = {
        "mp3": "go_mp3",
        "mp4": "go_mp4",
        "settings": "go_settings",
        "mp3_folder": "open_mp3_folder",
        "mp4_folder": "open_mp4_folder",
        "check_updates": "check_updates",
        "diagnostics": "copy_diagnostics",
        "setup": "run_setup",
        "tools_folder": "open_tools_folder",
        "ffmpeg_page": "open_ffmpeg_page",
        "deno_page": "open_deno_page",
        "release_page": "open_release_page",
    }

    def __init__(self, master, app):
        super().__init__(master, padding=12)
        self.app = app
        size = min(max(app.settings["help_text_size"], MIN_SIZE), MAX_SIZE)
        family = theme.FONTS["ui"].cget("family")  # the Help text resizes, so it needs its own Fonts
        self.fonts = {
            "body": tkfont.Font(self, family=family, size=size),
            "bold": tkfont.Font(self, family=family, size=size, weight="bold"),
            "title": tkfont.Font(self, family=family, size=size + 5, weight="bold"),
        }
        # This tab's buttons and labels grow with the help text, so they stay as easy to read and
        # to hit. A style that holds a named font follows that font when its size changes.
        style = ttk.Style(self)
        style.configure("Help.TButton", font=self.fonts["body"], padding=(10, 4))
        style.configure("Help.TLabel", font=self.fonts["body"])
        self.message = tk.StringVar()
        self._build()
        self.topics.selection_set(0)
        self.show(0)

    # ------------------------------------------------------------ layout

    def _build(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(self, text="Click a question:", font=self.fonts["bold"]).grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        # The list cannot wrap, which is why tests/test_core.py keeps titles to 30 characters.
        self.topics = tk.Listbox(self, font=self.fonts["body"], width=30, activestyle="none",
                                 exportselection=False, relief="solid", borderwidth=1,
                                 highlightthickness=0, selectbackground=theme.C["select"],
                                 selectforeground=theme.C["text"])
        for topic in help_content.TOPICS:
            self.topics.insert("end", " " + topic["title"])
        self.topics.grid(row=1, column=0, sticky="ns", padx=(0, 12))
        self.topics.bind("<<ListboxSelect>>", self._on_select)

        answer = ttk.Frame(self)
        answer.grid(row=0, column=1, rowspan=2, sticky="nsew")
        answer.columnconfigure(0, weight=1)
        answer.rowconfigure(0, weight=1)
        self.text = tk.Text(answer, wrap="word", font=self.fonts["body"], relief="solid", borderwidth=1,
                            padx=18, pady=14, cursor="arrow", takefocus=False,
                            background=theme.C["panel"], foreground=theme.C["text"])
        scroll = ttk.Scrollbar(answer, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.actions = ttk.Frame(answer)
        self.actions.grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Label(answer, textvariable=self.message, foreground=theme.C["ok"], style="Help.TLabel").grid(
            row=2, column=0, sticky="w", pady=(4, 0))

        size_row = ttk.Frame(self)
        size_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(size_row, text="Text size:", style="Help.TLabel").pack(side="left")
        for text, step in (("Smaller", -1), ("Bigger", 1)):
            ttk.Button(size_row, text=text, style="Help.TButton",
                       command=lambda step=step: self.resize(step)).pack(side="left", padx=(8, 0))
        self._style()

    def _style(self):
        """Tag settings that depend on the text size, so they are redone after a resize."""
        body = self.fonts["body"]
        indent = 6 + body.measure("00.") + 2 * body.measure(" ")
        t = self.text
        c = theme.C
        t.tag_configure("title", font=self.fonts["title"], foreground=c["vfd"], spacing3=12)
        t.tag_configure("para", spacing3=10)
        t.tag_configure("item", lmargin1=6, lmargin2=indent, tabs=(indent,), spacing3=8)
        t.tag_configure("tip", background=c["tip"], foreground=c["tip_text"], lmargin1=6, lmargin2=6,
                        rmargin=6, spacing1=4, spacing3=12)
        # Created last, so these win over the block tags above when both apply.
        t.tag_configure("strong", font=self.fonts["bold"])
        t.tag_configure("ui", font=self.fonts["bold"], foreground=c["link"])
        t.tag_configure("key", font=self.fonts["bold"], background=c["keycap"], foreground=c["text"])

    # ------------------------------------------------------------ showing an answer

    def _on_select(self, _event):
        chosen = self.topics.curselection()
        if chosen:
            self.show(chosen[0])

    def show_topic(self, title):
        """Select and show the topic with this title, as a click on it in the list would."""
        index = next(i for i, topic in enumerate(help_content.TOPICS) if topic["title"] == title)
        self.topics.selection_clear(0, "end")
        self.topics.selection_set(index)
        self.topics.see(index)
        self.show(index)

    def show(self, index):
        topic = help_content.TOPICS[index]
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", topic["title"] + "\n", ("title",))
        for kind, value in topic["blocks"]:
            if kind == "p":
                self._insert(value, ("para",))
            elif kind == "tip":
                t.insert("end", "Tip:  ", ("tip", "strong"))
                self._insert(value, ("tip",))
            else:
                for number, item in enumerate(value, 1):
                    marker = f"{number}." if kind == "steps" else "•"
                    t.insert("end", marker + "\t", ("item", "strong"))
                    self._insert(item, ("item",))
        t.configure(state="disabled")
        t.yview_moveto(0)
        for child in self.actions.winfo_children():
            child.destroy()
        # One button per line: side by side, the second ran off the edge once the text was bigger.
        for key in topic["actions"]:
            ttk.Button(self.actions, text=help_content.ACTION_LABELS[key], style="Help.TButton",
                       command=getattr(self, self.HANDLERS[key])).pack(anchor="w", pady=(0, 6))
        self.message.set("")

    def _insert(self, text, tags):
        for part in _MARKUP.split(text):
            if part.startswith("[") and part.endswith("]"):
                self.text.insert("end", part[1:-1], tags + ("ui",))
            elif part.startswith("<") and part.endswith(">"):
                self.text.insert("end", f" {part[1:-1]} ", tags + ("key",))
            elif part:
                self.text.insert("end", part, tags)
        self.text.insert("end", "\n", tags)

    def resize(self, step):
        size = min(max(int(self.fonts["body"].cget("size")) + step, MIN_SIZE), MAX_SIZE)
        self.fonts["body"].configure(size=size)
        self.fonts["bold"].configure(size=size)
        self.fonts["title"].configure(size=size + 5)
        self._style()
        self.app.settings["help_text_size"] = size
        self.app.save_settings()

    # ------------------------------------------------------------ the buttons under an answer

    def go_mp3(self):
        self.app.notebook.select(self.app.mp3_tab)

    def go_mp4(self):
        self.app.notebook.select(self.app.mp4_tab)

    def go_settings(self):
        self.app.show_settings()

    def open_mp3_folder(self):
        self.app.mp3_tab.open_folder()

    def open_mp4_folder(self):
        self.app.mp4_tab.open_folder()

    def check_updates(self):
        self.app.show_settings()
        if not self.app.updating:  # a check mid-update would read a half-installed tool
            self.app.recheck(True)

    def copy_diagnostics(self):
        self.app.settings_tab.copy_diagnostics()
        self.message.set("Copied. Paste it into your message: hold down Ctrl and press V.")

    def run_setup(self):
        # App refuses while a conversion runs or waits to start, since the page would hide it. Said here,
        # on the message line under the button, because nothing else on screen would change.
        if not self.app.request_setup():
            self.message.set(SETUP_REFUSED)

    def open_tools_folder(self):
        """Open the folder Tapewright keeps its own helpers in, with the two folders a copy by hand goes in.

        Both are made first, because the answer tells people to open them before anything has been installed.
        """
        tools = deps.tools_dir()
        if tools is None:
            self.message.set("Tapewright only keeps helpers of its own on Windows.")
            return
        try:
            for name in ("ffmpeg", "deno"):
                (tools / name).mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(tools)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(tools)])
        except OSError as e:
            self.message.set(f"Couldn't open the helpers folder: {e}")

    def open_ffmpeg_page(self):
        webbrowser.open(FFMPEG_PAGE)

    def open_deno_page(self):
        webbrowser.open(DENO_PAGE)

    def open_release_page(self):
        webbrowser.open(deps.RELEASES_PAGE)
