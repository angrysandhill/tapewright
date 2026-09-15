# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""The window: four tabs, the setup screen, the dependency state they share, and the gates between them.

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

from tapewright import APP_NAME, __version__, config, deps, fetch, help_content, launch, procs, theme, widgets
from tapewright.convert_tab import ConvertTab
from tapewright.help_tab import HelpTab
from tapewright.settings_tab import SettingsTab
from tapewright.setup_screen import SetupScreen

POLL_MS = 60
# The window's default and smallest sizes at 96 DPI, scaled to the screen's. tests/test_core.py checks that
# the Settings tab fits the default one.
WINDOW = (900, 760)
SMALLEST = (720, 680)
# How long closing waits for a helper being put in place. Two renames and a delete take a moment, so a wait
# longer than this means something is stuck.
SWAP_WAIT_S = 10
# The name the Windows installer's AppMutex looks for, so it waits for Tapewright to close rather than
# replacing files a running copy is using.
APP_MUTEX = "Tapewright.Running"
_app_mutex = []  # its handle, which nothing ever closes: the name belongs to this process until it ends
# The ID the installer's shortcuts carry too, so the taskbar groups the open window with them, and with
# a copy of one pinned there, rather than with pythonw.exe.
APP_USER_MODEL_ID = "AngrySandhill.Tapewright"


class App:
    def __init__(self, root, settings):
        self.root = root
        self.settings = settings
        self.deps = {}                # key -> deps.Dep from the latest check
        self.release = None           # {"version", "url"} when a newer Tapewright is out
        self.checking = False
        self.last_check_online = False
        self.updating = None          # key of the Dep being updated, or None
        self.active_jobs = set()      # tabs with a conversion running
        # fn(event, dep, *details), called on the main thread as a batch of updates runs:
        # ("start", dep, index, total), ("line", dep, text), ("done", dep, ok, message),
        # then ("finished", None, cancelled)
        self.update_listeners = []
        self.setup_shown = False
        self._setup_put_off = False   # [Not now] was clicked, so the screen doesn't come back by itself
        self._setup_requested = False  # it shows because someone clicked [Run setup again], not by itself
        self._checked_once = False
        self._after_check = []        # callbacks waiting on the check in progress
        self._update_queue = []
        self._update_runner = None
        self._update_cancelled = False
        self._update_index = self._update_total = 0
        self._auto_updated = False
        self._recheck_online = None   # a check asked for while another was running
        self._batch_recheck_online = False  # a check asked for during a batch, folded into the one after it
        # True from the fetcher's deps.SWAP_STEP line until that run ends. A kill between its two renames
        # would leave the helper missing, so Cancel does nothing then and closing waits for the run.
        self.update_swapping = False
        self._events = queue.Queue()
        # Seams for tests: all three are modal and would otherwise block a scripted run.
        self.ask_outdated = widgets.ask_outdated
        self.confirm = lambda title, message: messagebox.askyesno(title, message, parent=self.root)
        self.inform = lambda title, message: messagebox.showinfo(title, message, parent=self.root)

        theme.apply(root)  # before the first widget: styles only reach widgets created after it
        root.title(f"{APP_NAME} {__version__}")
        scale = root.winfo_fpixels("1i") / 96.0
        width = min(int(WINDOW[0] * scale), root.winfo_screenwidth() - int(40 * scale))
        height = min(int(WINDOW[1] * scale), root.winfo_screenheight() - int(90 * scale))
        root.geometry(f"{width}x{height}")
        # Tall enough for the tape deck and a few lines of log, but never taller than the screen.
        root.minsize(int(SMALLEST[0] * scale),
                     min(int(SMALLEST[1] * scale), root.winfo_screenheight() - int(90 * scale)))

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
        self.setup_screen = SetupScreen(root, self)  # packed in place of the notebook while it shows
        self.settings_tab.logview.followers.append(self.setup_screen.logview)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(POLL_MS, self._drain)
        if settings.problem:
            self.settings_tab.log(settings.problem)
        if not settings["setup_done"]:
            # At once, while the check runs, so a first launch never opens on tabs that can't work yet.
            self.show_setup()
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

    def converting(self):
        """True while a conversion runs, or waits for the gate to decide whether it may start."""
        return bool(self.active_jobs) or any(tab.pending for tab in self.tabs())

    # ------------------------------------------------------------ the setup screen

    def show_setup(self):
        """Put the setup screen in place of the tabs.

        pack_forget rather than covering the notebook, so no hidden tab or box can take the focus.
        """
        if not self.setup_shown:
            self.notebook.pack_forget()
            self.setup_screen.pack(fill="both", expand=True, padx=8, pady=8)
            self.setup_shown = True
        self.setup_screen.refresh()
        self.setup_screen.focus_primary()

    def request_setup(self):
        """Show the setup screen because someone asked: [Run setup again] on the Settings tab or in Help.

        Refused, with nothing shown, while a conversion runs or waits to start. The screen takes the tabs'
        place, so the job would go on out of sight, and the screen's own install would be refused because of
        it. The caller says why where it has room to. The screen that opens by itself at startup doesn't come
        through here.
        """
        if self.converting():
            return False
        self._setup_requested = True
        self.show_setup()
        return True

    def hide_setup(self, put_off=False):
        # Both Run setup again buttons are on the tabs, which the screen replaces, so nobody can ask for it
        # while it shows, and one flag is enough.
        self._setup_requested = False
        if put_off:
            self._setup_put_off = True
        if self.setup_shown:
            self.setup_screen.pack_forget()
            self.setup_screen.pause()
            self.notebook.pack(fill="both", expand=True, padx=8, pady=8)
            self.setup_shown = False

    def finish_setup(self):
        """Save, once, that setup is finished."""
        if not self.settings["setup_done"]:
            self.settings["setup_done"] = True
            self.save_settings()

    def show_help(self, title):
        self.hide_setup()
        self.notebook.select(self.help_tab)
        self.help_tab.show_topic(title)

    def _first_check_finished(self, found):
        """Keep the setup screen only when it has something to install.

        With nothing to install, setup is done with for good, which is how someone who already has every
        helper never sees it again. yt-dlp or FFmpeg going missing later still brings it back
        (deps.setup_needed), unless [Not now] put it off in this session. Only the screen that opened by
        itself goes: one someone opened with [Run setup again] while this check ran was asked for, and
        vanishing under them would say nothing about why.
        """
        if not deps.setup_needed(found, self.settings):
            if not self._setup_requested:
                self.hide_setup()
            self.finish_setup()
        elif not self._setup_put_off and not self.converting():
            # Never over a conversion that runs or waits on this check to start (see request_setup).
            self.show_setup()

    # ------------------------------------------------------------ dependency checks

    def check_dependencies(self, online=True, then=None):
        if then is not None:
            self._after_check.append(then)
        if self.checking:
            return
        self.checking = True
        snapshot = copy.deepcopy(self.settings.data)
        self.settings_tab.on_check_started(online)
        self.setup_screen.on_check_started(online)

        def work():
            found, updates = deps.check_all(snapshot, online=online)
            self.post(self._check_finished, found, updates, online)
            # Asked only once the tools' answer is on its way, so a slow or unreachable GitHub never keeps
            # a check running, which holds back a conversion. Kept out of found, as deps.check_release says.
            # Offline it only reads the cache. _release_finished works the notice out again from the live
            # cache, so all that goes with it is what this check learned and the cache entry it read.
            cache = snapshot.get("latest_cache") or {}
            news = deps.check_release(cache, online)[1]
            self.post(self._release_finished, news, cache.get("tapewright"))

        threading.Thread(target=work, daemon=True).start()

    def recheck(self, online):
        """Check again with the current settings, even while a check with the old ones runs.

        During a batch it waits for the check after the batch, which then goes online if any request
        wanted it to. A check now would read a tool the batch is about to replace, or hold its file open
        while the fetcher renames its folder.
        """
        if self.updating:
            self._batch_recheck_online = self._batch_recheck_online or online
        elif self.checking:
            self._recheck_online = bool(self._recheck_online) or online
        else:
            self.check_dependencies(online=online)

    def _check_finished(self, found, updates, online):
        self.checking = False
        if updates:  # the latest versions it looked up are still news, whatever else happened since
            self.settings["latest_cache"].update(updates)
            self.save_settings()
        if self._recheck_online is not None:
            # Another check was asked for while this one ran, because something changed after it read the
            # tools: a setting, or a batch that replaced one. Shown, its answer would put a tool that is
            # really there as missing in front of the gate, and the setup screen would call a good install a
            # failure. So it goes nowhere, and the fresh check does everything this one would have, waiting
            # callbacks included.
            again, self._recheck_online = self._recheck_online, None
            self.check_dependencies(online=again)
            return
        self.last_check_online = online
        self.deps = found
        self.settings_tab.on_deps_changed()
        for tab in self.tabs():
            tab.on_deps_changed()
        self.setup_screen.on_deps_changed()
        if not self._checked_once:
            self._checked_once = True
            self._first_check_finished(found)
        waiting, self._after_check = self._after_check, []
        for fn in waiting:
            fn()
        if waiting:
            # A start that waited on this check has been decided now, but the Settings tab drew its buttons
            # while it still waited, so Run setup again would stay disabled after a start that was declined.
            self.settings_tab.on_busy_changed()
        ytdlp = found.get("yt-dlp")
        if (online and self.settings["auto_update_ytdlp"] and not self._auto_updated and ytdlp
                and ytdlp.state in (deps.OUTDATED, deps.MISSING) and ytdlp.command
                and not self.active_jobs and not self.updating):
            self._auto_updated = True
            self.settings_tab.log("Automatic yt-dlp updates are on, so updating it now.")
            self.run_updates(["yt-dlp"], confirm=False)

    def _release_finished(self, updates, before):
        """Merge what a release check learned, then show what the live cache says. before is the cache entry
        that check read.

        Checks can overlap here, since each asks GitHub only after its tools' answer is posted: one started
        in between reads the cache before the earlier one's answer lands, and the answers arrive in either
        order. Shown as it came, an overtaken check's answer would hide a newer release. So the notice is
        worked out from the cache after merging, and a failure is merged only when no other answer has landed
        since this check read the cache, or it would put its stale last answer, fresh for an hour, over a
        real one.
        """
        cache = self.settings["latest_cache"]
        if (updates.get("tapewright") or {}).get("failed") and cache.get("tapewright") != before:
            updates = {key: value for key, value in updates.items() if key != "tapewright"}
        if updates:
            cache.update(updates)
            self.save_settings()
        self.release = deps.check_release(cache, False)[0]  # offline, so it only reads
        self.settings_tab.on_release_changed()

    # ------------------------------------------------------------ the conversion gate

    def request_start(self, tab, kind, start):
        """Decide whether a conversion may start, and call start() if it may.

        Order matters. An update in progress refuses outright, since the tool is being
        replaced underneath. A check in progress defers, so a click in the first second
        after launch cannot slip past the warning. A setup screen that took the tabs' place
        while the start waited declines it. Then any problem with a tool this job actually
        uses gets the hard warning.
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
            # Run setup again is refused while a start waits, so the Settings tab shows it disabled now.
            self.settings_tab.on_busy_changed()
            return
        if self.setup_shown:
            # A job started behind the setup screen would run out of sight (see request_setup).
            tab.start_declined()
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
        self._update_index, self._update_total = 0, len(todo)
        self._next_update()
        return True

    def _notify(self, event, dep, *details):
        for listener in list(self.update_listeners):
            try:
                listener(event, dep, *details)
            except Exception:
                # Logged and passed over: the next tool, the end of the batch and the check after it all
                # come after this call, and a listener's bug must not stop them.
                self.settings_tab.log("Internal error:\n" + traceback.format_exc())

    def _next_update(self):
        if not self._update_queue:
            if self.updating and not self._update_cancelled:
                # Each tool logs its own "done", so the end of the whole batch needs a line too:
                # the Help tab tells people to wait for this one before converting.
                self.settings_tab.log(help_content.UPDATES_FINISHED + ".")
            cancelled = self._update_cancelled
            self.updating = None
            self._update_runner = None
            self.settings_tab.on_busy_changed()
            self._notify("finished", None, cancelled)
            # Installed versions changed; the latest ones were just fetched and are cached. recheck rather
            # than check_dependencies, because a check still running read the tools before this batch
            # replaced them, and a fresh one has to follow it. A check asked for during the batch goes
            # online here if it wanted to.
            online, self._batch_recheck_online = self._batch_recheck_online, False
            self.recheck(online)
            return
        dep = self._update_queue.pop(0)
        runner = procs.Runner()
        self.updating, self._update_runner = dep.key, runner
        self._update_index += 1
        self.settings_tab.on_busy_changed()
        self.settings_tab.log(f"{dep.action or 'Update'}: {dep.label}")
        self.settings_tab.log("$ " + procs.format_command(dep.command))
        self._notify("start", dep, self._update_index, self._update_total)
        fetching = deps.is_fetch(dep.command)

        def work():
            # The fetcher ends a failure with an ERROR sentence, which says more than its exit code. It is
            # kept here, by this run's own thread, so it can never be mistaken for another run's.
            errors = []
            swapped = False  # whether this run said it was putting the helper in place

            def on_line(line):
                nonlocal swapped
                parsed = deps.parse_fetch_line(line) if fetching else None
                if parsed and parsed[0] == "error":
                    errors.append(parsed[1])
                elif parsed == ("step", deps.SWAP_STEP):
                    # Set as the line is read, not when the main thread gets to it: a Cancel clicked
                    # in between would kill the fetcher mid-rename. The main thread only reads it, and
                    # _update_finished, which this run posts after its last line, clears it.
                    self.update_swapping = swapped = True
                self.post(self._tool_line, dep, line)

            try:
                code = runner.run(dep.command, on_line)
                self.post(self._update_finished, dep, code, "", errors[-1] if errors else "")
            except procs.Cancelled:
                if fetching:
                    try:
                        for note in _recover_cancelled_fetch(dep.command):
                            self.post(self._tool_line, dep, note)
                    except Exception:  # never at the cost of the batch ending, which comes after it
                        self.post(self.settings_tab.log, "Internal error:\n" + traceback.format_exc())
                self.post(self._update_finished, dep, None, "cancelled", "", swapped)
            except Exception as e:
                self.post(self._update_finished, dep, None, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _tool_line(self, dep, line):
        self.settings_tab.log_tool_line(line)
        if self.update_swapping:
            self.settings_tab.on_busy_changed()  # its Cancel stays disabled until this run ends
        self._notify("line", dep, line)

    def _update_finished(self, dep, code, error, last_error="", swapped=False):
        self.update_swapping = False
        if error == "cancelled":
            message = deps.cancel_text(dep, swapped)
            self._update_queue.clear()
            self._update_cancelled = True
        elif error:
            message = error
        elif code == 0:
            message = "done."
        else:
            message = deps.update_failure_text(dep, code, last_error)
        self.settings_tab.log(f"{dep.label}: {message}")
        self._notify("done", dep, code == 0 and not error, message)
        self._next_update()

    def cancel_update(self):
        """Stop the update running now and skip the rest of its batch.

        Returns True when it did, and False when it ignored the click, because nothing is running or the
        fetcher is putting a helper in place. SetupScreen.cancel says why it goes by this answer alone.
        """
        if self.update_swapping:
            # See update_swapping. The click does nothing, so nothing queued is skipped.
            return False
        runner = self._update_runner
        if runner is None:
            return False
        if self._update_queue:
            # Said here, because the tool running now may already have finished: its "done"
            # would otherwise be followed by "Finished updating" for a batch that wasn't.
            self.settings_tab.log("Update cancelled. Not updated: " + ", ".join(
                f"the {d.label}" for d in self._update_queue) + ".")
            self._update_cancelled = True
        self._update_queue.clear()
        threading.Thread(target=runner.cancel, daemon=True).start()
        return True

    # ------------------------------------------------------------ closing

    def on_close(self):
        if self.update_swapping:
            pass  # moments from done, and no answer could stop it safely, so nothing is asked: shutdown waits
        elif self.updating:
            dep = self.deps[self.updating]
            # The fetcher replaces nothing before its last step, so for a download quitting only costs the
            # download. pip and winget make no such promise.
            risk = ("Quitting now stops it, and nothing is replaced." if deps.is_fetch(dep.command)
                    else "Quitting now can leave it half-installed and broken.")
            if not self.confirm(APP_NAME, f"The {dep.label} is still being updated. {risk}\n\nQuit anyway?"):
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
        runner = self._update_runner
        # A fetcher putting a helper in place is left to finish (see update_swapping). Past SWAP_WAIT_S
        # something is stuck, and a closed window must still leave nothing running; the next download's
        # recover() puts back what that kill left.
        if runner is not None and not (self.update_swapping and runner.wait(SWAP_WAIT_S)):
            runner.cancel()
        self.save_settings()
        self.root.destroy()


def _recover_cancelled_fetch(command):
    """Put right, at once, what a cancelled fetcher left behind. Returns what was done, as lines for the log.

    A kill on its way before the fetcher said it was putting the helper in place can still land between its
    two renames, which leaves no helper at all until its next run's recover() puts the old copy back. So that
    is done now, before the check after the batch looks. The tool's lock comes first, as in the fetcher: when
    another window holds it, that run is using the same staging names, and its own recover() already ran.
    command is deps.fetch_command's: the tool, then its version, then the folder.
    """
    if len(command) < 5:
        return []
    tool, tools_dir = str(command[2]), str(command[4])
    try:
        descriptor = fetch.lock(tools_dir, tool)
    except OSError:  # no folder, so nothing in it to put right, and none is made
        return []
    if descriptor is None:
        return []
    try:
        return fetch.recover(tools_dir, tool)
    except OSError as e:
        return [f"{tools_dir}: {e}"]
    finally:
        fetch.unlock(descriptor)


def _enable_dpi_awareness():
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass  # older Windows: the window is merely blurry


def _set_app_user_model_id():
    """Give this process the ID the installer's shortcuts carry, so the taskbar groups its window with them.

    Microsoft's documentation asks for it before a program shows anything, so main() calls this before the
    window exists.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass  # only the taskbar's grouping is lost


def _hold_app_mutex():
    """Name this process as a running Tapewright, so the Windows installer knows to wait for it to close.

    Windows lets go of the name when the process ends, however it ends, so the handle is never closed here.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, APP_MUTEX)
    except Exception:
        return  # only the installer would notice, by not seeing that Tapewright is open
    if handle:
        _app_mutex.append(handle)


def main():
    """Open the window. Start through launch.main(), which first checks this Python can run it."""
    _enable_dpi_awareness()
    _set_app_user_model_id()
    settings = config.Settings()
    settings.load()
    try:
        root = tk.Tk()
    except tk.TclError as e:  # tkinter imported, but Tcl/Tk itself is broken, or there is no display
        launch.explain(f"Tkinter couldn't open a window ({e}).", launch.tk_failure_fix(str(e)))
        return 1
    _hold_app_mutex()
    App(root, settings)
    root.mainloop()
    return 0
