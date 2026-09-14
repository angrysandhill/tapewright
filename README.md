# Tapewright

Turn a link or a local file into an MP3 or an MP4.

Tapewright is a small desktop app on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp) and
[FFmpeg](https://ffmpeg.org). It is plain Python and Tkinter with no other dependencies, and
its Settings tab keeps those tools up to date. When one falls behind, you get a hard warning
before any conversion that uses it.

Only download what you have the right to. Many sites' terms forbid downloading.

## Running it

You need Python 3.10 or newer with Tkinter. The Python from python.org includes Tkinter on
Windows and macOS; on Linux it is often a separate package, such as `python3-tk` on Debian and
Ubuntu.

```
python -m tapewright
```

On Windows you can also double-click `Tapewright.pyw`, which starts it without a console window.
If that Python is too old or has no Tkinter, a message box says so and what to install, rather
than nothing happening at all.

On first launch the Settings tab lists anything that is missing, by what it does: the
Downloader (yt-dlp), the Converter (FFmpeg) and the YouTube helper (deno or Node.js). yt-dlp
installs with one click (through pip, into the same Python). On Windows, FFmpeg and deno install
through winget, Windows' app installer. Before you install or update anything, a box says in
plain words what will happen, including that winget accepts its terms for you. Automatic yt-dlp
updates, if you turn them on, don't ask.

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
to try when something fails. Buttons under each answer open the tab or folder it mentions,
and **Bigger** and **Smaller** change the text size. The *Link or file* and *Save to* boxes
have a right-click Cut, Copy and Paste menu, since that is how many people paste.

## Settings: keeping the tools current

| Tool | Checked against | Updated by |
|---|---|---|
| yt-dlp | PyPI, on the stable or nightly channel | pip, into the Python running Tapewright, pinned to the exact version the check found |
| FFmpeg | the latest release number from gyan.dev (Windows) | `winget upgrade` when it was installed with winget; otherwise the way you installed it |
| deno | dl.deno.land | `deno upgrade`, or winget. Node.js 22+ also works and is checked only against yt-dlp's minimum version |

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
- **Check for updates on startup.** On by default.
- **Offline:** if the online check fails, the last known answer is used. With no known
  answer, yt-dlp still counts as out of date once its version (a release date) passes the
  age you set.
- **YouTube helper:** yt-dlp needs a JavaScript runtime to solve YouTube's challenges. The
  choices are automatic, deno, Node.js, or off.

Settings are saved in `%APPDATA%\Tapewright\settings.json` (Windows),
`~/Library/Application Support/Tapewright` (macOS) or `~/.config/tapewright` (Linux). Set
`TAPEWRIGHT_CONFIG_DIR` to put them somewhere else.

## When a site stops working

Update yt-dlp first, and try the nightly channel if stable is not enough. If it still fails,
*Copy log* on the tab and *Copy diagnostics* on the Settings tab give a bug report everything
it needs.

## Development

```
tapewright/jobs.py          every yt-dlp flag and FFmpeg option, and running a conversion
tapewright/deps.py          finding each tool, checking its version, and the update commands
tapewright/procs.py         child processes: environment, output lines, killing the tree
tapewright/versions.py      comparing version strings from four projects
tapewright/config.py        the settings file
tapewright/help_content.py  every word the Help tab says
tapewright/launch.py        starting up, and explaining a Python that can't run the app
tapewright/app.py           the window and the gates between checks, updates and conversions
tapewright/theme.py         every color and font, and the code that applies the VCR look
tapewright/convert_tab.py   the To MP3 and To MP4 tabs
tapewright/deck.py          the cassette and display that show a conversion running
tapewright/settings_tab.py  the Settings tab
tapewright/help_tab.py      the Help tab
tapewright/widgets.py       the log view, the banner, the warning dialog and the edit menu
tests/test_core.py          everything that needs no network and no installed tools
```

```
python -m unittest discover -s tests -v
```

GitHub Actions runs the same tests on every push, on Windows and Linux with Python 3.10 and
3.14 (`.github/workflows/test.yml`).

Read [AGENTS.md](AGENTS.md) before changing anything in `jobs.py`, `procs.py` or `deps.py`.
Each rule in it exists because breaking it failed quietly.

## License

Copyright © 2026 AngrySandhill

Tapewright is free software under the GNU General Public License, version 3 or any later
version (`GPL-3.0-or-later`). The full text is in [LICENSE](LICENSE).

In short, anyone may use, study, change and share it. Anything distributed that is based on
it, including a repackaged `.exe`, must be released under the same license, with its source
code. It comes with no warranty.

Contributions are accepted under the same license. Every source file starts with two SPDX
lines naming it, and a test fails if a new file is missing them.
