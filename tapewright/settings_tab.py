# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Settings tab: each tool's state, the buttons that fix it, and the update policy."""

import datetime
import platform
import re
import tkinter as tk
from tkinter import ttk

from tapewright import APP_NAME, __version__, deps, procs, theme, versions, widgets

STATE_COLORS = {
    deps.OK: theme.C["ok"], deps.OUTDATED: theme.C["error"], deps.MISSING: theme.C["error"],
    deps.UNSUPPORTED: theme.C["error"], deps.UNKNOWN: theme.C["warn"],
}
ROWS = (
    ("yt-dlp", "yt-dlp", "Downloads from links."),
    ("ffmpeg", "FFmpeg", "Converts and merges. Every conversion uses it."),
    ("js", "deno or Node.js", "Lets yt-dlp answer YouTube's challenges."),
)
JS_LABELS = {"auto": "Automatic (deno, else Node.js)", "deno": "deno", "node": "Node.js", "none": "Off"}
_SPINNER = re.compile(r"^\s*[-\\|/]\s*$")  # winget's progress spinner, one frame per line
RECHECK_DELAY_MS = 800


class SettingsTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=12)
        self.app = app
        self.rows = {}
        self._pending_recheck = None
        self._build()

    # ------------------------------------------------------------ layout

    def _build(self):
        self.columnconfigure(0, weight=1)
        # Weighted rows are the first to give up space, so without a floor the log is the
        # part that vanishes on a short window -- and it is where update output appears.
        self.rowconfigure(3, weight=1, minsize=int(110 * self.winfo_fpixels("1i") / 96))
        s = self.app.settings

        tools = ttk.LabelFrame(self, text="Tools", padding=10)
        tools.grid(row=0, column=0, sticky="ew")
        tools.columnconfigure(1, weight=1)
        header = ttk.Frame(tools)
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        self.check_status = ttk.Label(header, text="Not checked yet.")
        self.check_status.grid(row=0, column=0, sticky="w")
        self.update_all_button = ttk.Button(header, text="Update everything out of date",
                                            command=self.update_all)
        self.update_all_button.grid(row=0, column=1, padx=(8, 0))
        self.cancel_update_button = ttk.Button(header, text="Cancel update", command=self.app.cancel_update)
        self.cancel_update_button.grid(row=0, column=2, padx=(8, 0))
        self.cancel_update_button.grid_remove()
        self.check_button = ttk.Button(header, text="Check for updates",
                                       command=lambda: self.app.recheck(True))
        self.check_button.grid(row=0, column=3, padx=(8, 0))

        for i, (key, name, summary) in enumerate(ROWS):
            r = 1 + i * 2
            name_label = ttk.Label(tools, text=deps.Dep(key, name, False).label, font=theme.FONTS["display"])
            name_label.grid(row=r, column=0, sticky="w", padx=(0, 16))
            state = tk.Label(tools, text="Checking…", anchor="w", justify="left", font=theme.FONTS["small"])
            state.grid(row=r, column=1, sticky="w")
            action = ttk.Button(tools, text="Update", command=lambda k=key: self.app.run_updates([k]))
            action.grid(row=r, column=2, sticky="e")
            action.grid_remove()
            detail = ttk.Label(tools, text=summary, foreground=theme.C["text_dim"], justify="left")
            detail.grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(2, 10))
            self.rows[key] = {"name": name_label, "state": state, "action": action, "detail": detail}
        self.tools = tools
        tools.bind("<Configure>", lambda e: self._rewrap())

        policy = ttk.LabelFrame(self, text="Keeping them up to date", padding=10)
        policy.grid(row=1, column=0, sticky="ew", pady=12)
        self._check(policy, 0, "Check for updates when Tapewright starts", "check_on_startup", None)
        self._check(policy, 1, "Update yt-dlp automatically when a new version is out",
                    "auto_update_ytdlp", None)
        self._check(policy, 2, "Include yt-dlp's recommended extras when installing it (yt-dlp[default])",
                    "ytdlp_extras", False)
        self._radios(policy, 3, "yt-dlp channel:", "ytdlp_channel",
                     (("stable", "Stable"), ("nightly", "Nightly (site fixes arrive days sooner)")), True)
        self._radios(policy, 4, "When a tool is out of date:", "outdated_policy",
                     (("warn", "Warn before every conversion"),
                      ("block", "Block conversions until it is updated")),
                     None)

        ttk.Label(policy, text="Offline:").grid(row=5, column=0, sticky="w", pady=3, padx=(0, 8))
        offline = ttk.Frame(policy)
        offline.grid(row=5, column=1, sticky="w")
        intro = "If the online check fails, treat yt-dlp as out of date once it is"
        ttk.Label(offline, text=intro).pack(side="left")
        self.days = tk.StringVar(value=str(s["offline_max_age_days"]))
        ttk.Spinbox(offline, from_=7, to=365, width=5, textvariable=self.days).pack(side="left", padx=6)
        ttk.Label(offline, text="days old").pack(side="left")
        self.days.trace_add("write", self._days_changed)

        ttk.Label(policy, text="YouTube helper:").grid(row=6, column=0, sticky="w", pady=3, padx=(0, 8))
        self.js_box = ttk.Combobox(policy, state="readonly", values=list(JS_LABELS.values()), width=34)
        self.js_box.set(JS_LABELS.get(s["js_runtime"], JS_LABELS["auto"]))
        self.js_box.grid(row=6, column=1, sticky="w")
        self.js_box.bind("<<ComboboxSelected>>", lambda e: self._changed(
            "js_runtime", list(JS_LABELS)[self.js_box.current()], False))

        bottom = ttk.Frame(self)
        bottom.grid(row=2, column=0, sticky="ew")
        ttk.Label(bottom, text="Update log").pack(side="left")
        ttk.Button(bottom, text="Copy diagnostics", command=self.copy_diagnostics).pack(side="right")
        self.logview = widgets.LogView(self, height=8)
        self.logview.grid(row=3, column=0, sticky="nsew", pady=(4, 0))

    def _rewrap(self):
        """Wrap each row's words to the room they have.

        The state line shares its row with the name and the button, so it gets the room they leave;
        otherwise a long state runs under the button in the narrowest window. Redone when a name or
        a button's text changes, not only on a resize.
        """
        width = self.tools.winfo_width()
        if width <= 1:  # not laid out yet; the first <Configure> comes back here
            return
        names = max(row["name"].winfo_reqwidth() for row in self.rows.values())
        buttons = max(row["action"].winfo_reqwidth() for row in self.rows.values())
        for row in self.rows.values():
            row["detail"].configure(wraplength=max(width - 40, 200))
            row["state"].configure(wraplength=max(width - names - buttons - 60, 150))

    def _check(self, parent, row, text, key, recheck):
        var = tk.BooleanVar(value=self.app.settings[key])
        box = ttk.Checkbutton(parent, text=text, variable=var)
        box.grid(row=row, column=0, columnspan=2, sticky="w", pady=3)
        var.trace_add("write", lambda *_: self._changed(key, var.get(), recheck))

    def _radios(self, parent, row, label, key, options, recheck):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
        var = tk.StringVar(value=self.app.settings[key])
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=1, sticky="w")
        for value, text in options:
            ttk.Radiobutton(frame, text=text, value=value, variable=var).pack(side="left", padx=(0, 16))
        var.trace_add("write", lambda *_: self._changed(key, var.get(), recheck))

    # ------------------------------------------------------------ settings

    def _changed(self, key, value, recheck):
        """recheck: None = no new check needed, False = re-read installed tools, True = also go online."""
        if self.app.settings[key] == value:
            return
        self.app.settings[key] = value
        self.app.save_settings()
        if recheck is None:
            return
        # Debounced: a spinbox fires on every keystroke, and each check starts three processes.
        online = recheck or bool(self._pending_recheck and self._pending_recheck[1])
        if self._pending_recheck:
            self.after_cancel(self._pending_recheck[0])
        after_id = self.after(RECHECK_DELAY_MS, lambda: self._run_recheck(online))
        self._pending_recheck = (after_id, online)

    def _run_recheck(self, online):
        self._pending_recheck = None
        self.app.recheck(online)

    def _days_changed(self, *_):
        text = self.days.get().strip()
        if text.isdigit() and 1 <= int(text) <= 3650:
            self._changed("offline_max_age_days", int(text), False)

    # ------------------------------------------------------------ called by the app

    def on_check_started(self, online):
        self.check_status.configure(text="Checking for updates…" if online else "Reading installed versions…")
        self._refresh_buttons()

    def on_deps_changed(self):
        for key, row in self.rows.items():
            d = self.app.deps.get(key)
            if d is None:
                continue
            row["name"].configure(text=d.label)
            bits = [deps.STATE_LABELS[d.state]]
            if d.installed:
                bits.append(f"installed {d.installed}")
            if d.latest and (not d.installed or versions.is_newer(d.latest, d.installed)):
                bits.append(f"latest {d.latest}")
            row["state"].configure(text="   ·   ".join(bits), foreground=STATE_COLORS[d.state])
            # One paragraph per tool keeps the update log on screen; full paths are in
            # Copy diagnostics, where a bug report needs them.
            text = d.detail
            if d.how:
                text += ("  " if text else "") + f"Updates with: {d.how}."
            row["detail"].configure(text=text)
        problems = [d for d in self.app.deps.values() if d.state in deps.PROBLEMS]
        summary = "Everything is up to date." if not problems else (
            f"{len(problems)} need{'s' if len(problems) == 1 else ''} attention.")
        self.check_status.configure(text=f"Checked at {datetime.datetime.now():%H:%M}. {summary}")
        self._refresh_buttons()

    def on_busy_changed(self):
        self._refresh_buttons()
        if self.app.updating:
            self.check_status.configure(text=f"Updating the {self.app.deps[self.app.updating].label}…")

    def _refresh_buttons(self):
        busy = bool(self.app.updating or self.app.active_jobs or self.app.checking)
        for key, row in self.rows.items():
            d = self.app.deps.get(key)
            button = row["action"]
            if d and d.action and d.command:
                button.configure(text=d.action)
                button.grid()
                button.state(["disabled"] if busy else ["!disabled"])
            else:
                button.grid_remove()
        fixable = any(d.state in deps.PROBLEMS and d.command for d in self.app.deps.values())
        self.update_all_button.state(["disabled"] if busy or not fixable else ["!disabled"])
        self.check_button.state(["disabled"] if self.app.checking or self.app.updating else ["!disabled"])
        if self.app.updating:
            self.cancel_update_button.grid()
        else:
            self.cancel_update_button.grid_remove()
        self._rewrap()

    def update_all(self):
        fixable = [d.key for d in self.app.deps.values() if d.state in deps.PROBLEMS and d.command]
        self.app.run_updates(fixable)

    def log(self, text):
        for line in str(text).splitlines() or [""]:
            self.logview.append(line)

    def log_tool_line(self, line):
        if line.strip() and not _SPINNER.match(line):
            self.logview.append(line)

    def copy_diagnostics(self):
        s = self.app.settings
        lines = [f"{APP_NAME} {__version__}",
                 f"Python {platform.python_version()} ({procs.python_exe()})",
                 platform.platform()]
        for d in self.app.deps.values():
            lines.append(f"{d.name}: {d.state}; installed {d.installed or '-'}; "
                         f"latest {d.latest or '-'}; at {d.path or '-'}")
        lines.append(f"channel={s['ytdlp_channel']} extras={s['ytdlp_extras']} "
                     f"policy={s['outdated_policy']} js={s['js_runtime']}")
        text = "\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.log("Diagnostics copied to the clipboard:\n" + text)
