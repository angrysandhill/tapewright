# AGENTS.md

Guidance for anyone, human or agent, changing this repository. `README.md` describes what
the app does; this file covers what must stay true while you change it.

## Shape

The half with no window is `jobs.py`, `deps.py`, `procs.py`, `versions.py`, `config.py`,
`help_content.py` and `launch.py`. None of them imports tkinter when it is imported (`launch.py`
tries to inside `problem()`, to say when it is missing, and `launch.main()` hands over to
`app.py`), so all of it runs headless. The window half is `app.py`, `setup_screen.py`,
`convert_tab.py`, `settings_tab.py`, `help_tab.py`, `deck.py`, `theme.py` and `widgets.py`.
`fetch.py` belongs to neither: it is a program of its own that the app runs as a child (see "The
fetcher is a child process" below). The tests drive the first half directly, test the pure
functions in `deck.py` and `theme.py` (the reel geometry, the contrast ratio, the cassette's
pixels) without a window, build the real window withdrawn in the `Window` class, and cover
`fetch.py` in `tests/test_fetch.py`.

`packaging/` is part of neither half, and only its icon ships. Its scripts run on the machine that
builds the Windows installer (see "The installer" below): `build.py` reads Tapewright's version
with `ast` rather than importing the package, and `make_icon.py` imports only `theme`, inside
`main()`. `tests/test_packaging.py` covers both without a network, Inno Setup or a runtime.

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
built. Where one comes from a tool's documentation or source instead, it says so. Tapewright's
own downloads are tested against fake downloads, and Cancel against a real child, a grandchild
of its own and a server on 127.0.0.1; the real GitHub path has been run by hand once (see Known
gaps).

### Child processes

- **Python children get `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`** (`procs.child_env`).
  On Windows' ANSI code page, yt-dlp *drops* the characters it can't encode from the paths
  it prints. There is no replacement character; the bytes are just missing. A title
  containing a full-width `｜` came back as a path that doesn't exist.
- **`PYTHONUNBUFFERED=1` too.** A Python child writing to a pipe block-buffers, and the
  progress then arrives all at once at the end, which looks like a hang.
- **The installer's own Python keeps its children to itself** (`procs.bundled`, `procs.child_env`).
  When `tapewright-runtime.txt` sits beside `sys.executable`, children lose `PYTHONHOME` and
  `PYTHONPATH` and get `PYTHONNOUSERSITE=1`, so a variable left by other software can't stop
  `python -m yt_dlp` starting, and a yt-dlp from another Python, or from the user's own
  site-packages, can't answer in place of the one the Settings tab installs. A Python someone
  installed themselves keeps its environment. With `PYTHONNOUSERSITE=1`, pip never falls back to the
  user's site-packages when it can't write to the Python's own (its `decide_user_install` returns
  before looking), so the installer's Python must sit in a folder the user can write to, or the setup
  screen's yt-dlp install fails (see "The installer is per user"). Read in pip 26.1.1's source, and
  seen by calling that function, installing nothing, on this PC's Python in Program Files. Tested
  only with the marker file made by hand (see Known gaps).
- **Arguments are lists and `shell=False`.** Any YouTube URL with `&list=` breaks under
  `cmd.exe`, and pasted text must never reach a shell.
- **Cancel kills the whole tree** (`taskkill /F /T` on Windows, `killpg` elsewhere). During
  a link-to-MP3 conversion the tree is python → ffmpeg + conhost. Terminating only the
  Popen leaves FFmpeg writing after the UI says "Cancelled". The child also gets its own
  process group, so nothing else will ever stop it: every exit path goes through
  `App.shutdown()`. `Cancelled` in `tests/test_fetch.py` fails unless the child's own sleeping
  child is gone after Cancel; it failed on Windows with `/T` dropped, and under WSL with the group
  kill replaced by a kill of the child alone.
- **Starting a process and cancelling share one lock** (`procs.Runner`). Without it, a
  cancel can land between "checked the flag" and "Popen returned", and the process starts
  with nothing left to kill it. This matters because local jobs run ffprobe and then up to
  two ffmpeg passes. `Runner.run` raises `Cancelled` only when the program exited non-zero: a
  killed process never exits 0, so a 0 means it finished before the kill landed, and calling that
  cancelled would report a finished install as one that never happened.

### Conversions

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
- **The deck follows `phase` events from `jobs.py`, never the wording of status messages.** It
  animates only while something moves: the `after` loop stops once the reels are still and the
  tape has caught up, and `<Destroy>` cancels it. Clockwise means an increasing angle, because
  canvas y points down.
- **The tape never winds backwards during a job.** An MP4 download fetches the video and then
  the audio, and the second starts its progress again at zero; tape running back reads as
  starting over. Only a new job rewinds.

### Finding, checking and updating tools

- **Installs are pinned to the version the check found.** pip gets `==version`
  (`deps.ytdlp_install_command`), so "Update" installs exactly what the check reported. Pinning
  is also the only way to go from a nightly back to stable, because a plain `--upgrade` never
  downgrades. The fetcher is pinned the same way (`deps.fetch_command`), with the bare number
  (`versions.short`), because a check accepts any answer that starts with a version while
  `fetch.py` refuses anything but a dotted number. Offline with nothing cached, there is no
  version to pin: pip installs the newest, and the fetcher is given `latest` and looks it up.
- **A check never raises, and "can't tell" is never "outdated."** A failed check becomes
  `UNKNOWN`. `versions.is_newer` returns False whenever either side doesn't parse, since a
  false "outdated" puts a hard warning in front of every conversion. The same goes for a
  JavaScript runtime whose version can't be read: it is `UNKNOWN`, not too old.
- **`versions.release_date` requires a year of 2000 or later.** Otherwise `2.9.6` is a
  valid date.
- **Tapewright's own copy of a tool is found first, then `PATH`, then the places installers use**
  (`deps.find_tool`). Its own copies in `%LOCALAPPDATA%\Tapewright\tools` come first because only
  the fetcher updates them: an older ffmpeg on `PATH` that shadowed one would still be reported
  out of date after every successful "Update", forever. That follows from the search order; it
  wasn't seen happen. The search goes past `PATH` because a winget upgrade of FFmpeg moves it to
  a new versioned folder and rewrites `PATH` in the registry, which an already-running app never
  sees. Jobs then receive the exact path the check found, through `--ffmpeg-location` and
  `--js-runtimes NAME:PATH`. yt-dlp splits that on the first colon, so a Windows drive letter is
  safe.
- **Tapewright's own copy that won't run gives way to one that does** (`deps.check_ffmpeg`,
  `deps.check_js`, `find_tool(name, own=False)`). Found first every time, an own FFmpeg or deno
  that names no version (blocked by a policy or an antivirus, or copied by hand without what it
  needs) would otherwise hide a working copy on `PATH` for good. Any other copy that answers with a
  version is used, and its detail starts by saying the own copy won't run. On Automatic, when no
  other deno answers, the YouTube helper takes a Node.js new enough for yt-dlp instead,
  `--no-js-runtimes` included, but never a too-old one, which would trade the button that fetches
  deno again for a warning nothing can fix. With no other copy the state is `UNKNOWN`, never out of
  date, and on Windows the Install button fetches the own copy again, since otherwise only deleting
  its folder by hand would fix it. `UNKNOWN` is not a problem state, so neither the conversion gate
  nor `deps.setup_needed` acts on it, but it is `deps.unusable`, so a setup screen that is showing
  offers that fetch. "Development build" is said only when a version string was read. Tested with
  the checks' programs mocked, not with a real copy that fails to start.
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
- **winget only upgrades a copy it already manages; it never installs anything new.** A missing
  FFmpeg or deno gets Tapewright's own copy (`deps.fetch_command`), and so does a copy from
  anywhere else that is out of date or has lost its ffprobe, as "Get Tapewright's own copy",
  which leaves that copy where it is. Not every Windows profile has winget (its documentation
  says it is registered only after a user's first sign-in), and winget accepts terms on the
  user's behalf. A winget FFmpeg with its ffprobe gone gets the fetcher too, since upgrading
  can't bring back a file missing from the version winget already has. Node.js is never
  installed or fetched: it is used only when someone already has it, and only a copy winget
  manages is offered an update. Outside Windows nothing is fetched.
- **winget runs with `--source winget --accept-source-agreements --accept-package-agreements`**
  beside `--disable-interactivity` (`deps._winget_command`). With interactivity off, winget
  can't ask a profile that never accepted its source's terms, and stops with 0x8A150046 instead
  (that code is from winget's `returnCodes.md`; the failure was not reproduced here). Exit codes
  are looked up after `& 0xFFFFFFFF`: Windows returns them unsigned, while winget's documentation
  lists each one both ways.
- **winget's "try again later" is said only for a tool that is out of date**
  (`deps.update_failure_text`). For a tool that is behind, 0x8A15002B means winget hasn't caught
  up yet: its package list often trails gyan.dev's release by days. A tool in any other state that
  gets that code (in practice a winget deno or Node.js too old for yt-dlp) is told updating can't
  fix it, and the update log names `winget uninstall --id <package>` as the way back, since nothing
  in the window can reinstall. The fetcher's own "Try again later" is a different case: a release
  not on GitHub yet arrives in time, whatever state the tool is in.
- **The JavaScript runtime minimums in `deps.JS_MINIMUM` mirror `MIN_SUPPORTED_VERSION` in
  yt-dlp's `utils/_jsruntime.py`** (as of 2026.08.19). Re-check them when yt-dlp's release
  notes mention runtimes.
- **The Tapewright release check is not a Dep, and never gates a conversion**
  (`deps.check_release`). "Update everything", the red banner, diagnostics and the conversion gate
  all walk `app.deps`, and an older Tapewright converts as well as a new one, so its answer lives in
  `App.release` and only the Settings tab shows it, in place of the "Update log" caption: a row of
  its own would push the end of the update log out of the default window. The check's worker asks GitHub
  only after posting the tools' answer, and posts the release separately
  (`App._release_finished`), so a slow or unreachable api.github.com never keeps `App.checking`
  set, which would hold back a conversion. GitHub is asked at most once a day, because its API
  allows 60 unauthenticated requests an hour per address (from GitHub's documentation) and the
  fetcher shares that allowance. A 404 means nothing is released yet and is remembered like an
  answer. Any other failure keeps the last answer as
  `{"version": <last known>, "checked": <now>, "failed": true}`, fresh for an hour rather than a
  day; a `checked` time in the future, from a clock put back, is never fresh. Checks can overlap,
  since each asks GitHub after its check is over, so answers land in either order, each worked out
  from a cache that may be stale. So `_release_finished` merges what its check learned and works
  the notice out again from the live cache, offline. It merges a failure only while the live entry
  is still the one its check read, since otherwise that failure's last answer, fresh for an hour,
  would hide a real one that landed meanwhile. Both orders were reproduced with real threads (an
  offline check's empty answer after a real one, a slow failure after a success), and the test
  posts answers in them. The page it opens is always `RELEASES_PAGE`, never a URL from the answer.

### Tapewright's own downloads

- **The fetcher is a child process, run by its absolute path, that never imports `tapewright`**
  (`deps.FETCH_SCRIPT`). `App.run_updates` starts it through `procs.Runner`, the way it starts
  pip, so Cancel kills it at any byte, its lines stream because of `PYTHONUNBUFFERED`, and the
  gates that stop a tool being replaced while a job uses it hold with no new code. Run as
  `-m tapewright.fetch` it would depend on the working folder, which `procs.Runner` never sets.
  It uses the standard library only; `tests/test_fetch.py` fails on any other import, and runs it
  by its path from an unrelated folder. `app.py` imports it only for `lock`, `recover` and
  `unlock` after a cancelled run, so importing it must stay free of side effects and those three
  keep their signatures.
- **A downloaded helper is verified before anything uses it** (`fetch.install`). The size and
  SHA-256 come from GitHub's API for that release's asset. Only when the API gives no digest
  (unreachable, rate-limited, or none listed) does the publisher's checksum stand in: gyan.dev's,
  only while gyan.dev's newest release is the version being installed, since that file always
  describes the newest, or deno's `.zip.sha256sum` asset. With neither it stops. The download
  address is built from the recipe, never taken from the API's answer. Size and hash are checked
  before the zip is opened; only the recipe's members are written, each by a fixed name, so no path
  inside the zip lands anywhere else; each program is test-run from the new folder, where its first
  line must name exactly the version expected ("9.0.10" starts with "9.0.1" too) and FFmpeg must
  list `--enable-gpl` and not `--enable-nonfree`; and after those test runs, which can take seconds
  while an antivirus scans, every unpacked file must still be there at the size written, or nothing
  is swapped. Read on 2026-09-14: the `digest` field's format from GitHub's API, and both checksum
  files, each matching GitHub's digest. gyan.dev's is bare hex; deno's is PowerShell `Get-FileHash`
  output (`Algorithm`, then `Hash` in upper case, then `Path`, 177 bytes with CRLF line endings),
  which is why `parse_digest` takes the first word of exactly 64 hex digits, and
  `tests/test_fetch.py` feeds it that exact layout.
- **A download can neither fill the disk nor hang, and nor can a lookup** (`fetch._download`,
  `fetch._read_bounded`, `fetch._check_space`). Before the first byte, free space must cover the
  zip, the recipe's `unpacked` estimate (200 MB for FFmpeg, 120 MB for deno) and the 64 MB
  `MARGIN`; at unpacking the members' exact size is checked again, and the not-enough-space
  sentence then counts the download too, since the failed run deletes it before anyone reads the
  figure. With no size from GitHub or the server, the recipe's `ceiling` (250 MB and 100 MB, about
  twice the zips GitHub lists now) stands in for it and is the most that is read; anything past it
  fails as damaged. Bodies are read with `read1`, so progress moves on a slow link, and fewer than
  64 KiB in 60 seconds, or no byte in `TIMEOUT` (30 s), ends the run as stalled. The lookups
  (GitHub's API, the newest-version file, the checksum files) go through the same reader, capped at
  `ANSWER_LIMIT` (1 MiB), so a trickle can't hang the run at "Looking up…"; one that stalls, runs
  past the cap or ends short of its `Content-Length` counts as no answer, exactly like a failed
  connection. The short answer needs its own check because `read1` just stops there where `read`
  raised `IncompleteRead` (seen with a real server on 127.0.0.1), and "v2.9.6" cut to "v2.9" is
  still a version. Measured: the unpacked estimates against a real download on 2026-09-15 (FFmpeg
  9.0.1's members 196 MB, deno 2.9.6's 93 MB); and with GitHub's API answer trickled a byte a
  second from a server on 127.0.0.1, a run whose lookups lacked the stall rule still sat at
  "Looking up…" after 80 seconds with no `ERROR`, while with it the API lookup gave up at 60
  seconds, gyan.dev's lookup after it, trickled the same way, at 120, and the run ended with
  github.com's `NETWORK_ERROR`. The other lookups' stalls, the cap and the short answer are tested
  only with fake responses and a fake clock.
- **Two runs for the same helper never overlap** (`fetch.lock`). Two windows can each start getting
  FFmpeg, and both would use `.ffmpeg.part` and `.ffmpeg-new`: a second run's `recover()` deleted
  the first run's files while it test-ran them, and the first then swapped a folder holding only
  `ffprobe.exe` into place and printed `DONE` (reproduced with a real running program and a second
  process). So each run locks byte 0 of `tools_dir/.<tool>.lock` (`msvcrt.locking` on Windows,
  `flock` elsewhere) once the folder exists and before `recover()`, and lets go only after the swap,
  or after a failed run has cleared its own files away. A run that finds the lock held stops with
  `ALREADY` (exit 5) before touching anything. The lock file is never deleted and `recover()` never
  touches it, because a run that had opened it just before a delete would lock a file no later run
  can find. The system lets go of the lock however a process ends: in `Cancelled`, the run straight
  after the kill goes ahead. Each tool has its own lock. Otherwise tested with two handles in one
  process, not with two windows.
- **The copy in use is replaced only after the new one passed every check** (`fetch.swap`,
  `fetch.recover`). The download is staged in `.<tool>.part` and the unpacked folder in
  `.<tool>-new`; the swap renames the old folder to `.<tool>-old` and the new one into place, and
  only then deletes the old one, moving it back if the new one can't take its place. A failure,
  Cancel or closed window before the swap leaves the working copy working, and on a failure the
  fetcher removes only what that run created. `recover()`, run at the start of every run and by the
  window after a Cancel, clears away `.part` and `-new` and puts back a `-old` that a kill between
  the renames left. Measured through `procs.Runner.cancel()`, with `taskkill` on Windows and
  `killpg` on Linux (`Cancelled` in `tests/test_fetch.py`): a kill mid-download keeps the copy in
  use and leaves a `.part` the next run removes.
- **Nothing kills the fetcher while it puts a helper in place** (`App.update_swapping`,
  `fetch.SWAP_PAUSE`). A kill between `swap()`'s two renames leaves the helper missing, so
  `fetch.py` prints `STEP Putting it in place…`, waits `SWAP_PAUSE` (1 s), and only then calls
  `swap()`. The words are spelled twice, as `fetch.SWAP_STEP` and `deps.SWAP_STEP`, because
  `fetch.py` imports nothing from Tapewright, and a test fails when they differ. The update's
  worker thread sets `update_swapping` as it reads that line, before posting it, and
  `_update_finished` clears it. While it is set, `App.cancel_update` returns False and skips
  nothing queued, the Settings tab's "Cancel update" and the setup screen's "Cancel" are disabled,
  closing the window asks no question, and `App.shutdown` waits up to `SWAP_WAIT_S` (10 s), then
  kills the fetcher anyway, since a closed window must leave nothing running. A Cancel accepted
  just before the worker read the line can't be taken back: its kill lands later, on another
  thread, and the pause is there so it lands while nothing has been moved. Measured:
  `procs.kill_tree` took 66-84 ms to land and each rename 0.8-1.9 ms; with no pause, of 259
  Cancels timed to land near the swap, 18 left FFmpeg missing and 115 put the new copy in place
  while the log said nothing had been replaced; with the pause, all 111 accepted Cancels, 99
  landing after the line was read, left the old copy in place. A busy computer can still start
  `taskkill` late, so what a kill mid-swap leaves is put right at once (next rule). The tests check
  the pause with a fake `sleep`, and the flag with a stand-in for `procs.Runner`.
- **A cancelled download is put right at once** (`app._recover_cancelled_fetch`). After a fetch run
  that ends in `Cancelled`, the worker takes the tool's lock (`fetch.lock`), runs `fetch.recover`
  and lets go in a `finally`, all before `_update_finished`, so the check after the batch finds the
  old copy back rather than the helper missing until its next download. What `recover` did goes to
  the update log. With no tools folder nothing is done and none is made; a lock another window holds
  is left alone, since that run's own `recover()` already ran; an `OSError` from `recover` becomes a
  log line and any other exception an internal error, and the batch still ends. The tool and folder
  are read from the command `deps.fetch_command` builds, and pip is never locked. A run that had
  printed the swap line ends with `deps.cancel_text(dep, swapped=True)`, "cancelled while it was
  being put in place. The check after it shows what is there now.", instead of the promise that
  nothing was replaced. Tested with a stand-in for `procs.Runner` on a real folder with a real lock,
  not with a real kill mid-swap.
- **`fetch.py`'s output is a protocol, read by `deps.parse_fetch_line`.** `STEP <sentence>`,
  `PROGRESS <done> <total>`, `DONE <version>` and `ERROR <sentence>`, one to a line; any other line
  is for the log alone and must never start with one of those words, which is why a test run's
  output is logged as `<exe> printed: ...`. The last line is `DONE` or `ERROR`, and the `ERROR`
  sentence, not the exit code, is what the update log and the setup screen show
  (`deps.update_failure_text`). Nothing branches on the exit code, and nothing should start to: it
  only sorts failures roughly, as `fetch.py`'s docstring lists. "Check that you're connected" is
  wrong advice for a failure that isn't the connection, so those failures have sentences of their
  own: `RATE_LIMITED` for a 429, or a 403 with `X-RateLimit-Remaining: 0`, when no checksum file
  can stand in for GitHub's digest (any other 403 still says github.com couldn't be reached);
  `CERTIFICATE` for an `ssl.SSLCertVerificationError`, bare or as a `URLError`'s reason, which
  points at the computer's clock; `STALLED`; and `ALREADY`. `NETWORK_ERROR` names the host that
  didn't answer: github.com, or www.gyan.dev or dl.deno.land when the newest version is looked up,
  including when that site answers with something that isn't a version. It is one template because
  each `ERROR` line must match exactly one sentence. `tests/test_fetch.py` checks that every way a
  run ends reads as what it is, and fails when an `ERROR` sentence is reached by no run;
  `tests/test_core.py` replays those runs through the real window and compares the whole update log.

### The window

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
- **A running Tapewright holds the named mutex `Tapewright.Running`** (`app._hold_app_mutex`), on
  Windows, from the moment its window has opened until the process ends. It is the installer's
  `AppMutex`, which Setup and Uninstall both look for when they start (from Inno Setup's help), so
  they wait for Tapewright to close rather than replace files it is using (the script's side, and why
  nothing closes Tapewright for them, is under "The installer"). The handle is never closed, since
  Windows releases the name when the process ends. No installer has been built yet, so nothing has
  read it (see Known gaps).
- **The window takes the shortcuts' taskbar ID before it exists** (`app.APP_USER_MODEL_ID`,
  `app._set_app_user_model_id`). The installer's shortcuts carry `AngrySandhill.Tapewright` as
  their AppUserModelID, and `main()` gives the process the same ID on Windows before `tk.Tk()`,
  since Microsoft's documentation asks for it before a program shows anything. That lets the taskbar
  group the window with those shortcuts, and with a pinned copy of one, rather than with
  `pythonw.exe`. A failure is ignored, since only the grouping is lost. `tests/test_packaging.py`
  fails when a shortcut carries another ID. The grouping is reasoned from that documentation; no
  real shortcut has run it.
- **Only the Tk main thread touches widgets.** Workers call `App.post()`. Every job event
  carries its `Runner`, and events from any runner other than the tab's current one are
  dropped, so a late event from a finished job can't repaint the next one.
- **The conversion gate runs in this order:** an update in progress refuses, a check in
  progress defers the start, a setup screen that took the tabs' place while the start waited
  declines it, and only then does the warning run. Test-and-set happens on the main thread. A
  click in the first second after launch must not slip past the warning, a tool must not be
  replaced while a job is using it, and a job started behind the setup screen would run where
  nobody can see it, with the screen's own install refused because of it. `App.run_updates`
  refuses while a job runs, for the same reason.
- **A check's answer that a newer check has overtaken goes nowhere** (`App._check_finished`,
  `App.recheck`). A check that read the tools before a batch replaced one would put a helper the
  batch just installed in front of the gate as missing, and the setup screen would call a good
  install a failure (reproduced). So when another check was queued while one ran, the versions the
  finished one looked up are still cached, but its tools reach nothing:
  not the tabs, the gate, the setup screen, the first-check rule or the callbacks waiting on it.
  The queued check does all of that. The check after a batch is asked for through `recheck` for
  the same reason, and a check asked for during a batch, such as for a changed setting, waits for
  the batch's own check, which goes online if any request wanted it to. Tested by driving
  `_check_finished` and `_next_update` directly.
- **The warning dialog's default button is never "Convert anyway".** Going ahead with a
  broken tool has to be a deliberate click.
- **The confirmation before an update is in plain words** (`deps.describe_update`): what the tool
  does, where it comes from, about how much a download is and, for winget, that terms are accepted
  for the user, which nobody could tell from its flags. The exact command still goes to the update
  log as it starts.
- **The update log ends a batch with "Finished updating."**, which the Help tab tells people to
  wait for; the words live once, in `help_content.UPDATES_FINISHED`. A Cancel that skips queued
  tools names them instead, and that batch never claims to have finished.
- **An unreadable settings file is renamed to `settings.json.bad`, not overwritten.** It may
  have been edited by hand.

### The setup screen

- **The setup screen never takes the tabs' place over a conversion.** `App.request_setup`, which
  both "Run setup again" buttons (Settings and Help) call, refuses and shows nothing while a job
  runs or a start waits on the gate (`App.converting`); the Help tab then says why on its
  message line. The Settings tab's button is disabled over exactly that and nothing else, so it
  works during an update: the screen follows a batch whoever started it, and that tab is where
  "Hide this page" lands. App redraws it when a start begins waiting on a check and again once
  that check has decided the start, or a declined start would leave it disabled.
  `_first_check_finished` doesn't show the screen then either. Only the show at startup skips the
  rule, since nothing can be converting before the window exists.
- **A setup screen someone asked for stays until they leave it** (`App._setup_requested`). Opened
  with "Run setup again" while the first check still runs, it would otherwise be closed by that
  check's rule for a screen that opened by itself, vanishing with no word of why (reproduced). The
  flag is set in `request_setup` just before `show_setup` and cleared in `hide_setup`; one flag is
  enough, since both buttons are on the tabs the screen replaces.
- **The setup screen asks no second question, so it runs only pip and the fetcher.**
  `deps.setup_keys` keeps only a helper that `deps.unusable` counts (a problem state, or there but
  won't run) and that pip or the fetcher fixes (`deps.setup_runs`, the one place that rule is
  spelled, which the screen's judging of an install follows too), and each of those rows says how
  big the download is and whether it comes from pypi.org or github.com. A winget upgrade accepts
  terms for the user and `deno upgrade` replaces a copy Tapewright never checks, so both run only
  from the Settings tab, which asks first; on the setup screen such a row says "Can be updated
  from the Settings tab.", and a row with no command says how to get that helper by hand.
- **The setup screen installs through `App.run_updates`, never around it**
  (`setup_screen.SetupScreen`). Cancel, the update log, the check after a batch and the refusal to
  update during a conversion are then the same as on the Settings tab. It passes `confirm=False`,
  since the screen was the question, and after an offline check it checks online first, so the rows
  install exactly the versions that check reports. It follows every batch, whoever started it,
  through `App.update_listeners` on the main thread: `("start", dep, index, total)`,
  `("line", dep, text)`, `("done", dep, ok, message)` and `("finished", None, cancelled)`. A
  listener that raises is logged and passed over, because the rest of the batch comes after that
  call, and a test fails when App sends a kind of event the screen doesn't handle.
- **No key on the setup screen stops a download by accident.** While a batch runs, "Hide this
  page" comes before "Cancel", takes the focus and answers Escape, and it puts nothing off. ttk
  buttons answer only Space, so the screen binds Enter and keypad Enter on the window, acting only
  while it shows (`SetupScreen._enter`): they press the focused button, "Show details" included,
  or else the first one shown, and a focused disabled button presses nothing (`invoke()` on a
  disabled ttk button does nothing, checked on a withdrawn window). "Cancel" calls
  `App.cancel_update()` and shows "Cancelling…" only when it returns True: a look of its own at
  `update_swapping` could be overtaken by the worker reading the swap line, and the page would say
  "Cancelling…", with Cancel disabled, through the whole of the next helper's install.
- **The setup screen judges only what it runs itself** (`deps.setup_runs`, `SetupScreen._settle`).
  It also hears a winget upgrade or `deno upgrade` the Settings tab started, whose outcome is that
  tab's log's to report; kept on the screen, its failure would offer a Try again with nothing to
  run. An install that exited 0 while the check after the batch still finds the helper
  `deps.unusable` failed (a new copy that won't run counts, one that runs but couldn't be checked
  for updates doesn't), with the check's detail, cut at `setup_screen.DETAIL_LIMIT` (200
  characters) on the row and whole in the update log. Either cancel sentence is a note, never a
  failure. `verdict()` counts only failures in `setup_keys`, so "Some helpers didn't install." and
  "Almost ready." always leave Try again something to run, and a row forgets its failure or note
  once that helper leaves `setup_keys` (`_forget_fixed`), so an old sentence can't come back if the
  helper goes missing again, and a red sentence nobody can retry never sits under "All set!".
- **The setup screen's words are built from the rows it lists** (`setup_screen.intro_text`). The
  number of helpers is said only when it matches the rows. Only a helper the screen fetches is said
  to come from github.com and be kept in Tapewright's own folder; one pip installs is said to be
  added to the Python that runs Tapewright, which other programs may share, so nothing says the rest
  of the computer is untouched. Each sentence follows where the helper is now
  (`SetupScreen._update_plan`), not what happened earlier in the session, since a Settings change or
  a manual install can change that while the screen is away: a fetch sentence stays only while the
  helper is Tapewright's own copy (`deps.is_own_copy`), a pip sentence stays because `check_ytdlp`
  only ever finds yt-dlp in that Python, and either is dropped once the helper is unusable in a way
  the screen doesn't install. "You don't need an administrator password" is said on Windows only,
  and rests on the fetcher writing under `%LOCALAPPDATA%` and on pip falling back to the user's
  site-packages for a Python in Program Files. That fallback was seen in pip's decision, never in a
  real install, and never applies to the installer's Python (see "The installer's own Python keeps
  its children to itself").
- **The setup screen replaces the notebook with `pack_forget`; it never covers it**, so no hidden
  tab or box can take the focus. On a first launch it shows at once, before the check comes back.
- **`setup_done` is saved in three ways only:** by "Start using Tapewright" after "All set!" or
  "Almost ready" (yt-dlp and FFmpeg in place, only the YouTube helper failed); by "Not now" once
  yt-dlp and FFmpeg are in place and a fetch of the YouTube helper has already failed this session,
  whichever tab started it; or silently when the first check gives `deps.setup_needed` no reason to
  keep the screen, which is how someone who already has every helper never sees it again, even with
  something out of date, which is the Settings tab's business. "In place" means not `deps.unusable`.
  A setup where yt-dlp or FFmpeg failed never saves it, and "Not now" otherwise holds only for the
  session. `setup_needed` counts a missing helper only when it is in `setup_keys`, so on a later
  launch the screen comes back by itself for a missing yt-dlp or FFmpeg it can install and, until
  `setup_done` is saved, for a missing YouTube helper it can fetch unless Settings has that helper
  Off. Outside Windows it installs only through pip, so there only a missing yt-dlp brings it back.

### Words, look and Help

- **The window names each tool by its role first** (`Dep.label`, "Downloader (yt-dlp)") in the
  Settings rows, the setup screen, the red banner, the warning box, the confirmation before an
  update and the update log, and the Help tab uses the same role names. A missing YouTube helper
  is named after what would fill the gap, "deno" when the Install button fetches it, so the
  confirmation names the program it downloads.
- **The Help tab only names things that are really on screen.** Its text marks each button,
  tab and box as `[Label]`, and `tests/test_core.py` fails when a label no longer appears in
  the source. Rename a button and its help in the same change, and keep quoted labels as whole
  string literals (`START_LABELS`, `COVER_LABELS`), not assembled with f-strings the test
  cannot see. A button under an answer can't be marked that way, since its words live only in
  `help_content.ACTION_LABELS`, the one file that test doesn't read; text that names one says
  "the button below that says ...", and a test fails unless that button is under that answer.
  Topic titles stay within 30 characters, because the question list cannot wrap. No test checks
  where the text says something is, so move its words when you move a widget. Words on someone
  else's screen, such as Windows' "Run anyway" in "Windows warned me about it", are never marked,
  because that test would look for them in Tapewright's source.
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

### The installer

`TapewrightSetup.exe` is built by `.github/workflows/release.yml` with Inno Setup, from
`packaging/tapewright.iss`, python.org's Python runtime and Tapewright's source. The steps that
build it, other than the unit tests and compiling, are subcommands of `packaging/build.py`, whose
docstring lists them in order, so each can be run locally. Compiling needs Inno Setup, and the top
of `tapewright.iss` gives its command line. The installer itself has never been built or run (see
Known gaps).

- **The runtime is python.org's own zip, unmodified, pinned in `packaging/runtime.json` and
  verified twice.** The pin names the version, the zip's address, its SHA-256 and the Tk version,
  and `build.load_pin` refuses any other shape. It is the zip python.org's install manager
  (pymanager) installs, listed in `index-windows.json`. `build.py runtime` hashes the download as it
  streams and keeps it only with the pinned SHA-256; then python.org's live index must still list
  that exact address with that hash, on any of its pages (`next` links are followed on python.org
  only), so a pin python.org has withdrawn or re-hashed stops the build instead of shipping.
  Unpacking refuses any member name that could land outside its folder (`build.safe_members`: a
  leading slash of either kind, any colon, which could name a drive or a stream, or a part other than
  `.` made only of dots and spaces). To move the pin, read the index and change the version, address
  and hash together. Three files hold them, and the tests fail until all three agree:
  `packaging/runtime.json`, `tapewright.iss` (`PyVersion`'s default) and `tests/test_packaging.py`
  (`URL`, `SHA` and the pin's `tk`).
- **`build.py check` stands between the zip and the installer.** It runs the runtime's own
  `python.exe` and fails unless it is the pinned Python, `tkinter.TkVersion` and Tcl's patch level
  are the pinned Tk, `-m pip --version` works, `pythonw.exe` (which the shortcuts start) and
  `LICENSE.txt` exist, and there is no `._pth` file, which would limit Python's search path to what
  it lists, so the yt-dlp pip installs could go unread. It runs that Python with `-I`, as the
  shortcuts do, so the pip it finds is the runtime's own: on this PC's Python in Program Files,
  plain `-m pip` found a newer pip in the user's site-packages, and `-I` found the Python's own.
- **The unit tests run on the runtime before it is marked, and write no bytecode into it.**
  `procs.bundled()` treats a Python with `tapewright-runtime.txt` beside it as the installer's own and
  changes its children's environment, so `release.yml` runs the suite first, as on a Python someone
  installed, and only then does `build.py mark` write the pinned version, which `RuntimeChanged`
  later reads; a test keeps `build.MARKER` and `procs.RUNTIME_MARKER` the same. The job sets
  `PYTHONDONTWRITEBYTECODE=1`, and `check` passes `-B` too, so no `__pycache__` folder lands in what
  the installer copies.
- **Tk stays 8.6, so the pin stays at 3.14.6, until the whole window has been run on a Tk 9
  build.** python.org's 3.14.7 moved to Tcl/Tk 9.0, while 3.14.6 carries 8.6.15 (read in CPython's
  `PCbuild/get_externals.bat`), and Tapewright's window, setup screen and Help tab have never run
  on Tk 9. `check` compares Tk with the pin's `tk`, so moving the Python alone fails the build;
  change `tk` only after running the window on that Python.
- **The installer is per user, never in Program Files** (`PrivilegesRequired=lowest`, and no
  `PrivilegesRequiredOverridesAllowed`, so `/ALLUSERS` can't move it). `{autopf}` is then the user's
  own `%LOCALAPPDATA%\Programs`, and the folder page is hidden. That also means no administrator
  prompt, but the reason it must stay this way is pip, which installs yt-dlp into the runtime and has
  no user folder to fall back on (see "The installer's own Python keeps its children to itself"). A
  test holds `PrivilegesRequired`, the missing override, `DefaultDirName` and `DisableDirPage`.
- **The runtime folder is replaced only when the pin changes** (`RuntimeChanged`, in the script's
  `[Code]`), because pip installed yt-dlp into it and an app update shouldn't lose that; after a pin
  change the setup screen offers yt-dlp again. `{app}\app`, by contrast, is replaced whole on every
  install, so a module a newer version dropped can't linger. `RuntimeChanged` is True when
  `{app}\runtime` has no `python.exe`, no `tapewright-runtime.txt`, or a marker naming another
  version than `PyVersion`. Setup may call it several times (on the Preparing page when Windows has
  renames pending, for the progress bar's size, for `[InstallDelete]`, then for each runtime file),
  the first always after `{app}` is known and before the folder changes (read in Inno Setup 6.7.1's
  source). It keeps that first answer, which describes the folder as it was, so every call in one
  Setup agrees whatever has been deleted or copied by then.
- **In `[Files]` the app comes first, then the runtime, then the runtime's marker on its own.**
  `SolidCompression=yes` packs everything as one stream, which Inno Setup's help says compresses
  many small files far better, but reaching a file means unpacking every file before it. With the
  runtime last, an update that skips it never unpacks it. The runtime's wildcard excludes
  `tapewright-runtime.txt`, and a last entry copies it, so a Setup stopped partway through the
  runtime (a crash, a forced restart) leaves no marker, and the next Setup replaces the runtime
  instead of keeping half of one. A test holds the order, the `Excludes` and the marker's entry.
- **`AppMutex` is `app.APP_MUTEX`, and `CloseApplications=no`.** Restart Manager would end Tapewright
  from outside, skipping `App.shutdown()`, the only thing that stops detached children. Setup's
  message asks for Tapewright to be closed and OK clicked, which is what Help's "Updating Tapewright"
  and the release notes tell people to do. Setup looks for the mutex only as it starts, and Tapewright
  can be opened while the wizard is up, so `PrepareToInstall`, in the script's `[Code]`, looks again
  after the last page and before anything is deleted, with the same message; Cancel, or a silent
  install run with `/SUPPRESSMSGBOXES`, stops Setup with nothing changed. A test holds both
  directives, and that `PrepareToInstall` looks for the same name.
- **The shortcuts and the Finish page start `{app}\runtime\pythonw.exe -I
  "{app}\app\Tapewright.pyw"`, in `{app}\app`.** With `-I`, no `PYTHON*` variable another program
  left behind and no user site-packages can break the start, while the runtime's own site-packages,
  where yt-dlp is, still loads. It also leaves the script's folder off `sys.path`, which
  `Tapewright.pyw` puts back itself. A test checks the three entries start Tapewright alike, and that
  each file they name in the app folder is one `stage` copies.
- **The AppId never changes.** Windows knows the installed app by
  `{C7E317BA-41A6-4ADE-ABE5-173A5DAC0058}` (written with a doubled `{{` in the script). A later
  Setup with the same ID finds the same folder and replaces the same Installed apps entry; a new ID
  would install a second Tapewright beside the first. `OutputBaseFilename=TapewrightSetup` carries
  no version, so `/releases/latest/download/TapewrightSetup.exe` always finds the newest. A test
  fixes both.
- **yt-dlp, FFmpeg and deno never ship in the installer.** `build.py stage` copies only
  `Tapewright.pyw`, `tapewright/*.py` (no `__pycache__`, no tests), `LICENSE`, `README.md` and the
  icon, and refuses a module in a folder below `tapewright/`, which it would otherwise leave behind
  with no word. `runtime` and `stage` refuse a folder that already holds something rather than
  delete it, so nothing left over from before ships. The helpers come from the first launch's setup
  screen; only the pip inside python.org's zip ships with the runtime.
- **Uninstall removes the helpers and keeps the settings.** Beyond what Setup copied,
  `[UninstallDelete]` removes `{app}\runtime` (the packages pip added), `{app}\app` (the bytecode
  Python writes beside the installed modules when Tapewright runs),
  `%LOCALAPPDATA%\Tapewright\tools`, and then `%LOCALAPPDATA%\Tapewright` if that leaves it empty;
  `%APPDATA%\Tapewright` is kept. There is no entry for `{app}` itself: once its own files are
  gone, Uninstall tries again the folders it couldn't remove at first (read in Inno Setup's
  `Setup.UninstallLog.pas`, not seen). A test fails when an entry reaches into `%APPDATA%`.
- **`packaging/tapewright.ico` is the cassette `theme.cassette_pixels` draws, and the committed file
  must match a fresh drawing.** `cassette_pixels(size)` is pure, and `theme.cassette_icon` fills the
  title bar's 48-pixel image from it, so the title bar, the shortcuts, Setup and Installed apps show
  one cassette. `make_icon.py` writes 16, 24, 32, 48 and 64 pixels as bitmaps and 256 as a PNG
  (Inno Setup's help recommends at least 16, 32, 48, 64 and 256; 24 is the small icon at 150%).
  `tests/test_packaging.py` draws it again and compares bytes, so changing the cassette or a color
  it uses fails until `python packaging/make_icon.py` has been run and the .ico committed. The PNG's
  deflate stream is written by hand rather than with `zlib.compress`: measured, the same data at the
  same level compressed to different bytes on Windows' Python 3.14.5 (zlib-ng) and WSL's 3.14.4
  (zlib), and the test runs on both. `.gitattributes` marks `*.ico` binary.
- **Only a pushed tag releases, and only as a draft.** `release.yml` runs on a pushed `v*` tag and
  on Run workflow. The tag check (`build.py check-tag`: `v` and `tapewright.__version__`, exactly),
  the attestation (`actions/attest`) and `gh release create --draft` run only for a push, so a
  manual run, even from a tag, leaves a workflow artifact and releases nothing. `build.py versions`
  prints `app=` and `python=` lines for ISCC's `/DAppVersion` and `/DPyVersion`. Inno Setup is the
  runner image's own, refused below 6.6, where `WizardStyle=modern dark` arrived, and never
  downloaded. A test fails when the workflow calls a `build.py` step that doesn't exist or leaves one
  out, runs the steps out of order, or lets a releasing step run without a push. The draft is
  published by hand, after the checklist under Testing.

## Licensing

Tapewright is `GPL-3.0-or-later`, and the text is in `LICENSE`. Every source file starts with
the same two lines, and `tests/test_core.py` fails when a file is missing them:

    # SPDX-FileCopyrightText: 2026 AngrySandhill
    # SPDX-License-Identifier: GPL-3.0-or-later

That covers `tapewright/`, `tests/`, `Tapewright.pyw`, `packaging/*.py`, the workflows and
`tapewright.iss`, whose two lines start with `; ` instead. `packaging/before-install.txt` and
`packaging/release-notes.md` have none, since people read them on Setup's Information page and on
the release page. `packaging/runtime.json` has none either, since JSON has no comments.

Running yt-dlp, FFmpeg and deno as separate programs, and never shipping them, is also why
their licenses don't reach this code. Tapewright's own FFmpeg and deno don't change that: each
copy of the app fetches them from upstream into its user's own folder, and they never go into a
release. A release that bundles one of them, such as a one-click `.exe` with `ffmpeg.exe` inside,
has to include that tool's license and meet its terms. For FFmpeg those depend on the build:
`ffmpeg -version` lists `--enable-gpl` for a GPL build, and a build with `--enable-nonfree` can't
be redistributed at all. The fetcher refuses an FFmpeg whose test run shows `--enable-nonfree` or
lacks `--enable-gpl`, and unpacks the build's `LICENSE` beside it when the zip has one.

The Windows installer does carry other people's software: python.org's Python runtime, unmodified,
with the Tcl/Tk and pip that come in its zip. Each keeps its own license, and the license files stay
where python.org put them in the runtime folder. `build.py check` fails without Python's
`LICENSE.txt`. In python.org's 3.14.5 install on the PC this was built on, Tk's terms are in
`tcl\tk8.6\license.terms` and pip's in its `dist-info` folder; the zip's layout hasn't been looked
at. yt-dlp, FFmpeg and deno are still never in it.

## Testing

```
python -m unittest discover -s tests -v
```

The unit tests need no network and no installed tools. One test, `Cancelled` in
`tests/test_fetch.py`, runs `fetch.py` as a real child against a server on 127.0.0.1 and kills its
whole tree partway through a download; nothing leaves the computer. Its second run, which
finishes, keeps the real `time.sleep` and so really waits out `SWAP_PAUSE`, a second; every other
run of `fetch.install` is given a `sleep` that returns at once. The `Window` class builds the real
window withdrawn, so nothing appears, and `Window.new_app` points `TAPEWRIGHT_TOOLS_DIR` at a
temporary folder of its own, since the recovery after a replayed Cancel would otherwise tidy
Tapewright's real tools folder. It skips only where there is no display to open one on (not
Windows, and no `DISPLAY`); anywhere else a Tk that fails to start is a failure, never a skip.
Likewise only `import tkinter` may turn into a skip: an `ImportError` from Tapewright's own
modules has to fail.

The `Install` tests in `tests/test_fetch.py` check free space with the real `shutil.disk_usage`
unless a test replaces it, so the drive holding the temp folder needs about 265 MB free
(FFmpeg's 200 MB estimate and the 64 MB margin). On a fuller disk they fail with the
not-enough-space sentence, which says nothing about the code.

`fetch.py` has no setting that points it at another server, and should get none: an address that
could be changed from outside is one more thing to trust. Its tests swap `fetch.install`'s
`urlopen`, `run`, `now` and `sleep` instead, as arguments in-process, or `urlopen` and `run`
through `install.__kwdefaults__` in the child.

A withdrawn window is never laid out, which shapes the two tests that measure room.
`test_the_settings_tab_fits_the_default_window_with_a_release_and_every_button` adds up what the
Settings tab's widgets ask for, with their words wrapped by hand to the default window's width.
`test_the_setup_buttons_are_never_what_a_short_window_cuts_off` instead places the setup screen
at a fixed size, which Tk does lay out, children included, and simulates 125% scaling through
`Window.new_app(scale=...)`, which sets `tk scaling` before the App is built. Both have run only
on Windows with its fonts; the Ubuntu jobs use others.

`.github/workflows/test.yml` runs everything on Windows and Ubuntu with Python 3.10 and 3.14, the
Ubuntu jobs under `xvfb-run` so the window tests run there too. A test that passes on 3.14 alone
has not shown it works on the oldest Python `pyproject.toml` accepts.

`tests/test_packaging.py` needs no network, no Inno Setup and no runtime. It reads `tapewright.iss`
and `release.yml` as text and checks them against the code, tests `build.py`'s helpers with fake
downloads and zips, and compares the committed icon with a fresh drawing, the one test there that
skips on a Python without Tk. Whether Inno Setup compiles the script, and whether the installer
behaves, only `release.yml` and the checklist below can show. `release.yml` also runs the whole
suite on the runtime before marking it, with `TAPEWRIGHT_CONFIG_DIR` and `TAPEWRIGHT_TOOLS_DIR` in
the runner's temp folder.

There is no automated end-to-end suite yet. To drive the real window against the real tools from
a script, replace the modal pieces (`app.ask_outdated`, `app.confirm` and `app.inform` are
attributes for exactly this reason) and set `TAPEWRIGHT_CONFIG_DIR` and `TAPEWRIGHT_TOOLS_DIR` to
scratch folders, so the run never touches real settings or Tapewright's real copies of FFmpeg and
deno. Test the pip update path inside a throwaway venv whose `python.exe` runs the app, since the
app installs into whichever Python runs it.

### Before publishing a release

A pushed tag leaves a draft release. The maintainer publishes it only after these steps, on a spare
standard (not administrator) Windows account:

1. Download the draft's `TapewrightSetup.exe` with a browser, and screenshot any warning it shows.
   Compare `Get-FileHash` with `SHA256SUMS.txt`, run
   `gh attestation verify TapewrightSetup.exe --repo angrysandhill/tapewright`, and scan it.
2. Open it. No administrator (UAC) prompt may appear. Screenshot the SmartScreen box, before and
   after "More info", for the release notes. Check what the browser's warning and this box say
   against the README, the release notes, and Help's "Updating Tapewright" and "Windows warned me
   about it", and look at each page of the dark wizard.
3. On the setup page, click "Install them now", then "Cancel" during the FFmpeg download. Then
   install everything, up to "All set!".
4. Convert a link to MP3, a link to MP4 and a local file.
5. "Copy details for my helper" shows a Python under `%LOCALAPPDATA%\Programs\Tapewright\runtime`,
   "the installer's own Python: yes" and Tk 8.6.15. Close Tapewright, pin its Start menu shortcut to
   the taskbar and open it from there: its window must share the pinned button rather than get one
   of its own.
6. With Tapewright open, run the same installer again. Setup must show its AppMutex message, in the
   words Help's "Updating Tapewright" and the release notes repeat, and close nothing. Close
   Tapewright and click OK; once Setup has finished, yt-dlp is still installed and the settings are
   unchanged.
7. Run the installer again with Tapewright closed, and open Tapewright while Setup's first page is
   showing. Click through: the same message must appear before anything is installed, and Cancel
   must leave the installed Tapewright as it was.
8. When there is an earlier release, install it first and this one over it. If the pin changed, the
   setup page offers yt-dlp again.
9. Uninstall from Installed apps. `%LOCALAPPDATA%\Programs\Tapewright` and
   `%LOCALAPPDATA%\Tapewright` are gone, and `%APPDATA%\Tapewright\settings.json` is still there.
10. Add the screenshots to the release notes, and publish.

## Known gaps

- A standalone `yt-dlp.exe` is not used, by design. The app manages yt-dlp as a pip package
  in its own Python, so there is one update path, and it is tested.
- The real GitHub download path has been run end to end once, not in any automated test: on
  2026-09-15, on the Windows 10 PC this was built on, the setup screen fetched FFmpeg 9.0.1 and deno
  2.9.6 from GitHub into a scratch tools folder over a fast connection (18 s for both). Both
  matched GitHub's SHA-256 digests, unpacked from the layouts the recipes expect
  (`ffmpeg-9.0.1-essentials_build/bin/`, `deno.exe` at the zip's root), passed their test runs,
  were swapped in, and the screen reached "All set!". The downloaded FFmpeg then converted a
  generated tone to MP3 and a VP9/Opus clip to MP4 through `jobs.run_job`. The other copies on
  that PC were hidden from the checks and yt-dlp's check was stubbed, so pip, a slow or metered
  link, a proxy, the checksum fallbacks and a real Cancel mid-download were not part of that run;
  every automated test still serves a zip it built itself.
- The winget-free first run hasn't been tried on a fresh Windows account. The setup screen has
  been looked at on screen only at 96 DPI, on the PC this was built on (screenshots of every
  state, and of the real download above). Its layout was measured on withdrawn windows, with the screen
  placed at a fixed size so that Tk lays it out, after a batch that left three long failures: in
  the default window at 96 DPI; in the smallest window on a 1366×768 laptop at 125%, which is
  900×656; and in the smallest window on a 1080-pixel-tall screen at 200%, which is 1440×900. In
  each, with the details shown or not, the buttons stayed inside and the helper rows got less
  height than they asked for, which is when they scroll. The scrolling itself hasn't been seen:
  the canvas and its scrollbar sit deeper than that layout reaches.
- The Settings tab was measured the same way as its test measures it, from requested sizes at
  96 DPI on Windows, with every row showing its longest button and a release showing. It needs
  678 of the 700 px the default window gives it, and 714 of the 620 px in the smallest window, so
  there the update log is 94 px short of its floor. Only the default window was made to fit.
- The winget and `deno upgrade` update buttons have been run by hand (on 2026-09-13: FFmpeg 8.1.1
  to 9.0.1 through winget, deno 2.8.3 to 2.9.6 through `deno upgrade`). Nothing exercises them
  automatically, since running one changes the machine. That winget run came before its
  upgrade command gained `--source` and the accept flags in 0.1.1, and the new command line hasn't
  been run against the real winget since. Nor has the reinstall advice the update log gives when
  winget can't help (`winget uninstall --id`).
- The Windows installer has never been built or run. Inno Setup isn't installed on the PC this was
  built on, so `tapewright.iss` has never been compiled and `release.yml` has never run. The
  script's directives were checked against Inno Setup 6.7.1's help and source, and
  `tests/test_packaging.py` checks only its text. Unproven until the first build and the checklist
  under Testing: the preprocessor's line spanning, `RuntimeChanged`'s cached answer across
  `[InstallDelete]` and `[Files]`, the marker's `Excludes` pattern, `PrepareToInstall`'s second look
  for the mutex, `WizardStyle=modern dark`, the taskbar grouping by AppUserModelID, whether the
  runner's Inno Setup is where the workflow looks, whether python.org's live index lists the pin the
  way `build.index_has` reads it, whether the 3.14.6 zip passes `check` and the tests there, and the
  wording of Setup's AppMutex message, which Help and the release notes repeat. Both ends in this
  repository, `tapewright-runtime.txt` and `Tapewright.Running`, are tested only against fakes.
  After the first CI build this becomes "built but not yet run", until the checklist passes.
- The installer isn't signed. Until it is, SmartScreen warns about each new version (an unsigned
  file's reputation starts again with every one, from Microsoft's SmartScreen documentation), and a
  Windows 11 PC with Smart App Control on blocks it outright. Help's "Windows warned me about it",
  the README and the release notes describe both boxes, and Help's "Updating Tapewright", the
  README and the release notes the browser's "Keep", all from documentation; none of them has been
  seen on this PC.
- The installer accepts Arm64 Windows 11, which runs x64 programs (`x64compatible`), but it hasn't
  been tried there.
- The fetch buttons don't check for 64-bit Windows. On 32-bit Windows the button appears and
  `fetch.py` refuses with its "only on 64-bit Windows" sentence. It accepts ARM64, where
  Windows 11 runs x64 programs; Windows 10 on ARM can't, and gets the "Windows stopped FFmpeg from
  running" sentence instead, which is misleading there.
- Nothing retries the swap. If an antivirus is still scanning the programs just test-run, renaming
  the folder can fail and the update log says the helper is in use; trying again should work. A
  close that waits past `SWAP_WAIT_S` kills the fetcher anyway, and the recovery after a Cancel runs
  on a worker thread a closing window doesn't wait for, so that kill can leave the helper missing
  until its next download. Neither has been measured, only a normal swap's timing.
- Nothing stops two Tapewright windows running at once. Only fetches of the same helper are kept
  apart (`fetch.lock`); nothing else is shared between windows: each has its own gates, so one
  window can replace FFmpeg while the other converts with it, and both can run pip at the same time.
  That is reasoned from the code, not tried.
- An update started from the Settings tab shows no download progress: the update log leaves out
  `PROGRESS` lines, and only the setup screen has a bar.
- Until setup has finished once, on Windows the setup screen comes back at every launch while deno
  is missing and the YouTube helper setting is Automatic or deno, and the page offers no way to say
  that deno isn't wanted. That includes anyone upgrading from an earlier version without deno,
  whose settings file has no `setup_done`. It stops once deno installs, Node.js is found, the
  setting becomes Node.js or Off, or setup finishes. Someone upgrading from an earlier version
  with nothing missing sees only the "Looking…" page, once, during the first check.
- The Settings tab's "Run setup again" is redrawn only when something it watches changes. A start
  cancelled from its own tab while it waits for a check changes none of that, so the button stays
  disabled until that check ends, although `request_setup` would already allow it. That is
  reasoned from the code, not tried.
- On macOS and Linux, FFmpeg is only checked for presence, because a distribution's version
  lags upstream on purpose.
- Outside Windows, a Python that can't run Tapewright is explained only on stderr, so starting
  it from a desktop launcher on Linux or macOS still shows nothing.
- There is no GPU encoding and no drag-and-drop: Tk has none without the tkdnd extension.
- The Help tab's wording assumes Windows: File Explorer, the yellow folder on the taskbar,
  Windows+E. On macOS or Linux most steps are right but some of the names are not, and "Updating
  Tapewright" describes the Windows installer, with one paragraph for copies run from source.
- There is one look, the dark VCR one; no light or system theme to switch to.
