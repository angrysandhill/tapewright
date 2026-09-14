# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""The To MP3 and To MP4 tabs: one class, told which target it is."""

import datetime
import os
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, ttk

from tapewright import config, deps, jobs, procs, widgets

_VIDEO = "*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.flv *.wmv *.mpg *.mpeg *.ts *.3gp"
_AUDIO = "*.m4a *.aac *.wav *.flac *.ogg *.opus *.wma *.mp3"
FILETYPES = {
    "mp3": [("Video and audio files", f"{_VIDEO} {_AUDIO}"), ("All files", "*.*")],
    "mp4": [("Video files", _VIDEO), ("All files", "*.*")],
}
# Spelled out in full rather than assembled: the Help tab quotes these, and its test looks for
# them word for word in the source.
START_LABELS = {"mp3": "Convert to MP3", "mp4": "Convert to MP4"}
COVER_LABELS = {"mp3": "Add the thumbnail as cover art (links)", "mp4": "Embed the thumbnail (links)"}


class ConvertTab(ttk.Frame):
    def __init__(self, master, app, target):
        super().__init__(master, padding=12)
        self.app = app
        self.target = target
        s = app.settings
        self.source = tk.StringVar()
        self.out_dir = tk.StringVar(value=s[f"{target}_out_dir"] or str(config.default_download_dir()))
        self.thumbnail = tk.BooleanVar(value=s[f"{target}_thumbnail"])
        self.playlist = tk.BooleanVar(value=s[f"{target}_playlist"])
        self.status = tk.StringVar(value="Paste a link, or choose a file.")
        self.runner = None       # set while a job runs; events from any other runner are stale
        self.pending = False     # Start was pressed and the gate has not decided yet
        self.files = []
        self._build()
        self._set_running(False)

    # ------------------------------------------------------------ layout

    def _build(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(8, weight=1)

        self.banner = widgets.Banner(self, self.app.show_settings)
        self.banner.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        self.banner.grid_remove()

        ttk.Label(self, text="Link or file:").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.source_entry = ttk.Entry(self, textvariable=self.source)
        self.source_entry.grid(row=1, column=1, sticky="ew")
        self.source_entry.bind("<Return>", lambda e: self.start())
        widgets.add_edit_menu(self.source_entry)
        self.browse_button = ttk.Button(self, text="Browse…", command=self.browse_source)
        self.browse_button.grid(row=1, column=2, padx=(8, 0))
        what = "a video or audio file" if self.target == "mp3" else "a video file"
        ttk.Label(self, text=f"Paste a link (YouTube and most video sites), or choose {what} on this PC.",
                  foreground="#666").grid(row=2, column=1, sticky="w", pady=(2, 10))

        ttk.Label(self, text="Save to:").grid(row=3, column=0, sticky="w", padx=(0, 8))
        self.out_entry = ttk.Entry(self, textvariable=self.out_dir)
        self.out_entry.grid(row=3, column=1, sticky="ew")
        widgets.add_edit_menu(self.out_entry)
        self.out_button = ttk.Button(self, text="Choose…", command=self.browse_out)
        self.out_button.grid(row=3, column=2, padx=(8, 0))

        options = ttk.LabelFrame(self, text="Options", padding=10)
        options.grid(row=4, column=0, columnspan=3, sticky="ew", pady=12)
        options.columnconfigure(1, weight=1)
        self.option_widgets = []
        if self.target == "mp3":
            self.quality = self._combo(options, 0, "Quality:", jobs.MP3_QUALITY,
                                       {k: v[0] for k, v in jobs.MP3_QUALITY.items()},
                                       self.app.settings["mp3_quality"])
        else:
            self.mode = self._combo(options, 0, "Format:", jobs.MP4_MODE, jobs.MP4_MODE,
                                    self.app.settings["mp4_mode"])
            self.height = self._combo(options, 1, "Max resolution:", jobs.MP4_HEIGHT, jobs.MP4_HEIGHT,
                                      self.app.settings["mp4_max_height"])
            ttk.Label(options, text="Resolution applies to links. A local file keeps its own "
                                    "size, and is copied as is when its codecs allow.",
                      foreground="#666").grid(row=2, column=1, sticky="w", pady=(0, 6))
        checks = ttk.Frame(options)
        checks.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        for text, var in ((COVER_LABELS[self.target], self.thumbnail),
                          ("Download the whole playlist when the link is one", self.playlist)):
            box = ttk.Checkbutton(checks, text=text, variable=var)
            box.pack(side="left", padx=(0, 18))
            self.option_widgets.append(box)

        buttons = ttk.Frame(self)
        buttons.grid(row=5, column=0, columnspan=3, sticky="ew")
        self.start_button = ttk.Button(buttons, text=START_LABELS[self.target], command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self.cancel)
        self.cancel_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="Show in folder", command=self.open_folder).pack(side="left")
        ttk.Button(buttons, text="Copy log", command=self.copy_log).pack(side="right")

        self.progress = ttk.Progressbar(self, maximum=100)
        self.progress.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 4))
        self.status_label = ttk.Label(self, textvariable=self.status, anchor="w", justify="left")
        self.status_label.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.bind("<Configure>", lambda e: self.status_label.configure(wraplength=max(e.width - 30, 200)))

        self.logview = widgets.LogView(self, height=10)
        self.logview.grid(row=8, column=0, columnspan=3, sticky="nsew")

    def _combo(self, parent, row, label, keys, labels, current):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
        box = ttk.Combobox(parent, state="readonly", values=[labels[k] for k in keys], width=60)
        box.set(labels.get(current, labels[next(iter(keys))]))
        box.grid(row=row, column=1, sticky="w", pady=3)
        box.keys = list(keys)
        box.labels = labels
        self.option_widgets.append(box)
        return box

    @staticmethod
    def _key(box):
        return box.keys[box.current()] if box.current() >= 0 else box.keys[0]

    # ------------------------------------------------------------ state

    def set_status(self, text, error=False):
        self.status.set(text)
        self.status_label.configure(foreground=widgets.WARNING_RED if error else "")

    def log(self, text):
        for line in str(text).splitlines() or [""]:
            self.logview.append(line)

    def _set_running(self, running):
        busy = running or self.pending
        for widget in (self.start_button, self.source_entry, self.browse_button, self.out_entry,
                       self.out_button):
            widget.state(["disabled"] if busy else ["!disabled"])
        for widget in self.option_widgets:
            if isinstance(widget, ttk.Combobox):
                widget.state(["disabled"] if busy else ["!disabled", "readonly"])
            else:
                widget.state(["disabled"] if busy else ["!disabled"])
        self.cancel_button.state(["!disabled"] if busy else ["disabled"])

    def _set_progress(self, fraction):
        if fraction is None:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(15)
            return
        if str(self.progress.cget("mode")) != "determinate":
            self.progress.stop()
            self.progress.configure(mode="determinate")
        self.progress["value"] = max(0.0, min(fraction, 1.0)) * 100

    def on_deps_changed(self):
        self.banner.set_problems(deps.problems_for(self.app.deps, "url"))

    # ------------------------------------------------------------ actions

    def start(self):
        if self.runner is not None or self.pending:
            return
        kind, value = jobs.classify(self.source.get())
        if kind == "error":
            self.set_status(value, error=True)
            return
        out = Path(self.out_dir.get().strip() or config.default_download_dir()).expanduser()
        self.pending = True
        self._set_running(False)
        self.set_status("Starting…")
        self.app.request_start(self, kind, lambda: self._launch(kind, value, out))

    def start_declined(self):
        self.pending = False
        self._set_running(False)
        self.set_status("Not started.")

    def _launch(self, kind, source, out):
        self.pending = False
        s = self.app.settings
        ffmpeg = self.app.deps.get("ffmpeg")
        js = self.app.deps.get("js")
        job = jobs.Job(
            target=self.target, kind=kind, source=str(source), out_dir=out,
            ffmpeg=ffmpeg.path if ffmpeg and ffmpeg.path else "ffmpeg",
            ffprobe=ffmpeg.ffprobe if ffmpeg and ffmpeg.ffprobe else "ffprobe",
            playlist=self.playlist.get(), thumbnail=self.thumbnail.get(),
            js_args=list(js.js_args) if js else [],
        )
        s[f"{self.target}_out_dir"] = self.out_dir.get().strip()
        s[f"{self.target}_thumbnail"] = self.thumbnail.get()
        s[f"{self.target}_playlist"] = self.playlist.get()
        if self.target == "mp3":
            job.mp3_quality = s["mp3_quality"] = self._key(self.quality)
        else:
            job.mp4_mode = s["mp4_mode"] = self._key(self.mode)
            job.mp4_max_height = s["mp4_max_height"] = self._key(self.height)
        self.app.save_settings()

        tool_dirs = (ffmpeg.tool_dirs if ffmpeg else []) + (js.tool_dirs if js else [])
        runner = self.runner = procs.Runner(tool_dirs)
        self.files = []
        self._set_running(True)
        self._set_progress(None)
        self.log(f"—— {datetime.datetime.now():%H:%M:%S}  {source}")

        def emit(event, value):
            self.app.post(self._on_event, runner, event, value)

        def work():
            try:
                result = jobs.run_job(job, runner, emit)
            except Exception:
                emit("log", traceback.format_exc())
                result = jobs.Result(False, "Internal error; the details are in the log.")
            self.app.post(self._on_done, runner, result)

        threading.Thread(target=work, daemon=True).start()

    def _on_event(self, runner, event, value):
        if runner is not self.runner:
            return
        if event == "log":
            self.log(value)
        elif event == "status":
            self.set_status(value)
        elif event == "progress":
            self._set_progress(value)
        elif event == "file":
            self.files.append(Path(value))

    def _on_done(self, runner, result):
        if runner is not self.runner:
            return
        self.runner = None
        self._set_progress(1.0 if result.ok else 0.0)
        if result.ok:
            self.set_status("✔  " + result.message)
        elif result.cancelled:
            self.set_status("Cancelled.")
        else:
            saved = f" ({len(result.files)} file(s) were saved before that.)" if result.files else ""
            self.set_status("✖  " + result.message + saved, error=True)
        self.log(("Done: " if result.ok else "Stopped: ") + result.message)
        self._set_running(False)
        self.app.job_finished(self)

    def cancel(self, wait=False):
        if self.pending and self.runner is None:
            self.start_declined()
            self.set_status("Cancelled.")
            return
        runner = self.runner
        if runner is None:
            return
        self.set_status("Cancelling…")
        self.cancel_button.state(["disabled"])
        if wait:
            runner.cancel()
        else:
            threading.Thread(target=runner.cancel, daemon=True).start()

    def browse_source(self):
        title = "Choose a file to convert to " + self.target.upper()
        path = filedialog.askopenfilename(parent=self, title=title, filetypes=FILETYPES[self.target])
        if path:
            self.source.set(os.path.normpath(path))

    def browse_out(self):
        path = filedialog.askdirectory(parent=self, title="Save to", initialdir=self.out_dir.get())
        if path:
            self.out_dir.set(os.path.normpath(path))

    def open_folder(self):
        target = next((f for f in reversed(self.files) if f.exists()), None)
        folder = Path(self.out_dir.get().strip() or config.default_download_dir()).expanduser()
        try:
            if os.name == "nt":
                if target:
                    # explorer parses its own command line: /select,"path" must stay one string.
                    subprocess.Popen(f'explorer /select,"{target}"')
                else:
                    os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(target)] if target else ["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(target.parent if target else folder)])
        except OSError as e:
            self.set_status(f"Couldn't open the folder: {e}", error=True)

    def copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.logview.contents())
        self.set_status("Log copied to the clipboard.")
