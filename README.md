# Tapewright

Turn a link or a local file into an MP3 or an MP4.

Tapewright is a small desktop app on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp) and
[FFmpeg](https://ffmpeg.org). It is plain Python and Tkinter with no other dependencies. The
first time it opens it offers to install those tools for you (FFmpeg on Windows only), and its
Settings tab keeps them up to date. When one falls behind, you get a hard warning before any
conversion that uses it.

Only download what you have the right to. Many sites' terms forbid downloading.

## Install on Windows

For Windows 10 or 11, 64-bit:

1. Open the [latest release](https://github.com/angrysandhill/tapewright/releases/latest) and,
   under **Assets**, click `TapewrightSetup.exe`. The files called *Source code* aren't needed.
2. Open the file you downloaded. Tapewright is installed just for you, so no administrator
   password is needed, and nothing is downloaded while it installs.
3. The first time Tapewright opens, it offers to download the helpers it uses (see
   [The first launch](#the-first-launch)).

The installer isn't signed yet. If your browser says the file isn't commonly downloaded, choose
*Keep*. If Windows says *Windows protected your PC*, click *More info*, check that the name beside
*App* is `TapewrightSetup.exe`, and click *Run anyway*. If that box only offers *OK*, the computer
blocks programs that aren't signed, for example with Smart App Control: wait for a signed release,
or [run Tapewright from source](#running-from-source).

What goes where:

- **Tapewright and its own private Python**, which yt-dlp is installed into, go in
  `%LOCALAPPDATA%\Programs\Tapewright`. That Python is only for Tapewright: it isn't put on `PATH`
  and doesn't change any Python you already have.
- **The helpers Tapewright downloads**, FFmpeg and deno, go in `%LOCALAPPDATA%\Tapewright\tools`.
- **Settings** go in `%APPDATA%\Tapewright`.

**To update**, download the new `TapewrightSetup.exe` the same way and open it. The Settings tab
says when a new version is out, and its button opens the release page. Settings and helpers are
kept, yt-dlp included unless the new version brings a different Python, in which case the setup
page offers yt-dlp again. If Tapewright is open, Setup asks you to close it first, then click
*OK*; it never closes Tapewright itself.

**To uninstall**, open Windows' Settings, then *Apps* (*Installed apps* on Windows 11, *Apps &
features* on Windows 10), and uninstall Tapewright. That removes Tapewright, its Python with
yt-dlp, and the FFmpeg and deno it downloaded. Your settings are kept; delete
`%APPDATA%\Tapewright` to remove them too. A copy of FFmpeg, deno or Node.js that Tapewright didn't
download, such as one winget installed, is left alone.

Each release lists the installer's SHA-256 in `SHA256SUMS.txt`, and GitHub keeps a record of the
workflow that built it, which this command checks:
`gh attestation verify TapewrightSetup.exe --repo angrysandhill/tapewright`.

## Running from source

You need Python 3.10 or newer with Tkinter. The Python from python.org includes Tkinter on
Windows and macOS; on Linux it is often a separate package, such as `python3-tk` on Debian and
Ubuntu. Get the source with *Code* and then *Download ZIP* on this project's GitHub page, or with
`git clone https://github.com/angrysandhill/tapewright`, and in its folder run:

```
python -m tapewright
```

On Windows you can also double-click `Tapewright.pyw`, which starts it without a console window.
If that Python is too old or has no Tkinter, a message box says so and what to install, rather
than nothing happening at all.

## The first launch

The first time Tapewright opens, a page called *Let's get Tapewright ready* takes the place of
the tabs. It lists the helper programs by what they do: the Downloader (yt-dlp), the Converter
(FFmpeg) and the YouTube helper (deno, or Node.js if you already have it). It says which of them
this computer already has, and *Install them now* installs the rest:

- **yt-dlp** installs through pip, into the same Python that runs Tapewright.
- **FFmpeg and deno**, on Windows, are downloaded by Tapewright itself from their official GitHub
  releases: the essentials build from [GyanD/codexffmpeg](https://github.com/GyanD/codexffmpeg),
  and deno's Windows zip from [denoland/deno](https://github.com/denoland/deno). Each download is
  checked and test-run before it is used (see below) and kept in
  `%LOCALAPPDATA%\Tapewright\tools`, so nothing else on the computer changes. Neither winget nor
  an administrator password is needed.
- **A copy that winget or deno's own updater looks after** is left alone when it is out of date:
  its row says it can be updated from the Settings tab, which asks before it runs either of them.
- **On macOS and Linux** the page can only install yt-dlp. Install FFmpeg, and deno or Node.js
  22+, yourself; while the page is open, each of their rows says where to get it.

The helpers install one at a time, and a download's bar counts it out in megabytes. *Show
details* shows every step as it happens, *Hide this page* brings back the tabs while the helpers
go on installing, and *Cancel* stops the batch. A stopped download is never used, but yt-dlp
stopped partway through installing may be half-installed, so install it again before converting.
When yt-dlp and FFmpeg run and nothing is left for the page to install, it says *All set!*, and
when only the YouTube helper failed it says *Almost ready*; either way, *Start using Tapewright*
finishes setup. Otherwise a helper the page installed that failed, or that is there but won't run,
turns red with the reason, beside *Try again* and *Help with this*. An update started from the
Settings tab shows on the page too, but how it went is reported in that tab's update log. The page
itself is the question, so no second box asks you to confirm. *Enter* presses the highlighted
button, which while the helpers install is *Hide this page*, not *Cancel*.

*Not now* skips the page for the rest of the session. *Run setup again*, on the Settings tab or
under the Help tab's answer about setting up, brings it back, even while helpers are installing,
unless a conversion is running or about to start. If the first check finds nothing missing that
the page could install, a page that opened by itself closes and doesn't open again, while one
you opened with *Run setup again* stays open; a helper that is only out of date is left to the
Settings tab. From then on it only comes back by itself when Tapewright starts and finds yt-dlp or
FFmpeg missing, and the page can install it. Until setup has finished once, a missing YouTube
helper the page can download brings it back too, unless the YouTube helper is turned off in
Settings.

## The tabs

**To MP3** takes a link or a file. Links go through yt-dlp, which converts the best audio
stream. Local files go straight to FFmpeg, so a video on your PC becomes an MP3 with no
network involved. Quality is VBR V0 (the default), V2, or 320, 192 or 128 kbps. For links,
the thumbnail can be embedded as cover art, and a playlist link can download every item.

**To MP4** works the same way, with two choices:

- **Compatible** gives H.264 video with AAC audio, which plays on almost anything. From
  YouTube that usually tops out at 1080p.
- **Best quality** keeps whatever the source uses: VP9, AV1 or Opus in an MP4 container. Some
  players, including older Windows ones, can't play those.

For links you can cap the resolution. A local file is copied into the MP4 without
re-encoding when its codecs allow it. Otherwise it is re-encoded (x264 CRF 20, AAC 192 kbps).
Only the first video and audio tracks are kept, and the log says what was left out.

While a conversion runs, a cassette on the tab plays it out. Both reels turn and the tape winds
from the left reel onto the right one as the job goes, never back, so it can already be full
while a video's sound or a playlist's later videos are still coming. The display beside it says
LOADING, PLAY or REC and counts the time, and the label shows the title of the video being
downloaded, or the file's name for a file on this PC. The rest of the window has the same dark
VCR look.

Both tabs follow the same rules:

- **Nothing is overwritten.** A local conversion whose name is taken becomes `name (1).mp3`.
  A link whose file already exists is skipped, and the log says so.
- **Cancel stops everything.** That includes the FFmpeg and deno processes yt-dlp started,
  and any half-written files are deleted. A truncated MP3 looks exactly like a finished one,
  which is why none are left behind.
- **The log shows every command exactly as it ran**, so a failure can be reproduced in a
  terminal. *Copy log* puts all of it on the clipboard.

**Help** answers everyday questions in plain language, for someone who rarely uses a
computer: how to copy a link, where the saved file went, what the red warning means and what
to try when something fails, including how to put FFmpeg or deno in place by hand when a
download won't install. Buttons under each answer open the tab, folder or page it mentions,
and **Bigger** and **Smaller** change the text size. The *Link or file* and *Save to* boxes
have a right-click Cut, Copy and Paste menu, since that is how many people paste.

## Settings: keeping the tools current

| Tool | Checked against | Updated by |
|---|---|---|
| yt-dlp | PyPI, on the stable or nightly channel | pip, into the Python running Tapewright, pinned to the exact version the check found |
| FFmpeg | the latest release number from gyan.dev (Windows) | Tapewright's own copy: downloaded again from GitHub, pinned to that release. A copy winget installed: `winget upgrade`. Any other copy: the way you installed it, or *Get Tapewright's own copy* |
| deno | dl.deno.land | Tapewright's own copy: downloaded again from GitHub, pinned to that release. A copy winget installed: `winget upgrade`; one in `~/.deno`: `deno upgrade`. Any other copy: the way you installed it, or *Get Tapewright's own copy* |
| Node.js 22+ | yt-dlp's minimum version only | A copy winget installed, once it is too old: `winget upgrade`. Any other copy: the way you installed it. Tapewright never installs Node.js |

winget only ever updates a copy it installed itself, only from this tab, and Tapewright never uses
it to install anything new. *Get Tapewright's own copy* leaves the other copy where it is, and
Tapewright looks for its own copy first from then on. If Tapewright's own copy stops running, any
other copy that works is used instead (for the YouTube helper on Automatic, that includes Node.js 22
or newer), and when there is none the row offers to download its own copy again, as the setup page
does when you open it. Before you install or update anything from this tab, a box says in plain words what
will happen: where the tool comes from, about how big a download is and, for winget, that it
accepts terms for you. Automatic yt-dlp updates, if you turn them on, don't ask.

When a tool is out of date, a red banner appears on both conversion tabs. Starting a
conversion that uses that tool opens a warning, and its default button is *Update now*. A
local file conversion only warns about FFmpeg, since that is all it uses. Every choice is on
the Settings tab:

- **When a tool is out of date:** *warn before every conversion* (the default) or *block
  conversions until it is updated*. A missing yt-dlp or FFmpeg always blocks, because the job
  can't run without it.
- **yt-dlp channel:** stable, or nightly, where fixes for site changes arrive days sooner.
- **Update yt-dlp automatically** when a new version is out, or install it when it is missing,
  without asking first. Off by default.
- **Check for updates when Tapewright starts.** On by default.
- **Offline:** if the online check fails, the last known answer is used. With no known
  answer, yt-dlp still counts as out of date once its version (a release date) passes the
  age you set.
- **YouTube helper:** yt-dlp needs a JavaScript runtime to solve YouTube's challenges. The
  choices are automatic, deno, Node.js, or off.

When a newer Tapewright is out, the Settings tab says so just above its update log, with a button
that opens its download page. Tapewright asks GitHub at most once a day, when it checks for
updates, or an hour after an attempt that failed. A new version never warns about or blocks a
conversion: an older Tapewright converts as well as a newer one.

Settings are saved in `%APPDATA%\Tapewright\settings.json` (Windows),
`~/Library/Application Support/Tapewright` (macOS) or `~/.config/tapewright` (Linux). Set
`TAPEWRIGHT_CONFIG_DIR` to put them somewhere else. Tapewright's own FFmpeg and deno are in
`%LOCALAPPDATA%\Tapewright\tools`, in a folder each; deleting one removes that copy. Set
`TAPEWRIGHT_TOOLS_DIR` to use another folder.

## How Tapewright checks its own downloads

FFmpeg and deno are fetched by `tapewright/fetch.py`, which the window runs as a separate program.
For each one it:

1. Reads the file's size and SHA-256 from GitHub's listing for the release. When GitHub can't
   answer, for example after 60 requests in an hour from one address, it uses the checksum the
   publisher puts beside the download instead, and stops when there is none.
2. Checks there is room, then downloads from an address it builds itself,
   `https://github.com/<project>/releases/download/<version>/<file>`, never one taken from
   GitHub's answer. A download that stops moving is given up.
3. Throws the download away unless its size and SHA-256 match.
4. Unpacks only `ffmpeg.exe`, `ffprobe.exe` and FFmpeg's `LICENSE`, or `deno.exe`, into a new
   folder.
5. Runs each program from there, and keeps it only if it reports exactly the version expected.
   An FFmpeg must also be a GPL build, and never one built with `--enable-nonfree`.
6. Only then puts the new folder in place of the old one. *Cancel* is grayed out for that moment,
   since stopping halfway through could leave the helper missing.

A failure, *Cancel* or a closed window before the last step leaves the copy you had in use, and
whatever was half-downloaded is cleared away (after a closed window, the next time that helper is
downloaded). Only one download of each helper runs at a time, even across two Tapewright windows.
When something goes wrong, the reason says what to do about it, such as trying again in an hour
when GitHub has had too many requests, or checking the computer's date and time when a secure
connection couldn't be made.

## Privacy

Tapewright has no telemetry: it never reports anything about you, your computer or what you
convert. It connects only to these, and only for the reasons given:

- **pypi.org**, to look up yt-dlp's newest version, and **pypi.org** and **files.pythonhosted.org**
  when pip installs or updates yt-dlp.
- **www.gyan.dev** (on Windows) and **dl.deno.land**, to look up the newest FFmpeg and deno. When
  GitHub can't give a download's checksum, gyan.dev's own checksum for FFmpeg stands in.
- **api.github.com**, for a helper's size and SHA-256 before it is downloaded, and to see whether a
  newer Tapewright is out, at most once a day, or an hour after a check that failed.
- **github.com**, where FFmpeg and deno are downloaded from, along with deno's checksum file when
  GitHub's listing has no SHA-256. GitHub sends the files themselves from its own download servers.
- **Wherever winget and `deno upgrade` connect**, when you use the Settings tab to update a copy of
  FFmpeg, deno or Node.js that winget or deno's own updater installed: winget reaches Microsoft's
  winget source and the package's download site, and `deno upgrade` reaches deno's download servers.
- **The sites of the links you paste**, and the servers they send the video from, which yt-dlp
  downloads from.

The version lookups and the release check run when Tapewright checks for updates: as it starts,
unless *Check for updates when Tapewright starts* is off on the Settings tab, and when you do
something that needs a fresh answer, such as clicking *Check for updates*. Nothing is installed or
downloaded until you click, unless you turn on *Update yt-dlp automatically when a new version is
out*. A button that opens a web page, such as a download page, opens it in your own web browser.
Like any website, each of these sees your computer's internet address.

## When a site stops working

Update yt-dlp first, and try the nightly channel if stable is not enough. If it still fails,
*Copy log* on the tab and *Copy diagnostics* on the Settings tab give a bug report everything
it needs.

## Development

```
tapewright/jobs.py          every yt-dlp flag and FFmpeg option, and running a conversion
tapewright/deps.py          finding each tool, checking its version, and the update commands
tapewright/fetch.py         downloading, checking and swapping in Tapewright's own FFmpeg and deno
tapewright/procs.py         child processes: environment, output lines, killing the tree
tapewright/versions.py      comparing version strings from four projects
tapewright/config.py        the settings file
tapewright/help_content.py  every word the Help tab says
tapewright/launch.py        starting up, and explaining a Python that can't run the app
tapewright/app.py           the window and the gates between checks, updates and conversions
tapewright/setup_screen.py  the "Let's get Tapewright ready" page shown in place of the tabs
tapewright/theme.py         every color and font, and the code that applies the VCR look
tapewright/convert_tab.py   the To MP3 and To MP4 tabs
tapewright/deck.py          the cassette and display that show a conversion running
tapewright/settings_tab.py  the Settings tab
tapewright/help_tab.py      the Help tab
tapewright/widgets.py       the log view, the banner, the warning dialog and the edit menu
packaging/build.py          the installer's build steps: python.org's runtime, its checks, staging, sums
packaging/runtime.json      the python.org runtime the installer carries: version, address, SHA-256, Tk
packaging/tapewright.iss    the Inno Setup script that makes TapewrightSetup.exe
packaging/make_icon.py      writes packaging/tapewright.ico from the title bar's cassette
packaging/*.txt, *.md       the text on Setup's Information page and on the release page
tests/test_core.py          everything but fetch.py, with the window built withdrawn
tests/test_fetch.py         fetch.py with fake downloads, and once as a real child cancelled midway
tests/test_packaging.py     the build steps and the icon, and the .iss and release.yml against the code
```

`fetch.py` sits in the package but is a program of its own: the window runs it by its path, and
it never imports the rest of Tapewright.

```
python -m unittest discover -s tests -v
```

GitHub Actions runs the same tests on every push, on Windows and Linux with Python 3.10 and
3.14 (`.github/workflows/test.yml`).

`.github/workflows/release.yml` builds the Windows installer with Inno Setup. A pushed `v<version>`
tag ends in a draft release, published by hand once it has been tried on a spare Windows account;
*Run workflow* builds the same installer as a download from that run and releases nothing. Its
build steps, other than the unit tests and compiling, are subcommands of `packaging/build.py`, so
each can be run locally. Compiling needs Inno Setup 6.6 or later, and its command line is at the top
of `packaging/tapewright.iss`.

Read [AGENTS.md](AGENTS.md) before changing anything in `jobs.py`, `procs.py`, `deps.py`,
`fetch.py`, `app.py`, `setup_screen.py` or `packaging/`. Each rule in it exists because breaking it
fails quietly.

## License

Copyright © 2026 AngrySandhill

Tapewright is free software under the GNU General Public License, version 3 or any later
version (`GPL-3.0-or-later`). The full text is in [LICENSE](LICENSE).

In short, anyone may use, study, change and share it. Anything distributed that is based on
it, including a repackaged `.exe`, must be released under the same license, with its source
code. It comes with no warranty.

Tapewright never ships yt-dlp, FFmpeg or deno, not even in the Windows installer. Each copy of the
app gets them from the projects that make them, and they keep their own licenses.

The Windows installer does include python.org's Python for Windows, unmodified, with the Tcl/Tk and
pip that come with it. Each is under its own license, and their license files are in the installed
`runtime` folder, starting with Python's `LICENSE.txt`.

Contributions are accepted under the same license. Every source file starts with two SPDX
lines naming it, and a test fails if a new file is missing them.
