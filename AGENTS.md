# AGENTS.md

Guidance for anyone, human or agent, changing this repository. `README.md` describes what
the app does; this file covers what must stay true while you change it.

## Shape

The half with no window is `jobs.py`, `deps.py`, `procs.py`, `versions.py`, `config.py`,
`help_content.py` and `launch.py`. None of them imports tkinter when it is imported (`launch.py`
tries to inside `problem()`, to say when it is missing, and `launch.main()` hands over to
`app.py`), so all of it runs headless. The window half is `app.py`, `convert_tab.py`,
`settings_tab.py`, `help_tab.py`, `deck.py`, `theme.py` and `widgets.py`. The tests drive the
first half directly, test the pure functions in `deck.py` and `theme.py` (the reel geometry, the
contrast ratio) without a window, and build the real window withdrawn in the `Window` class.

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

Most of these were measured failing, or verified against the real tool, while the app was
built. Where one comes from a tool's documentation or source instead, it says so.

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
  The finished path comes from `--print after_move:...`, so both flags are needed. The
  cassette's title comes from `--print before_dl:...` rather than from a file name, whose format
  ids (`.f251`, `.fhls-1080p`) no pattern strips reliably.
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
  false "outdated" puts a hard warning in front of every conversion. The same goes for a
  JavaScript runtime whose version can't be read: it is `UNKNOWN`, not too old.
- **`versions.release_date` requires a year of 2000 or later.** Otherwise `2.9.6` is a
  valid date.
- **Tools are found with a fallback past `PATH`** (`deps.find_tool`). A winget upgrade of
  FFmpeg moves it to a new versioned folder and rewrites `PATH` in the registry, which an
  already-running app never sees. Jobs then receive the exact path the check found, through
  `--ffmpeg-location` and `--js-runtimes NAME:PATH`. yt-dlp splits that on the first colon,
  so a Windows drive letter is safe.
- **winget's `Packages` folders are searched, not only `Links`, and versioned folders compare as
  versions.** winget doesn't always make a `Links` entry for a portable package; on the PC this
  was built on, `Links` is empty and FFmpeg's own `bin` folder is on `PATH` instead. Sorted by
  name, `ffmpeg-9.0.1-full_build` beats `ffmpeg-10.0-full_build`. deno's winget package should
  keep `deno.exe` at the package folder's root: its manifest puts the exe at the zip's top, and
  Rojo's package on the same PC is laid out that way, but no winget deno was installed to check.
- **`ffprobe` is taken from `ffmpeg`'s own folder when it is there** (`deps.check_ffmpeg`), so
  the two come from one install whenever that install has both, and only then searched for
  separately. Without that search, FFmpeg would be called missing on a setup that works for local
  files, which blocks every conversion. yt-dlp only ever looks beside the ffmpeg given to
  `--ffmpeg-location`, so a separately found ffprobe serves local-file jobs alone.
- **winget runs with `--source winget --accept-source-agreements --accept-package-agreements`**
  beside `--disable-interactivity` (`deps._winget_command`). With interactivity off, winget
  can't ask a profile that never accepted its source's terms, and stops with 0x8A150046 instead
  (that code is from winget's `returnCodes.md`; the failure was not reproduced here). Exit codes
  are looked up after `& 0xFFFFFFFF`: Windows returns them unsigned, while winget's documentation
  lists each one both ways. An `install` also passes `--no-upgrade`: without it winget turns an
  install of a package it already has into an upgrade, and answers 0x8A15002B (no applicable
  update) rather than 0x8A150061 (already installed). That was read from winget-cli's
  `UpdateFlow.cpp`, not reproduced here.
- **"Try again later" is said only for a tool that is out of date** (`deps.update_failure_text`).
  For a tool that is behind, 0x8A15002B means winget hasn't caught up yet: its package list often
  trails gyan.dev's release by days. For anything else, such as the ffprobe gone from a current
  FFmpeg, waiting fixes nothing, and neither does reopening when winget keeps a record of a tool
  whose files are gone (0x8A150061). The update log says so, and names
  `winget uninstall --id <package>` as the way back, since nothing in the window can reinstall.
- **The JavaScript runtime minimums in `deps.JS_MINIMUM` mirror `MIN_SUPPORTED_VERSION` in
  yt-dlp's `utils/_jsruntime.py`** (as of 2026.08.19). Re-check them when yt-dlp's release
  notes mention runtimes.
- **The launchers parse and import on any Python 3.** `Tapewright.pyw`, `tapewright/__init__.py`,
  `tapewright/__main__.py` and `tapewright/launch.py` run before the version check can, and an
  error under `pythonw.exe` shows nothing at all. No f-strings, annotations (Python 3.9 evaluates
  `tuple[str, str] | None` at import), walrus, `match`, positional-only parameters, numeric
  underscores or parenthesized `with` in them. A test fails on those, though not on every newer
  construct.
- **A Python that can't run Tapewright says why.** `launch.main()` checks for Python 3.10 and
  Tkinter, and `app.main()` catches a Tk that imports but can't open a window, with advice chosen
  by the cause (`launch.tk_failure_fix`): reinstalling Python doesn't bring a display, or clear a
  stray `TCL_LIBRARY`. On Windows the explanation is a message box.
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
- **The confirmation before an update is in plain words** (`deps.describe_update`): what the tool
  does, where it comes from and, for winget, that terms are accepted for the user, which nobody
  could tell from its flags. The exact command still goes to the update log as it starts.
- **The update log ends a batch with "Finished updating."**, which the Help tab tells people to
  wait for; the words live once, in `help_content.UPDATES_FINISHED`. A Cancel that skips queued
  tools names them instead, and that batch never claims to have finished.
- **The window names each tool by its role first** (`Dep.label`, "Downloader (yt-dlp)") in the
  Settings rows, the red banner, the warning box, the confirmation before an update and the update
  log, and the Help tab uses the same role names. A missing YouTube helper is named after what
  would fill the gap, "deno" when the Install button fetches it, so the box that accepts license
  terms says whose they are.
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
- **Every color is spelled out in `theme.C` and nowhere else.** A test fails on a hex color, or
  on white or black, spelled out in any other module, and `theme.CONTRAST` lists the pairs that
  must stay readable, checked against WCAG ratios. Body text keeps 7:1: the Help tab is written
  for people who find computers hard, and a dark theme is only kind to them if its words are
  bright.
- **ttk runs on "clam", and `theme.apply()` runs before the first widget exists.** Windows'
  native "vista" theme ignores style colors. Classic Tk widgets get the palette from the option
  database by class (`*Text.background`), never a bare `*Background`, which ttk widgets also
  read as an option that then overrides their style's state maps.
- **The deck follows `phase` events from `jobs.py`, never the wording of status messages.** It
  animates only while something moves: the `after` loop stops once the reels are still and the
  tape has caught up, and `<Destroy>` cancels it. Clockwise means an increasing angle, because
  canvas y points down.
- **The tape never winds backwards during a job.** An MP4 download fetches the video and then
  the audio, and the second starts its progress again at zero; tape running back reads as
  starting over. Only a new job rewinds.

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

The unit tests need no network and no installed tools. The `Window` class builds the real window
withdrawn, so nothing appears. It skips only where there is no display to open one on (not
Windows, and no `DISPLAY`); anywhere else a Tk that fails to start is a failure, never a skip.
Likewise only `import tkinter` may turn into a skip: an `ImportError` from Tapewright's own
modules has to fail.

`.github/workflows/test.yml` runs everything on Windows and Ubuntu with Python 3.10 and 3.14, the
Ubuntu jobs under `xvfb-run` so the window tests run there too. A test that passes on 3.14 alone
has not shown it works on the oldest Python `pyproject.toml` accepts.

There is no automated end-to-end suite yet. To drive the real window against the real tools from
a script, replace the modal pieces (`app.ask_outdated`, `app.confirm` and `app.inform` are
attributes for exactly this reason) and set `TAPEWRIGHT_CONFIG_DIR` to a scratch folder so the
run never touches real settings. Test the pip update path inside a throwaway venv whose
`python.exe` runs the app, since the app installs into whichever Python runs it.

## Known gaps

- A standalone `yt-dlp.exe` is not used, by design. The app manages yt-dlp as a pip package
  in its own Python, so there is one update path, and it is tested.
- The FFmpeg and deno update buttons have been run by hand (on 2026-09-13: FFmpeg 8.1.1 to
  9.0.1 through winget, deno 2.8.3 to 2.9.6 through `deno upgrade`). Nothing exercises them
  automatically, since running one changes the machine. That winget run came before its
  commands gained `--source`, the accept flags and `--no-upgrade` in 0.1.1, and the new command
  lines haven't been run against the real winget since. Nor has the reinstall advice the update
  log gives when winget can't help (`winget uninstall --id`, then Install).
- On macOS and Linux, FFmpeg is only checked for presence, because a distribution's version
  lags upstream on purpose.
- Outside Windows, a Python that can't run Tapewright is explained only on stderr, so starting
  it from a desktop launcher on Linux or macOS still shows nothing.
- There is no GPU encoding and no drag-and-drop: Tk has none without the tkdnd extension.
- The Help tab's wording assumes Windows: File Explorer, the yellow folder on the taskbar,
  Windows+E. On macOS or Linux the steps are right but some of the names are not.
- There is one look, the dark VCR one; no light or system theme to switch to.
