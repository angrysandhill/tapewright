# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""The window: four tabs, the dependency state they share, and the gates between them.

Everything that touches a widget runs on the Tk main thread. Workers (conversions, updates,
checks) talk back through a queue the main thread drains every POLL_MS. That is also what
makes each gate's check-and-set atomic without a lock: nothing else runs in between.
"""

import copy
import os
import queue
import threading
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

from tapewright import APP_NAME, __version__, config, deps, help_content, launch, procs, theme, widgets
from tapewright.convert_tab import ConvertTab
from tapewright.help_tab import HelpTab
from tapewright.settings_tab import SettingsTab

POLL_MS = 60


class App:
    def __init__(self, root, settings):
        self.root = root
        self.settings = settings
        self.deps = {}                # key -> deps.Dep from the latest check
        self.checking = False
        self.updating = None          # key of the Dep being updated, or None
        self.active_jobs = set()      # tabs with a conversion running
        self._after_check = []        # callbacks waiting on the check in progress
        self._update_queue = []
        self._update_runner = None
        self._update_cancelled = False
        self._auto_updated = False
        self._recheck_online = None   # a check asked for while another was running
        self._events = queue.Queue()
        # Seams for tests: all three are modal and would otherwise block a scripted run.
        self.ask_outdated = widgets.ask_outdated
        self.confirm = lambda title, message: messagebox.askyesno(title, message, parent=self.root)
        self.inform = lambda title, message: messagebox.showinfo(title, message, parent=self.root)

        theme.apply(root)  # before the first widget: styles only reach widgets created after it
        root.title(f"{APP_NAME} {__version__}")
        scale = root.winfo_fpixels("1i") / 96.0
        width = min(int(900 * scale), root.winfo_screenwidth() - int(40 * scale))
        height = min(int(760 * scale), root.winfo_screenheight() - int(90 * scale))
        root.geometry(f"{width}x{height}")
        # Tall enough for the tape deck and a few lines of log, but never taller than the screen.
        root.minsize(int(720 * scale), min(int(680 * scale), root.winfo_screenheight() - int(90 * scale)))

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self.mp3_tab = ConvertTab(self.notebook, self, "mp3")
        self.mp4_tab = ConvertTab(self.notebook, self, "mp4")
        self.settings_tab = SettingsTab(self.notebook, self)
        self.help_tab = HelpTab(self.notebook, self)
        self.notebook.add(self.mp3_tab, text="   To MP3   ")
        self.notebook.add(self.mp4_tab, text="   To MP4   ")
        self.notebook.add(self.settings_tab, text="   Settings   ")
        self.notebook.add(self.help_tab, text="   Help   ")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(POLL_MS, self._drain)
        if settings.problem:
            self.settings_tab.log(settings.problem)
        # Installed versions are always read; the network is only asked if allowed.
        self.check_dependencies(online=settings["check_on_startup"])

    # ------------------------------------------------------------ plumbing

    def post(self, fn, *args):
        """Run fn(*args) on the main thread. Safe to call from any thread."""
        self._events.put((fn, args))

    def _drain(self):
        for _ in range(500):
            try:
                fn, args = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                self.settings_tab.log("Internal error:\n" + traceback.format_exc())
        self.root.after(POLL_MS, self._drain)

    def save_settings(self):
        problem = self.settings.save()
        if problem:
            self.settings_tab.log(problem)

    def show_settings(self):
        self.notebook.select(self.settings_tab)

    def tabs(self):
        return (self.mp3_tab, self.mp4_tab)

    # ------------------------------------------------------------ dependency checks

    def check_dependencies(self, online=True, then=None):
        if then is not None:
            self._after_check.append(then)
        if self.checking:
            return
        self.checking = True
        snapshot = copy.deepcopy(self.settings.data)
        self.settings_tab.on_check_started(online)

        def work():
            found, updates = deps.check_all(snapshot, online=online)
            self.post(self._check_finished, found, updates, online)

        threading.Thread(target=work, daemon=True).start()

    def recheck(self, online):
        """Check again with the current settings, even while a check with the old ones runs."""
        if self.checking:
            self._recheck_online = bool(self._recheck_online) or online
        else:
            self.check_dependencies(online=online)

    def _check_finished(self, found, updates, online):
        self.checking = False
        if updates:
            self.settings["latest_cache"].update(updates)
            self.save_settings()
        self.deps = found
        self.settings_tab.on_deps_changed()
        for tab in self.tabs():
            tab.on_deps_changed()
        waiting, self._after_check = self._after_check, []
        for fn in waiting:
            fn()
        if self._recheck_online is not None:
            again, self._recheck_online = self._recheck_online, None
            self.check_dependencies(online=again)
            return
        ytdlp = found.get("yt-dlp")
        if (online and self.settings["auto_update_ytdlp"] and not self._auto_updated and ytdlp
                and ytdlp.state in (deps.OUTDATED, deps.MISSING) and ytdlp.command
                and not self.active_jobs and not self.updating):
            self._auto_updated = True
            self.settings_tab.log("Automatic yt-dlp updates are on, so updating it now.")
            self.run_updates(["yt-dlp"], confirm=False)

    # ------------------------------------------------------------ the conversion gate

    def request_start(self, tab, kind, start):
        """Decide whether a conversion may start, and call start() if it may.

        Order matters. An update in progress refuses outright, since the tool is being
        replaced underneath. A check in progress defers, so a click in the first second
        after launch cannot slip past the warning. Then any problem with a tool this job
        actually uses gets the hard warning.
        """
        if not tab.pending:
            return
        if self.updating:
            tab.start_declined()
            self.inform(APP_NAME, f"The {self.deps[self.updating].label} is being updated. "
                                  "Start again when that finishes.")
            return
        if self.checking:
            tab.set_status("Waiting for the dependency check to finish…")
            self.check_dependencies(then=lambda: self.request_start(tab, kind, start))
            return
        problems = deps.problems_for(self.deps, kind)
        if problems:
            blocked = deps.blocking(problems, self.settings["outdated_policy"])
            choice = self.ask_outdated(self.root, problems, blocked)
            if not tab.pending:
                return
            if choice == "update":
                tab.start_declined()
                self.show_settings()
                if any(d.command for d in problems):
                    self.run_updates([d.key for d in problems if d.command])
                return
            if choice != "continue" or blocked or self.updating:
                tab.start_declined()
                return
            tab.log("Starting anyway, although " + "; ".join(
                f"the {d.label} is {deps.STATE_LABELS[d.state].lower()}" for d in problems) + ".")
        self.active_jobs.add(tab)
        self.settings_tab.on_busy_changed()
        start()

    def job_finished(self, tab):
        self.active_jobs.discard(tab)
        self.settings_tab.on_busy_changed()

    # ------------------------------------------------------------ updates

    def run_updates(self, keys, confirm=True):
        todo = [self.deps[k] for k in keys if k in self.deps and self.deps[k].command]
        if not todo:
            return False
        if self.active_jobs:
            self.inform(APP_NAME, "A conversion is running. Let it finish, or cancel it, before "
                                  "updating: replacing a tool while it is in use can break both.")
            return False
        if self.updating:
            self.inform(APP_NAME, f"The {self.deps[self.updating].label} is already being updated.")
            return False
        if confirm:
            # Plain words here; the exact commands go to the update log as each one starts.
            plan = "\n\n".join(deps.describe_update(d) for d in todo)
            if not self.confirm(APP_NAME, f"{plan}\n\nThe update log on the Settings tab shows each "
                                          "step as it happens.\n\nGo ahead?"):
                return False
        self.show_settings()
        self._update_queue = list(todo)
        self._update_cancelled = False
        self._next_update()
        return True

    def _next_update(self):
        if not self._update_queue:
            if self.updating and not self._update_cancelled:
                # Each tool logs its own "done", so the end of the whole batch needs a line too:
                # the Help tab tells people to wait for this one before converting.
                self.settings_tab.log(help_content.UPDATES_FINISHED + ".")
            self.updating = None
            self._update_runner = None
            self.settings_tab.on_busy_changed()
            # Installed versions changed; the latest ones were just fetched and are cached.
            self.check_dependencies(online=False)
            return
        dep = self._update_queue.pop(0)
        runner = procs.Runner()
        self.updating, self._update_runner = dep.key, runner
        self.settings_tab.on_busy_changed()
        self.settings_tab.log(f"{dep.action or 'Update'}: {dep.label}")
        self.settings_tab.log("$ " + procs.format_command(dep.command))

        def work():
            try:
                code = runner.run(dep.command, lambda line: self.post(self.settings_tab.log_tool_line, line))
                self.post(self._update_finished, dep, code, "")
            except procs.Cancelled:
                self.post(self._update_finished, dep, None, "cancelled")
            except Exception as e:
                self.post(self._update_finished, dep, None, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _update_finished(self, dep, code, error):
        if error == "cancelled":
            self.settings_tab.log(f"{dep.label}: update cancelled. It may be half-installed; "
                                  "run the update again.")
            self._update_queue.clear()
            self._update_cancelled = True
        elif error:
            self.settings_tab.log(f"{dep.label}: {error}")
        elif code == 0:
            self.settings_tab.log(f"{dep.label}: done.")
        else:
            self.settings_tab.log(f"{dep.label}: {deps.update_failure_text(dep, code)}")
        self._next_update()

    def cancel_update(self):
        runner = self._update_runner
        if runner is not None:
            if self._update_queue:
                # Said here, because the tool running now may already have finished: its "done"
                # would otherwise be followed by "Finished updating" for a batch that wasn't.
                self.settings_tab.log("Update cancelled. Not updated: " + ", ".join(
                    f"the {d.label}" for d in self._update_queue) + ".")
                self._update_cancelled = True
            self._update_queue.clear()
            threading.Thread(target=runner.cancel, daemon=True).start()

    # ------------------------------------------------------------ closing

    def on_close(self):
        if self.updating:
            label = self.deps[self.updating].label
            if not self.confirm(APP_NAME, f"The {label} is still being updated. Quitting now can leave it "
                                          "half-installed and broken.\n\nQuit anyway?"):
                return
        elif self.active_jobs:
            if not self.confirm(APP_NAME, "A conversion is still running. Cancel it and quit?"):
                return
        self.shutdown()

    def shutdown(self):
        """Every exit goes through here. Children run in their own process groups, so nothing
        else would ever stop them: a closed window must not leave an ffmpeg writing."""
        for tab in list(self.active_jobs):
            tab.cancel(wait=True)
        if self._update_runner is not None:
            self._update_runner.cancel()
        self.save_settings()
        self.root.destroy()


def _enable_dpi_awareness():
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass  # older Windows: the window is merely blurry


def main():
    """Open the window. Start through launch.main(), which first checks this Python can run it."""
    _enable_dpi_awareness()
    settings = config.Settings()
    settings.load()
    try:
        root = tk.Tk()
    except tk.TclError as e:  # tkinter imported, but Tcl/Tk itself is broken, or there is no display
        launch.explain(f"Tkinter couldn't open a window ({e}).", launch.tk_failure_fix(str(e)))
        return 1
    App(root, settings)
    root.mainloop()
    return 0
