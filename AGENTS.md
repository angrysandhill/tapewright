# AGENTS.md

Guidance for anyone, human or agent, changing this repository. `README.md` describes what
the app does; this file covers what must stay true while you change it.

## Shape

The half with no window is `jobs.py`, `deps.py`, `procs.py`, `versions.py`, `config.py` and
`help_content.py`. None of them imports tkinter, so all of it runs headless and is what the
tests drive. The window half is `app.py`, `convert_tab.py`, `settings_tab.py`, `help_tab.py`
and `widgets.py`.

It uses the standard library only, deliberately. External tools are run as programs, never
imported.

## Talk to the tools through their command lines

- **Never `import yt_dlp`.** Run `python -m yt_dlp`. yt-dlp's Python options are
  undocumented and get renamed between releases, while its CLI flags keep deprecated
  aliases. A long-running process that imported yt-dlp would also keep the old code after
  the Settings tab updated it. The same goes for `deps.py`: `importlib.util.find_spec` only
  locates the package, it does not import it.
- **`jobs.py` owns every flag.** When a yt-dlp or FFmpeg release changes one, change it
  there, and update its checks in `tests/test_core.py` in the same commit.

## Invariants

Each of these was measured failing, or was verified against the real tool, while the app
was built.

- **Python children get `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`** (`procs.child_env`).
  On Windows' ANSI code page, yt-dlp *drops* the characters it can't encode from the paths
  it prints. There is no replacement character; the bytes are just missing. A title
  containing a full-width `｜` came back as a path that doesn't exist.
- **`PYTHONUNBUFFERED=1` too.** A Python child writing to a pipe block-buffers, and the
  progress then arrives all at once at the end, which looks like a hang.
- **Arguments are lists and `shell=False`.** Any YouTube URL with `&list=` breaks under
  `cmd.exe`, and pasted text must never reach a shell.
- **Cancel kills the whole tree** (`taskkill /F /T` on Windows, `killpg` elsewhere). During
  a link-to-MP3 conversion the tree is python → ffmpeg + conhost. Terminating only the
  Popen leaves FFmpeg writing after the UI says "Cancelled". The child also gets its own
  process group, so nothing else will ever stop it: every exit path goes through
  `App.shutdown()`.
- **Starting a process and cancelling share one lock** (`procs.Runner`). Without it, a
  cancel can land between "checked the flag" and "Popen returned", and the process starts
  with nothing left to kill it. This matters because local jobs run ffprobe and then up to
  two ffmpeg passes.
- **`--print` implies `--quiet` unless `--no-quiet` is given, and quiet hides progress.**
  The finished path comes from `--print after_move:...`, so both flags are needed.
- **In the MP4 sort order the resolution cap goes before `quality`.** `-t mp4`'s order puts
  `quality` first. YouTube's quality ranking follows resolution, so a cap in the preset's
  position does nothing: `res:360` there downloaded 1080p.
- **A local output file is reserved by exclusive create** (`jobs.reserve_output`) before
  FFmpeg writes it. That makes cleanup after a failure or cancel safe by construction: the
  job can only delete a file it created. That includes never touching the source when an
  MP4 is converted to MP4 in its own folder.
- **yt-dlp leftovers are removed only if yt-dlp named them and they didn't exist at that
  moment** (`jobs.remove_unfinished`). The names come from human-readable log lines, so a
  wording change can only make cleanup miss a file, never delete a wrong one.
  `--no-post-overwrites` stops a repeat download from converting over a good file.
- **pip installs are pinned `==version` whenever the check found one**
  (`deps.ytdlp_install_command`). "Update" then installs exactly what the check reported.
  Pinning is also the only way to go from a nightly back to stable, because a plain
  `--upgrade` never downgrades. Offline with nothing cached, there is no version to pin.
- **A check never raises, and "can't tell" is never "outdated."** A failed check becomes
  `UNKNOWN`. `versions.is_newer` returns False whenever either side doesn't parse, since a
  false "outdated" puts a hard warning in front of every conversion.
- **`versions.release_date` requires a year of 2000 or later.** Otherwise `2.9.6` is a
  valid date.
- **Tools are found with a fallback past `PATH`** (`deps.find_tool`). A winget upgrade of
  FFmpeg moves it to a new versioned folder and rewrites `PATH` in the registry, which an
  already-running app never sees. Jobs then receive the exact path the check found, through
  `--ffmpeg-location` and `--js-runtimes NAME:PATH`. yt-dlp splits that on the first colon,
  so a Windows drive letter is safe.
- **The JavaScript runtime minimums in `deps.JS_MINIMUM` mirror `MIN_SUPPORTED_VERSION` in
  yt-dlp's `utils/_jsruntime.py`** (as of 2026.08.19). Re-check them when yt-dlp's release
  notes mention runtimes.
- **Only the Tk main thread touches widgets.** Workers call `App.post()`. Every job event
  carries its `Runner`, and events from any runner other than the tab's current one are
  dropped, so a late event from a finished job can't repaint the next one.
- **The conversion gate runs in this order:** an update in progress refuses, a check in
  progress defers the start, and only then does the warning run. Test-and-set happens on the
  main thread. A click in the first second after launch must not slip past the warning, and
  a tool must not be replaced while a job is using it. `App.run_updates` refuses while a job
  runs, for the same reason.
- **The warning dialog's default button is never "Convert anyway".** Going ahead with a
  broken tool has to be a deliberate click.
- **An unreadable settings file is renamed to `settings.json.bad`, not overwritten.** It may
  have been edited by hand.
- **The Help tab only names things that are really on screen.** Its text marks each button,
  tab and box as `[Label]`, and `tests/test_core.py` fails when a label no longer appears in
  the source. Rename a button and its help in the same change, and keep quoted labels as whole
  string literals (`START_LABELS`, `COVER_LABELS`), not assembled with f-strings the test
  cannot see. Topic titles stay within 30 characters, because the question list cannot wrap.
- **Every box the Help tab mentions has a right-click menu and Ctrl+A**
  (`widgets.add_edit_menu`). Tk entries come with neither, and the Help tab tells people to
  right-click and paste.

## Licensing

Tapewright is `GPL-3.0-or-later`, and the text is in `LICENSE`. Every source file starts with
the same two lines, and `tests/test_core.py` fails when a file is missing them:

    # SPDX-FileCopyrightText: 2026 AngrySandhill
    # SPDX-License-Identifier: GPL-3.0-or-later

Running yt-dlp, FFmpeg and deno as separate programs, and never shipping them, is also why
their licenses don't reach this code. A release that bundles one of them, such as a
one-click `.exe` with `ffmpeg.exe` inside, has to include that tool's license and meet its
terms. For FFmpeg those depend on the build: `ffmpeg -version` lists `--enable-gpl` for a GPL
build, and a build with `--enable-nonfree` can't be redistributed at all.

## Testing

```
python -m unittest discover -s tests -v
```

The unit tests need no window, no network and no installed tools. There is no automated
end-to-end suite yet. To drive the real window against the real tools from a script, replace
the modal pieces (`app.ask_outdated`, `app.confirm` and `app.inform` are attributes for
exactly this reason) and set `TAPEWRIGHT_CONFIG_DIR` to a scratch folder so the run never
touches real settings. Test the pip update path inside a throwaway venv whose `python.exe`
runs the app, since the app installs into whichever Python runs it.

## Known gaps

- A standalone `yt-dlp.exe` is not used, by design. The app manages yt-dlp as a pip package
  in its own Python, so there is one update path, and it is tested.
- The FFmpeg and deno update buttons have been run by hand (on 2026-09-13: FFmpeg 8.1.1 to
  9.0.1 through winget, deno 2.8.3 to 2.9.6 through `deno upgrade`). Nothing exercises them
  automatically, since running one changes the machine.
- On macOS and Linux, FFmpeg is only checked for presence, because a distribution's version
  lags upstream on purpose.
- There is no GPU encoding and no drag-and-drop: Tk has none without the tkdnd extension.
- The Help tab's wording assumes Windows: File Explorer, the yellow folder on the taskbar,
  Windows+E. On macOS or Linux the steps are right but some of the names are not.
