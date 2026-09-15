# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""The setup screen: one page, in place of the tabs, that installs the helpers a new copy is missing.

It installs nothing by itself. [Install them now] goes through App.run_updates like every other update, so
Cancel, the gates and the update log behave exactly as they do on the Settings tab, and the screen follows
the batch through App.update_listeners, whoever started it. The words are for someone who has never heard
of yt-dlp: each helper goes by what it does, and a download's progress is in megabytes.
"""

import tkinter as tk
from tkinter import ttk

from tapewright import deps, theme, widgets

KEYS = ("yt-dlp", "ffmpeg", "js")
# Said only when it is the number of rows listed: with the YouTube helper turned off there are two.
COUNTS = {1: "one free helper program", 2: "two free helper programs", 3: "three free helper programs"}
LOOKING = "Looking for the helper programs Tapewright uses…"
STATUS_WORDS = {
    deps.OK: "Already on this computer ✓",
    deps.MISSING: "Needs installing",
    deps.OUTDATED: "Needs an update",
    deps.UNSUPPORTED: "Too old, will be replaced",
    deps.UNKNOWN: "Couldn't check",
}
# Too old, with nothing on this screen that could replace it, such as Node.js, or a deno that only deno's own
# updater updates. Its hint says what to do instead.
TOO_OLD = "Too old"
# The hint for a helper with a problem the screen leaves alone, for the reason deps.setup_runs gives.
SETTINGS_HINT = "Can be updated from the Settings tab."
# Only settled states get a color. A helper that still needs installing isn't an error, just the next step.
STATUS_COLORS = {deps.OK: "ok", deps.UNKNOWN: "warn"}
INSTALLED = "Installed ✓"
# A failure with no sentence of its own, such as pip's. What explains it is in the update log.
DIDNT_INSTALL = "It didn't install. Click Show details to see why."
# A check's detail can be a program's whole last line of output, and a row that long is hard to read and
# scrolls the other rows out of sight. So a row shows this much of it, and the update log all of it.
DETAIL_LIMIT = 200
CUT = "… Click Show details to read the rest."
HELP_TOPIC = "A helper won't install"  # a title in help_content.TOPICS; tests/test_core.py checks it is
MB = 1024 * 1024
# The least height the details keep while they show, at 96 DPI, as the Settings tab's log has. In a short
# window the rows scroll instead, rather than the details shrinking to nothing.
DETAILS_FLOOR = 110


def megabytes(done, total):
    """'48 of 106 MB' while a download's size is known, and '48 MB' while it isn't."""
    got = done // MB  # rounded down, so it never says the whole size before the last byte
    return f"{got} of {max(round(total / MB), got)} MB" if total else f"{got} MB"


def shorten(text, limit=DETAIL_LIMIT):
    """text whole when a row can show it; otherwise its start, cut between words, and where the rest is."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + CUT


def _names(keys):
    """'The Converter and the YouTube helper', for the start of a sentence."""
    text = " and ".join(f"the {deps.ROLES[key]}" for key in keys)
    return text[:1].upper() + text[1:]


def intro_text(listed, plan, windows):
    """The words under the heading, true for the rows listed and for how this screen installs each of them.

    listed holds the keys of the rows shown, and plan maps a key to "fetch" or "pip" for each helper
    the screen installs. A helper it doesn't install gets no sentence: a copy from winget or deno's own
    updater isn't downloaded or kept by Tapewright. And pip puts yt-dlp in the Python that runs
    Tapewright, which other programs may share, so nothing here claims that nothing else on the computer
    changes.
    """
    sentences = [f"Tapewright needs {COUNTS.get(len(listed), 'some free helper programs')}."]
    fetched = [key for key in listed if plan.get(key) == "fetch"]
    if windows and fetched:
        many = len(fetched) > 1
        sentences.append(f"{_names(fetched)} {'are' if many else 'is'} downloaded from "
                         f"{'their' if many else 'its'} official releases on github.com, checked, and "
                         "kept in Tapewright's own folder.")
    pipped = [key for key in listed if plan.get(key) == "pip"]
    if pipped:
        sentences.append(f"{_names(pipped)} {'are' if len(pipped) > 1 else 'is'} added to the Python that "
                         "runs Tapewright.")
    if windows:  # pip falls back to the user's own folder, and the fetcher writes only to %LOCALAPPDATA%
        sentences.append("You don't need an administrator password.")
    return " ".join(sentences)


class SetupScreen(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=(24, 16))
        self.app = app
        self.rows = {}
        self.buttons = {}
        self.shown_buttons = ()
        self.details_shown = False
        self.running = False      # from a batch's first "start" until the check after it comes back
        self.batch_over = False   # the batch has ended, and that check hasn't come back yet
        self.cancelling = False
        self.stage = ""           # "Step 2 of 3: Installing the Converter (FFmpeg)…"
        self.current = None       # the key being installed right now
        self.activity = {}        # for that key: its latest STEP sentence, and "done" and "total" bytes
        self.installed = {}       # key -> the command that installed it in this batch without an error
        self.failures = {}        # key -> why it didn't install, until the next batch or it can't be retried
        self.notes = {}           # key -> the sentence for an install that was cancelled
        self.errors = {}          # key -> the fetcher's last ERROR sentence in this batch
        self.failed_once = set()  # keys whose install failed at any time in this session
        # key -> "fetch" or "pip": how the screen installs each helper it lists. Kept while that helper is
        # where this screen puts it, so the intro doesn't change under a setup that has just finished.
        self.plan = {}
        self._build()
        app.update_listeners.append(self.on_update)
        # On the window, not this frame: a frame never has the focus, and Escape should work from any button.
        # The wheel is caught there too, since it goes to whichever label in a row is under the pointer. So is
        # Enter, which no ttk button answers by itself: only Space presses one.
        top = self.winfo_toplevel()
        top.bind("<Escape>", self._escape, add="+")
        for sequence in ("<Return>", "<KP_Enter>"):
            top.bind(sequence, self._enter, add="+")
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            top.bind(sequence, self._wheel, add="+")

    # ------------------------------------------------------------ layout

    def _build(self):
        c = theme.C
        self.columnconfigure(0, weight=1)
        # When the window is shorter than the page, grid takes the room it lacks from weighted rows only.
        # So the helper rows (row 4) give way and scroll, never the status line or the buttons, which a
        # long failure sentence would otherwise push out of a small window at 125% scaling. Row 6 holds the
        # details while they show: weighing far more, it takes spare room first and gives it back first,
        # down to DETAILS_FLOOR. While they are hidden it is empty and holds the spare room, which keeps
        # the buttons right under the rows.
        self.rowconfigure(4, weight=1)
        self.rowconfigure(6, weight=1000)
        ttk.Label(self, text="Let's get Tapewright ready", font=theme.FONTS["heading"],
                  foreground=c["vfd"]).grid(row=0, column=0, sticky="w")
        self.intro = ttk.Label(self, font=theme.FONTS["body"], justify="left")
        self.intro.grid(row=1, column=0, sticky="ew", pady=(6, 14))
        self.status = ttk.Label(self, font=theme.FONTS["display"], justify="left")
        self.status.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        self.looking_bar = ttk.Progressbar(self, mode="indeterminate")
        self.looking_bar.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        self.rows_area = ttk.Frame(self)
        self.rows_area.grid(row=4, column=0, sticky="nsew", pady=(6, 0))
        self.rows_area.columnconfigure(0, weight=1)
        self.rows_area.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self.rows_area, height=1, borderwidth=0, highlightthickness=0,
                                yscrollincrement=20)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self.rows_area, orient="vertical", command=self.canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.scrollbar.grid_remove()  # shown only while the rows don't fit
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.table = ttk.Frame(self.canvas)
        self._table_item = self.canvas.create_window(0, 0, window=self.table, anchor="nw")
        self.table.columnconfigure(0, weight=1)
        for i, key in enumerate(KEYS):
            frame = ttk.Frame(self.table)
            frame.grid(row=i, column=0, sticky="ew", pady=(0, 14))
            frame.columnconfigure(0, weight=1)
            parts = {
                "frame": frame,
                "name": ttk.Label(frame, font=theme.FONTS["display"]),
                "about": ttk.Label(frame, text=deps.ABOUT[key], font=theme.FONTS["body"],
                                   foreground=c["text_dim"], justify="left"),
                "state": ttk.Label(frame, font=theme.FONTS["body"], justify="left"),
                "hint": ttk.Label(frame, foreground=c["text_dim"], justify="left"),
                "bar": ttk.Progressbar(frame, mode="indeterminate"),
                "amount": ttk.Label(frame, foreground=c["text_dim"]),
            }
            parts["name"].grid(row=0, column=0, columnspan=2, sticky="w")
            parts["about"].grid(row=1, column=0, columnspan=2, sticky="w")
            parts["state"].grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
            parts["hint"].grid(row=3, column=0, columnspan=2, sticky="w")
            parts["bar"].grid(row=4, column=0, sticky="ew", pady=(4, 0))
            parts["amount"].grid(row=4, column=1, sticky="e", padx=(10, 0), pady=(4, 0))
            parts["bar"].grid_remove()  # shown only on its turn
            parts["amount"].grid_remove()
            self.rows[key] = parts
        self.table.bind("<Configure>", self._fit)
        self.canvas.bind("<Configure>", self._fit)

        bar = ttk.Frame(self)
        bar.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        for name, text, command in (
            ("install", "Install them now", self.install),
            ("start", "Start using Tapewright", self.start_using),
            ("retry", "Try again", self.install),
            ("check", "Check again", self.check_again),
            ("help", "Help with this", self.help_with_this),
            ("later", "Not now", self.not_now),
            ("hide", "Hide this page", self.hide),
            ("cancel", "Cancel", self.cancel),
        ):
            self.buttons[name] = ttk.Button(bar, text=text, command=command)
        # Beside the buttons rather than one of them: watching the details is most useful while installing.
        self.details_button = ttk.Button(bar, text="Show details", command=self.toggle_details)
        self.details_button.pack(side="right")
        # Below the buttons, so opening the details never moves the button that was just clicked.
        self.logview = widgets.LogView(self, height=8)  # App feeds it every line the Settings log gets
        self.logview.grid(row=6, column=0, sticky="nsew", pady=(10, 0))
        self.logview.grid_remove()
        self.bind("<Configure>", self._rewrap)

    def _rewrap(self, event):
        width = max(event.width - 60, 300)
        for label in (self.intro, self.status):
            label.configure(wraplength=width)
        # The rows keep room for the scrollbar whether or not it shows. Otherwise showing it would rewrap
        # them, which changes their height, and with it whether the scrollbar is needed at all.
        rows = max(width - self.scrollbar.winfo_reqwidth() - 8, 300)
        for row in self.rows.values():
            for name in ("about", "state", "hint"):
                row[name].configure(wraplength=rows)

    def _fit(self, _event=None):
        """Ask for the rows' whole height, stretch them across the canvas, and scroll only in a short window.

        Asking for exactly that height, rather than growing with the window, is what keeps the buttons right
        under the rows whenever there is room for both.
        """
        needed = self.table.winfo_reqheight()
        if int(self.canvas.cget("height")) != needed:
            self.canvas.configure(height=needed)
        self.canvas.configure(scrollregion=(0, 0, 0, needed))
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width > 1:  # 1 until the canvas is laid out
            self.canvas.itemconfigure(self._table_item, width=width)
        if 1 < height < needed:
            self.scrollbar.grid()
        else:
            self.scrollbar.grid_remove()
            self.canvas.yview_moveto(0)

    def _wheel(self, event):
        if not (self.app.setup_shown and self.scrollbar.winfo_manager()
                and str(event.widget).startswith(str(self.canvas))):
            return
        up = event.num == 4 or getattr(event, "delta", 0) > 0  # X11 sends buttons 4 and 5; the rest a delta
        self.canvas.yview_scroll(-2 if up else 2, "units")

    def _show_buttons(self, *names):
        """Show just these buttons, in this order. The first is the default one, and takes the focus when the
        set changes, so Space, or Enter (_enter), presses it without reaching for the mouse."""
        for button in self.buttons.values():
            button.pack_forget()
            button.configure(default="normal")
        for i, name in enumerate(names):
            self.buttons[name].pack(side="left", padx=(0 if i == 0 else 8, 0))
            self.buttons[name].state(["!disabled"])
        if names:
            self.buttons[names[0]].configure(default="active")
        changed, self.shown_buttons = names != self.shown_buttons, names
        if changed:
            self.focus_primary()

    def focus_primary(self):
        if self.shown_buttons and self.app.setup_shown:
            self.buttons[self.shown_buttons[0]].focus_set()

    def _say(self, text, color=None):
        self.status.configure(text=text, foreground=theme.C[color] if color else "")
        if text:
            self.status.grid()
        else:
            self.status.grid_remove()

    # ------------------------------------------------------------ drawing

    def keys(self):
        """The helpers listed: all three, less the YouTube helper when Settings has it turned off."""
        off = self.app.settings["js_runtime"] == "none"
        return [key for key in KEYS if key in self.app.deps and not (key == "js" and off)]

    def verdict(self):
        """"ready" (still to set up), "done", "almost" or "failed", from the latest check and the last batch.

        Only a yt-dlp or FFmpeg that failed makes it "failed", and a YouTube helper that failed makes it
        "almost". A problem this screen won't install, such as an out-of-date copy that winget manages or a
        Node.js that is too old, doesn't hold it up either, except for a missing yt-dlp or FFmpeg, since no
        conversion runs without that. A helper that is there but won't run is a problem like a missing one
        (deps.unusable), so an install that leaves one is never "All set!".
        """
        found = self.app.deps
        installable = deps.setup_keys(found)
        problems = [key for key in self.keys() if deps.unusable(found[key])]
        # Only a failure Try again can do something about, so "failed" and "almost" never offer a Try again
        # with nothing to run. _forget_fixed already drops the others; this is for anything that keeps one.
        failed = [key for key in problems if key in self.failures and key in installable]
        if any(key != "js" for key in failed):
            return "failed"
        required_missing = [key for key in problems if key != "js" and found[key].state == deps.MISSING]
        if any(key not in failed and (key in installable or key in required_missing) for key in problems):
            return "ready"
        return "almost" if failed else "done"

    def refresh(self):
        """Redraw everything from the app's state and from what the batch has reported so far."""
        if not self.app.setup_shown:
            return  # redrawn by show_setup; nothing should animate out of sight
        if self.running:
            self._draw_running()
        elif self.app.checking or not self.app.deps:
            self._draw_looking()
        else:
            self._draw_result()
        self._draw_intro()

    def pause(self):
        """Stop every moving bar, for when the screen is put away. refresh() starts whichever it shows."""
        for bar in [self.looking_bar] + [row["bar"] for row in self.rows.values()]:
            bar.stop()

    def _draw_intro(self):
        off = self.app.settings["js_runtime"] == "none"
        # Before the first check no row is listed yet, but which rows will be is already known from Settings.
        listed = self.keys() if self.app.deps else [key for key in KEYS if not (key == "js" and off)]
        self.intro.configure(text=intro_text(listed, self.plan, deps.WINDOWS))

    def _update_plan(self):
        installable = deps.setup_keys(self.app.deps)
        for key in self.keys():
            dep = self.app.deps[key]
            if key in installable:
                self.plan[key] = "fetch" if deps.is_fetch(dep.command) else "pip"
            elif deps.unusable(dep) or (self.plan.get(key) == "fetch" and not deps.is_own_copy(dep.path)):
                # Left to the Settings tab now, or no longer the copy this screen fetched: Node.js, or a
                # deno or FFmpeg from winget or put there by hand. Decided from where the helper is now, not
                # from what happened earlier in the session. A pip plan stays, since check_ytdlp only ever
                # finds yt-dlp in the Python that runs Tapewright, which is where pip puts it.
                self.plan.pop(key, None)

    def _draw_looking(self):
        self._say(LOOKING)
        self.looking_bar.grid()
        self.looking_bar.start()
        self.rows_area.grid_remove()
        self._show_buttons("later")

    def _draw_running(self):
        self.looking_bar.stop()
        self.looking_bar.grid_remove()
        if self.cancelling:
            self._say("Cancelling…")
        elif self.batch_over:
            self._say("Checking that everything works…")
        else:
            self._say(self.stage)
        self.rows_area.grid()
        for key in KEYS:
            self._draw_row(key)
        # Hide first, so the button with the focus, the one Space and Enter press, never stops a download.
        # Hiding stops nothing: the batch runs through App.run_updates whether the page shows or not.
        self._show_buttons("hide", "cancel")
        self._sync_cancel()

    def _sync_cancel(self):
        # Nothing is left to cancel once a cancel is on its way or the batch is over, and App ignores Cancel
        # while the fetcher puts a helper in place (see App.update_swapping).
        stuck = self.cancelling or self.batch_over or self.app.update_swapping
        self.buttons["cancel"].state(["disabled"] if stuck else ["!disabled"])

    def _draw_result(self):
        self.looking_bar.stop()
        self.looking_bar.grid_remove()
        self._update_plan()
        self.rows_area.grid()
        for key in KEYS:
            self._draw_row(key)
        verdict = self.verdict()
        if verdict == "done":
            self._say("All set! Tapewright is ready.", "ok")
            self._show_buttons("start")
        elif verdict == "almost":
            self._say("Almost ready.", "warn")
            self._show_buttons("start", "retry")
        elif verdict == "failed":
            self._say("Some helpers didn't install.", "error")
            self._show_buttons("retry", "help", "later")
        else:
            self._say("")
            if deps.setup_keys(self.app.deps):
                # In place, whether or not its updates could be checked: nothing here installs it again.
                some_ok = any(not deps.unusable(self.app.deps[key]) for key in self.keys())
                self.buttons["install"].configure(text="Install the missing ones" if some_ok
                                                  else "Install them now")
                self._show_buttons("install", "later")
            else:
                # Something is missing that nothing here can install, and each such row's hint says how.
                self._show_buttons("check", "later")

    def _draw_row(self, key):
        parts = self.rows[key]
        if key not in self.keys():
            parts["bar"].stop()
            parts["frame"].grid_remove()
            return
        dep = self.app.deps[key]
        installable = deps.setup_keys(self.app.deps)
        parts["frame"].grid()
        parts["name"].configure(text=dep.label)
        if key == self.current:
            text, color = self.activity.get("text", ""), None
            self._draw_bar(parts)
        else:
            parts["bar"].stop()
            parts["bar"].grid_remove()
            parts["amount"].grid_remove()
            text, color = self._row_status(key, dep, installable)
        parts["state"].configure(text=text, foreground=theme.C[color] if color else "")
        hint = ""
        if key in installable:
            # A copy that is there but won't run too: every row this screen would install without asking says
            # how big the download is and where it comes from.
            hint = deps.download_hint(dep)
        elif dep.state in deps.PROBLEMS:
            hint = SETTINGS_HINT if dep.command else dep.how
        parts["hint"].configure(text=hint)
        if hint:
            parts["hint"].grid()
        else:
            parts["hint"].grid_remove()

    def _row_status(self, key, dep, installable):
        """(words, color name or None) for a row that isn't being installed right now."""
        # A failure the latest check no longer confirms was fixed since, by the batch or by hand. A copy
        # that is there but won't run confirms it.
        current = self.running or deps.unusable(dep)
        if key in self.failures and current:
            return self.failures[key], "error"
        if key in self.notes and current:
            return self.notes[key], None
        if self.running and key in self.installed:
            return INSTALLED, "ok"
        if dep.state == deps.UNSUPPORTED and key not in installable:
            return TOO_OLD, None
        return STATUS_WORDS[dep.state], STATUS_COLORS.get(dep.state)

    def _draw_bar(self, parts):
        """Moving while nobody can say how far along it is, as with pip; filling once a download says."""
        bar, done, total = parts["bar"], self.activity.get("done"), self.activity.get("total")
        bar.grid()
        if total:
            bar.stop()  # which also sets the value to 0, so the real value goes in after it
            bar.configure(mode="determinate", maximum=total, value=min(done, total))
        else:
            if str(bar.cget("mode")) != "indeterminate":
                # The last download's maximum would leave the moving block far too slow to see.
                bar.configure(mode="indeterminate", maximum=100, value=0)
            bar.start()
        if done is None:
            parts["amount"].grid_remove()
        else:
            parts["amount"].configure(text=megabytes(done, total))
            parts["amount"].grid()

    # ------------------------------------------------------------ called by the app

    def on_check_started(self, online):
        self.refresh()

    def on_deps_changed(self):
        if self.running and self.batch_over:
            self._settle()
        if not self.running:
            self._forget_fixed()
        self.refresh()

    def _forget_fixed(self):
        """Forget what went wrong with each helper this screen now has nothing to install for.

        That is one a check has since found working, checked for updates or not, and one left to the Settings
        tab, such as an FFmpeg put in place with winget, which often trails the newest release. Otherwise the
        old sentence would come back, as if it were news, if that helper went missing again later, and a red
        sentence about a download Try again can't repeat would sit under "All set!".
        """
        installable = deps.setup_keys(self.app.deps)
        for key in self.app.deps:
            if key not in installable:
                self.failures.pop(key, None)
                self.notes.pop(key, None)

    def _fail(self, key, text):
        self.failures[key] = text
        self.failed_once.add(key)

    def _settle(self):
        """The check after a batch is back, so each install can be judged by what it really achieved."""
        for key, command in self.installed.items():
            dep = self.app.deps.get(key)
            if dep is not None and deps.setup_runs(command) and deps.unusable(dep):
                # It ran without an error, yet the check still can't use it: still missing or too old, or
                # there but won't run, as a new copy stopped from starting in its final folder is. Its detail
                # says why: whole in the update log, and on the row as much as a row can show.
                if dep.detail:
                    self.app.settings_tab.log(f"{dep.label}: the install finished, but the check after it "
                                              f"still can't use it. {dep.detail}")
                self._fail(key, shorten(dep.detail) if dep.detail else DIDNT_INSTALL)
        self.running = self.batch_over = self.cancelling = False
        self.current = None

    def on_update(self, event, dep, *details):
        """One of App.update_listeners, so it hears every batch, including one the Settings tab started."""
        if event == "start":
            index, total = details
            if not self.running:  # the first tool of a new batch: forget how the last one went
                self.running, self.batch_over, self.cancelling = True, False, False
                for record in (self.installed, self.failures, self.notes, self.errors):
                    record.clear()
            verb = "Updating" if dep.state in (deps.OUTDATED, deps.UNSUPPORTED) else "Installing"
            self.stage = f"Step {index} of {total}: {verb} the {dep.label}…"
            self.current, self.activity = dep.key, {"text": "Getting ready…"}
            self.refresh()
        elif event == "line":
            parsed = deps.parse_fetch_line(details[0]) if deps.is_fetch(dep.command) else None
            if parsed is None or dep.key != self.current:
                return  # pip and winget say nothing a bar could show, so theirs stays moving
            if parsed[0] == "step":
                self.activity["text"] = parsed[1]
            elif parsed[0] == "progress":
                self.activity["done"], self.activity["total"] = parsed[1], parsed[2]
            elif parsed[0] == "error":
                self.errors[dep.key] = parsed[1]
            if self.app.setup_shown:
                self._draw_row(dep.key)
                if "cancel" in self.shown_buttons:
                    self._sync_cancel()  # App sets update_swapping as it reads the fetcher's SWAP_STEP line
        elif event == "done":
            ok, message = details
            if ok:
                self.installed[dep.key] = dep.command
            elif message in (deps.cancel_text(dep), deps.cancel_text(dep, swapped=True)):
                # A note, never a failure, whether or not the fetcher had begun putting the helper in place.
                self.notes[dep.key] = message[:1].upper() + message[1:]
            elif deps.setup_runs(dep.command):
                # Only what this screen runs. How a winget upgrade or `deno upgrade` from the Settings
                # tab went is that tab's log's to say: kept here, its failure would offer a Try again with
                # nothing to run, and make Not now think the YouTube helper had been tried here.
                self._fail(dep.key, self.errors.get(dep.key) or DIDNT_INSTALL)
            self.current = None
            self.refresh()
        elif event == "finished":
            self.batch_over, self.current = True, None
            self.refresh()

    # ------------------------------------------------------------ the buttons

    def install(self):
        """[Install them now] and [Try again]: every helper with a problem that one click can fix."""
        app = self.app
        if app.checking or app.updating:
            return
        if not app.last_check_online:
            # The commands carry the versions the last check found, and an offline one may have found none,
            # so look online first: the rows then install exactly the versions that check reports.
            app.check_dependencies(online=True, then=self._install_checked)
            self.refresh()
            return
        self._install_checked()

    def _install_checked(self):
        app = self.app
        if not app.setup_shown or app.updating:  # put off, or another update began, while the check ran
            return
        keys = deps.setup_keys(app.deps)
        # confirm=False: this screen, which says what each helper is and where it comes from, was the
        # question. A second box asking it again would only teach people to click Yes without reading.
        if not (keys and app.run_updates(keys, confirm=False)):
            self.refresh()

    def start_using(self):
        # "almost" finishes setup too: yt-dlp and FFmpeg are in place, and deps.setup_needed still brings the
        # page back if either goes missing. A YouTube helper that won't install is named on its row, on the
        # Settings tab and in the warning before a link, and a page that came back for it at every launch,
        # with a download that fails the same way each time, would help nobody.
        if self.verdict() in ("done", "almost"):
            self.app.finish_setup()
        self.app.hide_setup()
        self.app.notebook.select(self.app.mp3_tab)

    def not_now(self):
        found = self.app.deps
        # In place, as "almost" counts it: one that runs but couldn't be checked for updates will do.
        ready = all(key in found and not deps.unusable(found[key]) for key in ("yt-dlp", "ffmpeg"))
        # Only once the YouTube helper was really tried and failed: Not now before trying anything shouldn't
        # give up for good on the one helper that is optional.
        if ready and "js" in self.failed_once:
            self.app.finish_setup()
        self.app.hide_setup(put_off=True)

    def hide(self):
        """[Hide this page]: the tabs come back while a batch goes on. Unlike Not now, it puts nothing off."""
        self.app.hide_setup()

    def help_with_this(self):
        self.app.show_help(HELP_TOPIC)

    def check_again(self):
        self.app.check_dependencies(online=True)
        self.refresh()

    def cancel(self):
        # App decides, and says whether it acted; this page only follows. A look of its own at
        # update_swapping could be overtaken by the worker reading the fetcher's swap line while the page
        # redraws, and then the page would say "Cancelling…", with Cancel disabled, through the whole of the
        # next helper's install.
        if self.app.cancel_update():
            self.cancelling = True
        self.refresh()

    def toggle_details(self):
        self.details_shown = not self.details_shown
        floor = int(DETAILS_FLOOR * self.winfo_fpixels("1i") / 96) if self.details_shown else 0
        self.rowconfigure(6, minsize=floor)
        if self.details_shown:
            self.logview.grid()
        else:
            self.logview.grid_remove()
        self.details_button.configure(text="Hide details" if self.details_shown else "Show details")

    def _enter(self, event):
        """Enter presses the button that has the focus, as it does in Windows, and otherwise the default one.

        Show details counts as one of the buttons, so Enter on it never installs anything. The default is the
        first button shown, which is Hide while a batch runs, so Enter stops a download only when Cancel was
        given the focus on purpose. A disabled button that has the focus presses nothing.
        """
        if not (self.app.setup_shown and self.shown_buttons):
            return None
        pressable = [self.buttons[name] for name in self.shown_buttons] + [self.details_button]
        # event.widget is the widget with the focus, or its name when Tk has no widget object for it.
        button = event.widget if event.widget in pressable else self.buttons[self.shown_buttons[0]]
        button.invoke()
        return "break"

    def _escape(self, _event):
        if not self.app.setup_shown:
            return
        if "later" in self.shown_buttons:
            self.not_now()
        elif "hide" in self.shown_buttons:
            self.hide()
