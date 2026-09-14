# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tk pieces shared by the tabs: the log, the red banner, the warning dialog and the edit menu."""

import sys
import tkinter as tk
from tkinter import ttk

from tapewright import deps

WARNING_RED = "#b3261e"


class LogView(ttk.Frame):
    """A read-only scrolling log that follows new output only while you are at the bottom.

    The view keeps the last MAX_LINES lines and says how many it dropped: a log that trims
    silently reads as a complete one.
    """

    MAX_LINES = 5000

    def __init__(self, master, height=10):
        super().__init__(master)
        self.text = tk.Text(self, height=height, wrap="none", state="disabled",
                            font=("Consolas", 9), relief="solid", borderwidth=1)
        ys = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        xs = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._dropped = 0

    def append(self, line):
        follow = self.text.yview()[1] >= 0.999
        self.text.configure(state="normal")
        self.text.insert("end", line.rstrip("\r\n") + "\n")
        count = int(self.text.index("end-1c").split(".")[0]) - 1
        if count > self.MAX_LINES + (1 if self._dropped else 0):
            if not self._dropped:
                self.text.insert("1.0", "\n")  # line 1 becomes the notice
                count += 1
            drop = count - self.MAX_LINES - 1
            self.text.delete("2.0", f"{2 + drop}.0")
            self._dropped += drop
            self.text.delete("1.0", "1.end")
            self.text.insert("1.0", f"[{self._dropped} earlier lines are not shown]")
        self.text.configure(state="disabled")
        if follow:
            self.text.see("end")

    def contents(self):
        return self.text.get("1.0", "end-1c")


class Banner(tk.Frame):
    """A red strip naming whatever is out of date. It removes itself when there is nothing to say."""

    def __init__(self, master, on_open_settings):
        super().__init__(master, background=WARNING_RED, padx=10, pady=6)
        self.label = tk.Label(self, background=WARNING_RED, foreground="white", anchor="w",
                              justify="left", font=("Segoe UI", 9, "bold"))
        self.label.pack(side="left", fill="x", expand=True)
        tk.Button(self, text="Open Settings", command=on_open_settings, relief="flat",
                  background="white", foreground=WARNING_RED, activebackground="#f2d4d1",
                  font=("Segoe UI", 9, "bold"), padx=8).pack(side="right")
        self.bind("<Configure>", lambda e: self.label.configure(wraplength=max(e.width - 160, 200)))

    def set_problems(self, problems):
        if not problems:
            self.grid_remove()
            return
        parts = [f"{d.name} is {deps.STATE_LABELS[d.state].lower()}" for d in problems]
        self.label.configure(text="⚠  " + "; ".join(parts) + ". Conversions will warn before they start.")
        self.grid()


def ask_outdated(parent, problems, blocked):
    """The hard warning shown before a conversion. Returns "update", "continue" or "cancel".

    "Convert anyway" exists only when nothing is blocking, and it is never the default
    button: going ahead with a broken tool has to be a deliberate click.
    """
    dialog = tk.Toplevel(parent)
    dialog.title("Tools need attention")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)
    choice = {"value": "cancel"}

    def close(value):
        choice["value"] = value
        dialog.destroy()

    body = ttk.Frame(dialog, padding=16)
    body.pack(fill="both", expand=True)
    if blocked:
        heading = "This can't start until these are fixed:"
    else:
        heading = "Out-of-date tools are a common reason downloads fail:"
    tk.Label(body, text="⚠  " + heading, foreground=WARNING_RED, font=("Segoe UI", 11, "bold"),
             anchor="w", justify="left").pack(fill="x", pady=(0, 10))
    for d in problems:
        version = f"  {d.installed}" if d.installed else ""
        if d.latest and d.state == deps.OUTDATED:
            version += f"  →  {d.latest}"
        ttk.Label(body, text=f"{d.name} — {deps.STATE_LABELS[d.state]}{version}",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(body, text=d.detail, wraplength=460, justify="left").pack(anchor="w", pady=(0, 8))
    if blocked and any(d.state != deps.MISSING or not d.required for d in blocked):
        ttk.Label(body, text="Settings are set to block conversions while a tool is out of date.",
                  foreground="#555").pack(anchor="w", pady=(0, 8))

    buttons = ttk.Frame(body)
    buttons.pack(fill="x", pady=(8, 0))
    fixable = any(d.command for d in problems)
    ttk.Button(buttons, text="Cancel", command=lambda: close("cancel")).pack(side="right")
    if not blocked:
        anyway = ttk.Button(buttons, text="Convert anyway", command=lambda: close("continue"))
        anyway.pack(side="right", padx=(0, 8))
    primary = ttk.Button(buttons, text="Update now" if fixable else "Open Settings",
                         command=lambda: close("update"))
    primary.pack(side="left")
    dialog.bind("<Escape>", lambda e: close("cancel"))
    dialog.protocol("WM_DELETE_WINDOW", lambda: close("cancel"))

    dialog.update_idletasks()
    top = parent.winfo_toplevel()
    x = top.winfo_rootx() + max((top.winfo_width() - dialog.winfo_width()) // 2, 0)
    y = top.winfo_rooty() + max((top.winfo_height() - dialog.winfo_height()) // 3, 0)
    dialog.geometry(f"+{x}+{y}")
    primary.focus_set()
    dialog.grab_set()
    parent.wait_window(dialog)
    return choice["value"]


def select_all(entry):
    entry.select_range(0, "end")
    entry.icursor("end")
    return "break"


def add_edit_menu(entry):
    """Give an entry a right-click Cut/Copy/Paste menu, and Ctrl+A to select all of it.

    Tk entries come with neither, yet right-click Paste is how many people paste, and the Help
    tab tells them to use both. Every box the Help tab mentions needs this.
    """
    def editable():
        return entry.instate(["!disabled", "!readonly"])

    menu = tk.Menu(entry, tearoff=False)
    menu.add_command(label="Cut", command=lambda: editable() and entry.event_generate("<<Cut>>"))
    menu.add_command(label="Copy", command=lambda: entry.event_generate("<<Copy>>"))
    menu.add_command(label="Paste", command=lambda: editable() and entry.event_generate("<<Paste>>"))
    menu.add_separator()
    menu.add_command(label="Select all", command=lambda: select_all(entry))

    def popup(event):
        entry.focus_set()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    entry.bind("<Button-3>", popup)
    if sys.platform == "darwin":  # a Mac's secondary click
        entry.bind("<Button-2>", popup)
        entry.bind("<Control-Button-1>", popup)
    entry.bind("<Control-a>", lambda e: select_all(entry))
    entry.bind("<Control-A>", lambda e: select_all(entry))
