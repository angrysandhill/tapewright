# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""Which tools are installed, whether each is current, and the exact command that updates it.

Checking never raises. A failure becomes a Dep in state "unknown" with a sentence saying
why, because a Settings tab that crashes on a DNS error is the one place nobody can fix
anything from.

Nothing here imports tkinter; check_all() runs on a worker thread and hands back plain data.
"""

import datetime
import importlib.util
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from tapewright import APP_NAME, __version__, procs, versions

WINDOWS = os.name == "nt"
USER_AGENT = f"Tapewright/{__version__} (dependency check)"

OK, OUTDATED, MISSING, UNSUPPORTED, UNKNOWN = "ok", "outdated", "missing", "unsupported", "unknown"
PROBLEMS = (OUTDATED, MISSING, UNSUPPORTED)
STATE_LABELS = {
    OK: "Up to date", OUTDATED: "Out of date", MISSING: "Missing",
    UNSUPPORTED: "Too old for yt-dlp", UNKNOWN: "Couldn't check",
}

# yt-dlp refuses JavaScript runtimes older than these. Mirrored from MIN_SUPPORTED_VERSION in
# yt_dlp/utils/_jsruntime.py as of yt-dlp 2026.08.19 -- re-check them when yt-dlp's release
# notes mention a runtime change.
JS_MINIMUM = {"deno": (2, 3, 0), "node": (22, 0, 0)}

PYPI_YTDLP = "https://pypi.org/pypi/yt-dlp/json"
FFMPEG_RELEASE = "https://www.gyan.dev/ffmpeg/builds/release-version"
DENO_RELEASE = "https://dl.deno.land/release-latest.txt"
RELEASES_API = "https://api.github.com/repos/angrysandhill/tapewright/releases/latest"
RELEASES_PAGE = "https://github.com/angrysandhill/tapewright/releases/latest"

# What each tool does, in the words the window uses before its real name: someone who has never
# heard of yt-dlp can still tell what "Downloader (yt-dlp)" is for.
ROLES = {"yt-dlp": "Downloader", "ffmpeg": "Converter", "js": "YouTube helper"}
# And in a sentence, for the setup screen and the Settings rows, under that name.
ABOUT = {
    "yt-dlp": "Fetches the video or the sound from a link.",
    "ffmpeg": "Makes the MP3 or MP4 file. Every conversion needs it.",
    "js": "Answers YouTube's checks so downloads keep working.",
}

# Tapewright keeps its own copies of FFmpeg and deno in a folder of its own, where fetch.py puts
# them. The variable moves that folder, so tests never touch the real one.
TOOLS_DIR_ENV = "TAPEWRIGHT_TOOLS_DIR"
# Run by its absolute path, never with -m; fetch.py's docstring says why.
FETCH_SCRIPT = Path(__file__).with_name("fetch.py")
# (folder, file) inside tools_dir(). ffprobe comes in the same zip as ffmpeg and lands beside it,
# which is where check_ffmpeg looks for it first.
OWN_COPIES = {"ffmpeg": ("ffmpeg", "ffmpeg.exe"), "ffprobe": ("ffmpeg", "ffprobe.exe"),
              "deno": ("deno", "deno.exe")}
FETCH_SIZES = {"ffmpeg": "about 110 MB", "deno": "about 45 MB"}
# fetch.SWAP_STEP, spelled again because fetch.py imports nothing from Tapewright; a test checks they match.
# App.update_swapping says what the window does from that line.
SWAP_STEP = "Putting it in place…"
OWN_COPY_ACTION = "Get Tapewright's own copy"
OWN_COPY_HOW = "Tapewright's own copy, from github.com"

# winget's exit codes, from doc/windows/package-manager/winget/returnCodes.md in winget-cli.
# Keys are the unsigned form; winget_message() masks whatever sign the code arrived with.
WINGET_UPDATE_NOT_APPLICABLE = 0x8A15002B
WINGET_MESSAGES = {
    WINGET_UPDATE_NOT_APPLICABLE:
        "winget doesn't offer a newer version yet. New releases usually reach winget a few days "
        "after they come out, so try again later.",
    0x8A150046: "winget stopped because its source's terms were not accepted.",
    0x8A150041: "winget stopped because the package's license terms were not accepted.",
    0x8A150008: "The download failed. Check that you're connected to the internet, then try again.",
    0x8A150107: "winget couldn't reach the internet. Check that you're connected, then try again.",
    0x8A150045: "winget couldn't open its list of programs. Check that you're connected to the "
                "internet, then try again.",
    0x8A150105: "There isn't enough free space on this computer. Make some room, then try again.",
    0x8A150101: "The program is running, so it can't be replaced. Close anything that might be "
                "using it, then try again.",
    0x8A150111: "Another program is using it, so it can't be replaced. Close other programs, then "
                "try again.",
    0x8A150102: "Another installation is already running. Wait for it to finish, then try again.",
    0x8A15010C: "The installation was cancelled.",
    0x8A150109: "Restart the computer to finish, then check for updates again.",
    0x8A15003A: "This computer's settings don't allow winget to install programs. Whoever looks "
                "after this computer can change that.",
    0x8A15010F: "This computer's settings don't allow installing programs. Whoever looks after "
                "this computer can change that.",
}


@dataclass
class Dep:
    key: str
    name: str
    required: bool               # a job that uses it cannot run at all without it
    state: str = UNKNOWN
    installed: str = ""
    latest: str = ""
    path: str = ""
    detail: str = ""
    action: str = ""             # button text; "" means there is no one-click fix
    command: list = field(default_factory=list)
    how: str = ""                # how this tool gets updated, in words
    ffprobe: str = ""            # FFmpeg only
    js_args: list = field(default_factory=list)    # JS runtime only: yt-dlp arguments
    tool_dirs: list = field(default_factory=list)  # directories jobs should have on PATH

    @property
    def label(self):
        """The name the window shows: what it does, then what it is called, as in "Downloader (yt-dlp)"."""
        role = ROLES.get(self.key)
        return f"{role} ({self.name})" if role else self.name


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def fetch_text(url, timeout=20, headers=None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _why(error):
    return str(getattr(error, "reason", None) or error)[:160]


def _latest(key, fetch, cache, online, updates):
    """(version, where it came from, why the online check failed or "")."""
    reason = "online checks were skipped"
    if online:
        try:
            version = fetch()
            updates[key] = {"version": version, "checked": _now()}
            return version, "checked just now", ""
        except Exception as e:  # network, TLS, JSON shape: none of it may crash a check
            reason = _why(e)
    cached = cache.get(key)
    if isinstance(cached, dict) and cached.get("version"):
        return cached["version"], f"as of the check on {str(cached.get('checked', ''))[:10]}", reason
    return "", "", reason


def _newest_first(paths):
    """Tools inside versioned folders ('ffmpeg-10.0-full_build/bin/ffmpeg.exe'), newest first.

    Compared as versions: sorted by name, ffmpeg-9.0.1 comes after ffmpeg-10.0 and would win.
    A folder with no version in its name sorts last.
    """
    def key(path):
        m = re.search(r"\d+(?:\.\d+)*", path.parent.parent.name)
        parsed = versions.parse(m.group(0)) if m else None
        return (parsed is not None, parsed or ())

    return sorted(paths, key=key, reverse=True)


def tools_dir():
    """The folder holding Tapewright's own copies of its helpers, or None where it keeps none.

    It is under LOCALAPPDATA, not APPDATA: two big programs have no business roaming with a profile,
    and the folder needs no administrator to write to.
    """
    override = os.environ.get(TOOLS_DIR_ENV)
    if override:
        return Path(override)
    if not WINDOWS:
        return None
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / "AppData" / "Local") / APP_NAME / "tools"


def own_copy(name):
    """Where Tapewright's own copy of ffmpeg, ffprobe or deno is, or would be. None for anything else."""
    tools = tools_dir()
    if tools is None or name not in OWN_COPIES:
        return None
    folder, file = OWN_COPIES[name]
    return tools / folder / file


def is_own_copy(path):
    if not path:
        return False
    here = os.path.normcase(os.path.abspath(str(path)))
    return any(own is not None and os.path.normcase(os.path.abspath(str(own))) == here
               for own in map(own_copy, OWN_COPIES))


def find_tool(name, own=True):
    """Tapewright's own copy, then shutil.which, then the places installers use before a restart
    has refreshed PATH. own=False leaves Tapewright's own copy out, for when that copy won't run.

    The own copy comes first because only the fetcher updates it. An older ffmpeg on PATH that
    shadowed it would still be reported out of date after every successful "Update", forever.
    A winget upgrade of FFmpeg moves it to a new versioned folder and rewrites PATH in the
    registry, which this already-running process never sees. winget also doesn't always make a
    Links entry for a portable package; when it doesn't, it puts the package's own folder on
    PATH instead, so the Packages folders are searched as well.
    """
    mine = own_copy(name) if own else None
    if mine is not None and mine.is_file():
        return str(mine)
    found = shutil.which(name)
    if found and (own or not is_own_copy(found)):
        return found
    candidates = []
    home = Path.home()
    if WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            winget = Path(local) / "Microsoft" / "WinGet"
            candidates.append(winget / "Links" / f"{name}.exe")
            if name in ("ffmpeg", "ffprobe"):
                candidates += _newest_first(winget.glob(f"Packages/Gyan.FFmpeg*/*/bin/{name}.exe"))
            elif name == "deno":
                # deno's winget manifest puts deno.exe at the zip's root. No real install was checked.
                candidates += sorted(winget.glob("Packages/DenoLand.Deno_*/deno.exe"))
        if name == "deno":
            candidates.append(home / ".deno" / "bin" / "deno.exe")
    elif name == "deno":
        candidates.append(home / ".deno" / "bin" / "deno")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def winget_package_id(tool_path):
    """'...\\WinGet\\Packages\\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\\...' -> 'Gyan.FFmpeg'."""
    if not WINDOWS or not tool_path:
        return ""
    real = os.path.realpath(tool_path)  # a WinGet\Links entry is a symlink into Packages
    m = re.search(r"[\\/]WinGet[\\/]Packages[\\/](.+?)_Microsoft\.Winget\.Source_", real, re.IGNORECASE)
    return m.group(1) if m else ""


def _winget():
    return (shutil.which("winget") or "") if WINDOWS else ""


def _winget_command(winget, package):
    """A winget upgrade that can't stop to ask anything.

    --disable-interactivity turns every question into a failure, and a profile that has never
    accepted the winget source's terms gets asked one (0x8A150046). The accept flags answer it,
    so the confirmation before an update must say that terms are being accepted for the user.
    --source winget keeps the Microsoft Store source, and its separate terms, out of it.
    """
    return [winget, "upgrade", "--id", package, "--exact", "--source", "winget", "--disable-interactivity",
            "--accept-source-agreements", "--accept-package-agreements"]


def _program(command):
    """'winget' for 'C:\\...\\WindowsApps\\winget.EXE'. PureWindowsPath splits on both slashes on
    every OS, where Path on Linux would keep a Windows path whole."""
    return PureWindowsPath(str(command[0])).stem.lower() if command else ""


def is_winget(command):
    return _program(command) == "winget"


def fetch_command(tool, version):
    """fetch.py, run like pip through App.run_updates, so Cancel kills it at any byte and the gates hold.

    Pinned to the version the check found, as pip is, so the button installs what it promised. Only the
    number goes on the command line: a check accepts any answer that starts with one, and fetch.py
    refuses anything else, so "9.0.1-essentials_build" would stop it before it began.
    """
    pinned = versions.short(version) if version else "latest"
    return [procs.python_exe(), str(FETCH_SCRIPT), tool, pinned, str(tools_dir())]


def is_fetch(command):
    return len(command) > 2 and PureWindowsPath(str(command[1])).name.lower() == FETCH_SCRIPT.name


def is_pip(command):
    """True for [python, "-m", "pip", ...], the only way yt-dlp is ever installed or updated."""
    return [str(part) for part in command][1:3] == ["-m", "pip"]


def setup_runs(command):
    """True for pip and the fetcher, the only commands the setup screen runs, since it asks nothing first.

    Its rows say that each download comes from pypi.org or github.com. A winget upgrade accepts terms for the
    user, and `deno upgrade` replaces a copy Tapewright never checks, so both stay on the Settings tab, which
    asks first. setup_keys and the screen's judging of an install both follow this one rule.
    """
    return is_pip(command) or is_fetch(command)


# fetch.py's output protocol: STEP, PROGRESS, DONE and ERROR lines, and anything else is for the log.
_FETCH_LINE = re.compile(r"(STEP|DONE|ERROR) (.*\S)|PROGRESS (\d+) (\d+)")


def parse_fetch_line(line):
    """("step", text), ("progress", done, total), ("done", version), ("error", text), or None."""
    m = _FETCH_LINE.fullmatch(line.strip())
    if not m:
        return None
    if m.group(3) is not None:
        return "progress", int(m.group(3)), int(m.group(4))
    return m.group(1).lower(), m.group(2)


def download_hint(dep):
    """About how much running dep.command downloads, and from where, or "" when that isn't known."""
    command = [str(part) for part in dep.command]
    if is_pip(command):
        return "a few MB from pypi.org"
    if is_fetch(command) and command[2] in FETCH_SIZES:
        return f"{FETCH_SIZES[command[2]]} from github.com"
    return ""


def cancel_text(dep, swapped=False):
    """What the update log says after the tool's label when Cancel stopped dep.command.

    The fetcher only swaps folders in its last step, so a kill at any byte before that leaves the copy
    in use untouched, and App clears the leftovers away straight after (the log says what it removed).
    pip and winget promise no such thing.
    swapped means the fetcher had already said it was putting the helper in place. A kill on its way before
    then can still land during the swap, or after it, so nothing can be promised about what is there; the
    check after the batch says.
    """
    if is_fetch(dep.command):
        if swapped:
            return ("cancelled while it was being put in place. The check after it shows what is there "
                    "now.")
        return "cancelled. Nothing was replaced."
    return "update cancelled. It may be half-installed; run the update again."


def winget_message(code):
    """A sentence for a winget exit code, or "" for success.

    Windows hands back the code as an unsigned 32-bit number, and winget's documentation lists
    each one both ways (0x8A150046 is -1978335162), so the code is masked before the lookup.
    """
    if not code:
        return ""
    return WINGET_MESSAGES.get(code & 0xFFFFFFFF, "winget stopped with an error; its output is above.")


def update_failure_text(dep, code, last_error=""):
    """The update log's sentence for dep.command exiting with a non-zero code.

    The fetcher ends a failure with an ERROR line that is already a plain sentence, so last_error,
    when the caller kept one, says more than any exit code could. Its "Try again later" holds whatever
    state the tool is in, since a release not on GitHub yet does arrive in time.

    winget's "try again later" is only true for a tool that is out of date. Anything else that gets its
    no-applicable-update code is never fixed by waiting. It needs the tool reinstalling, and the sentence
    says how, since nothing in the window can do it.
    """
    if is_fetch(dep.command):
        return last_error or f"the download stopped with code {code}; its output is above."
    if not is_winget(dep.command):
        return f"the command exited with code {code}; its output is above."
    unsigned = code & 0xFFFFFFFF
    command = [str(part) for part in dep.command]
    package = command[command.index("--id") + 1] if "--id" in command[:-1] else ""
    reinstall = (f"To reinstall it, open a Command Prompt and run  winget uninstall --id {package}  "
                 "When that finishes, click Check for updates here, then the Install button." if package
                 else "It needs uninstalling and installing again.")
    if unsigned == WINGET_UPDATE_NOT_APPLICABLE and dep.state != OUTDATED:
        text = f"winget has no newer version to install, so updating can't fix this. {reinstall}"
    else:
        text = winget_message(code)
    return f"{text} (winget exit code 0x{unsigned:08X})"


def describe_update(dep):
    """What running dep.command will do, in words for the confirmation before it runs.

    The exact command still goes to the update log. A box that shows raw arguments asks people
    to approve something they cannot read, and winget accepts terms on their behalf, which
    nobody could tell from its flags.
    """
    command = [str(part) for part in dep.command]
    verb = "Install" if dep.action.startswith("Install") else "Update"
    if is_pip(command):
        version = command[-1].partition("==")[2]
        if dep.action == "Switch to stable":
            first = f"Switch the {dep.label} to {version}, the latest stable release."
        elif version:
            first = f"{verb} the {dep.label} {'to ' if verb == 'Update' else ''}version {version}."
        elif verb == "Update":
            first = f"Update the {dep.label} to its newest version."
        else:
            first = f"Install the newest version of the {dep.label}."
        return first + " pip downloads it from pypi.org and installs it into the Python that runs Tapewright."
    if is_fetch(command):
        version = command[3] if len(command) > 3 and command[3] != "latest" else ""
        if dep.action == OWN_COPY_ACTION:
            first = f"Get Tapewright's own copy of the {dep.label}"
        elif verb == "Update":
            first = f"Update the {dep.label} to {f'version {version}' if version else 'its newest version'}"
        else:
            first = f"Install the {dep.label}"
        return (f"{first}, {download_hint(dep) or 'from github.com'}. Tapewright checks the download before "
                "using it and keeps it in its own folder, so nothing else on this computer changes.")
    if is_winget(command):
        return (f"{verb} the {dep.label} with winget, Windows' app installer, which fetches the newest "
                "version it offers. This accepts the program's license terms and the terms of winget's "
                "source for you.")
    if command[1:2] == ["upgrade"] and _program(command) == "deno":
        return f"Update the {dep.label} to its newest release, using deno's own updater."
    return f"{verb} the {dep.label}."


# ---------------------------------------------------------------- yt-dlp

def ytdlp_install_command(target, extras):
    """pip, pinned to the exact version the check found, so "Update" installs what it promised.

    Pinning is also what makes switching channels work in both directions: a plain
    --upgrade will never go from a nightly back down to the latest stable release.
    """
    package = "yt-dlp[default]" if extras else "yt-dlp"
    spec = f"{package}=={target}" if target else package
    return [procs.python_exe(), "-m", "pip", "install", "--upgrade", "--disable-pip-version-check",
            "--no-input", "--progress-bar", "off", spec]


def latest_ytdlp(channel):
    data = json.loads(fetch_text(PYPI_YTDLP))
    if channel == "stable":
        return data["info"]["version"]
    best = None
    for version, files in (data.get("releases") or {}).items():
        if ".dev" not in version or not files or all(f.get("yanked") for f in files):
            continue
        parsed = versions.parse(version)
        if parsed and (best is None or parsed > best[0]):
            best = (parsed, version)
    if best is None:
        raise ValueError("PyPI lists no nightly builds of yt-dlp")
    return best[1]


def check_ytdlp(channel, extras, max_age_days, cache, online):
    dep = Dep("yt-dlp", "yt-dlp", required=True)
    dep.how = "pip, into the Python that runs Tapewright"
    updates = {}
    code, out = procs.run_capture([procs.python_exe(), "-m", "yt_dlp", "--version"], timeout=60)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    broken = ""
    if code == 0 and lines and versions.parse(lines[-1]):
        dep.installed = lines[-1]
        spec = importlib.util.find_spec("yt_dlp")
        dep.path = str(Path(spec.origin).parent) if spec and spec.origin else ""
    elif not (code is not None and "No module named yt_dlp" in out):
        broken = (lines[-1] if lines else "") or f"exit code {code}"

    latest, source, reason = _latest(f"yt-dlp:{channel}", lambda: latest_ytdlp(channel),
                                     cache, online, updates)
    dep.latest = latest
    if not dep.installed:
        dep.state = MISSING
        dep.action, dep.command = "Install", ytdlp_install_command(latest, extras)
        dep.detail = (f"yt-dlp is installed but won't run: {broken}" if broken
                      else "Not installed in this Python. Links can't be downloaded without it.")
    elif latest and versions.is_newer(latest, dep.installed):
        dep.state = OUTDATED
        dep.action, dep.command = "Update", ytdlp_install_command(latest, extras)
        dep.detail = (f"{latest} is available on the {channel} channel ({source}). Sites change "
                      "constantly, and an old yt-dlp fails or quietly picks worse formats.")
    elif latest:
        dep.state = OK
        if channel == "stable" and versions.is_newer(dep.installed, latest):
            dep.action, dep.command = "Switch to stable", ytdlp_install_command(latest, extras)
            dep.detail = f"A nightly build is installed; the latest stable release is {latest}."
        else:
            dep.detail = f"Latest on the {channel} channel ({source})."
    else:
        released = versions.release_date(dep.installed)
        age = (datetime.date.today() - released).days if released else None
        if age is not None and age > max_age_days:
            dep.state = OUTDATED
            dep.action, dep.command = "Update", ytdlp_install_command("", extras)
            dep.detail = (f"Couldn't check online ({reason}), but this version is {age} days old. "
                          f"yt-dlp older than {max_age_days} days usually fails on YouTube.")
        else:
            dep.detail = f"Couldn't check for updates ({reason})."
    return dep, updates


# ---------------------------------------------------------------- FFmpeg

def _no_version(code, out):
    """Why a program named no version, in a few words: run_capture's own sentence, the program's first line,
    or its exit code."""
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if lines:
        return lines[0][:160].rstrip(".")
    return f"it exited with code {code} and printed nothing" if code else "it printed nothing"


def _ffmpeg_version(path):
    """(the version `ffmpeg -version` names, or "", and why it named none)."""
    code, out = procs.run_capture([path, "-version"])
    m = re.search(r"ffmpeg version (\S+)", out)
    return (m.group(1), "") if m else ("", _no_version(code, out))


def check_ffmpeg(cache, online):
    dep = Dep("ffmpeg", "FFmpeg", required=True)
    updates = {}
    winget = _winget()
    dep.path = find_tool("ffmpeg")
    full, why = _ffmpeg_version(dep.path) if dep.path else ("", "")
    # Why Tapewright's own copy won't run, when it doesn't. A policy added later, an antivirus, or a copy made
    # by hand without its DLLs can stop it, and find_tool picks it first every time, so any other copy that
    # answers with a version is used instead.
    broken = why if dep.path and not full and is_own_copy(dep.path) else ""
    if broken:
        other = find_tool("ffmpeg", own=False)
        other_full = _ffmpeg_version(other)[0] if other else ""
        if other_full:
            dep.path, full = other, other_full

    def finish():
        if broken and full:
            dep.detail = f"Tapewright's own copy won't run ({broken}), so another copy is used. {dep.detail}"
        return dep, updates

    # The ffprobe beside ffmpeg first, so the two come from one install whenever that install has
    # both. Only then a separate search: an ffmpeg copied alone into a folder on PATH still works
    # for local files, and calling it missing would block every conversion. yt-dlp itself only ever
    # looks beside the ffmpeg it is given, so a fallback ffprobe serves local-file jobs alone.
    beside = Path(dep.path).with_name("ffprobe" + Path(dep.path).suffix) if dep.path else None
    if beside and beside.is_file():
        dep.ffprobe = str(beside)
    else:  # past Tapewright's own ffprobe too, when its ffmpeg was passed over for not running
        dep.ffprobe = find_tool("ffprobe", own=False) if broken and full else find_tool("ffprobe")

    def fetch():
        text = fetch_text(FFMPEG_RELEASE, timeout=15).strip()
        if not versions.parse(text):
            raise ValueError(f"unexpected answer from gyan.dev: {text[:40]!r}")
        return text

    if not dep.path:
        dep.state = MISSING
        dep.detail = "Every conversion needs FFmpeg."
        if WINDOWS:
            # Tapewright's own copy, never winget: not every Windows has winget, and it accepts terms on the
            # way. Looked up before returning, so Install fetches the version this check reports.
            dep.latest = _latest("ffmpeg", fetch, cache, online, updates)[0]
            dep.action, dep.command = "Install", fetch_command("ffmpeg", dep.latest)
            dep.how = OWN_COPY_HOW
        else:
            dep.how = "Install FFmpeg from ffmpeg.org, make sure ffmpeg is on PATH, then check again."
        return dep, updates
    dep.tool_dirs = [str(Path(dep.path).parent)]
    dep.installed = versions.short(full) if full else ("" if broken else "unknown version")
    own = is_own_copy(dep.path)
    package = winget_package_id(dep.path)
    if own:
        dep.how = OWN_COPY_HOW
    elif package and winget:
        dep.command = _winget_command(winget, package)
        dep.how = f"winget upgrade {package}"
    else:
        dep.how = "Update FFmpeg the way you installed it."
    # Offered to any other copy that needs fixing. Tapewright's own copy then takes over, since
    # find_tool looks there first, and the other copy is left exactly where it is.
    elsewhere = "Update FFmpeg the way you installed it, or let Tapewright keep its own copy."

    if not dep.ffprobe:
        dep.state = MISSING
        dep.detail = f"ffmpeg is at {dep.path}, but ffprobe (part of every FFmpeg build) wasn't found."
        if WINDOWS:
            # Even for winget's copy: upgrading can't bring back a file gone from the version winget
            # already has, while the fetcher always unpacks ffmpeg and ffprobe together.
            dep.latest = _latest("ffmpeg", fetch, cache, online, updates)[0]
            dep.action = "Install" if own else OWN_COPY_ACTION
            dep.command = fetch_command("ffmpeg", dep.latest)
            dep.how = OWN_COPY_HOW if own else elsewhere
        else:
            dep.action = "Update" if dep.command else ""
        return finish()
    if broken and not full:
        # Nobody can tell whether it is out of date, so it is never called that. It still gets the button that
        # fetches it again, since otherwise only deleting its folder by hand would get FFmpeg working.
        dep.detail = f"Tapewright's own copy of FFmpeg won't run: {broken}."
        if WINDOWS:
            dep.latest = _latest("ffmpeg", fetch, cache, online, updates)[0]
            dep.action, dep.command = "Install", fetch_command("ffmpeg", dep.latest)
        return dep, updates
    if not WINDOWS:
        dep.state = OK
        dep.detail = "Your package manager keeps FFmpeg updated, so there is no release to compare with."
        return finish()
    if not full:
        dep.detail = f"Couldn't read the version of FFmpeg at {dep.path}: {why}."
        return dep, updates
    if "git" in full or re.match(r"^(N-|\d{4}-\d{2}-\d{2})", full):
        dep.detail = f"{full} is a development build, with no release number to compare."
        return finish()

    latest, source, reason = _latest("ffmpeg", fetch, cache, online, updates)
    dep.latest = latest
    if latest and versions.is_newer(latest, dep.installed):
        dep.state = OUTDATED
        dep.detail = f"FFmpeg {latest} is out ({source})."
        if dep.command:
            dep.action = "Update"
        else:
            dep.action = "Update" if own else OWN_COPY_ACTION
            dep.command = fetch_command("ffmpeg", latest)
            dep.how = OWN_COPY_HOW if own else elsewhere
    elif latest:
        dep.state = OK
        dep.detail = f"Latest release ({source})."
    else:
        dep.detail = f"Couldn't check for updates ({reason})."
    return finish()


# ---------------------------------------------------------------- JavaScript runtime

def _js_version(path, deno):
    """(the version `--version` names, or "", and why it named none). deno says "deno 2.9.6 (...)"."""
    code, out = procs.run_capture([path, "--version"])
    first = out.strip().splitlines()[0] if out.strip() else ""
    words = first.split()
    version = versions.short(words[1] if deno and len(words) > 1 else first)
    return (version, "") if versions.parse(version) else ("", _no_version(code, out))


def check_js(choice, cache, online):
    dep = Dep("js", "deno or Node.js", required=False)
    updates = {}
    if choice == "none":
        dep.state, dep.installed = OK, "turned off"
        dep.detail = "Turned off in Settings. YouTube downloads may fail or be missing formats."
        dep.js_args = ["--no-js-runtimes"]
        return dep, updates
    winget = _winget()
    deno = find_tool("deno") if choice in ("auto", "deno") else ""
    node = find_tool("node") if choice in ("auto", "node") and not deno else ""

    def fetch():
        text = fetch_text(DENO_RELEASE, timeout=15).strip()
        if not versions.parse(text):
            raise ValueError(f"unexpected answer from dl.deno.land: {text[:40]!r}")
        return versions.short(text)

    if not (deno or node):
        wanted = {"auto": "deno (or Node.js 22+)", "deno": "deno", "node": "Node.js 22+"}[choice]
        # Named after what would fill the gap, so "YouTube helper (deno)" says which program the
        # Install button fetches.
        dep.name = {"auto": "deno or Node.js", "deno": "deno", "node": "Node.js"}[choice]
        dep.state = MISSING
        dep.detail = (f"No {wanted} found. yt-dlp uses it to solve YouTube's JavaScript "
                      "challenges; without one, downloads can fail or miss formats.")
        if WINDOWS and choice != "node":
            # Tapewright's own deno, never winget, for the same reasons as FFmpeg. Node.js never gets an
            # install button: it is only ever used when someone already has it.
            dep.name = "deno"
            dep.latest = _latest("deno", fetch, cache, online, updates)[0]
            dep.action, dep.command = "Install deno", fetch_command("deno", dep.latest)
            dep.how = OWN_COPY_HOW
        else:
            dep.how = "Install deno from deno.com or Node.js from nodejs.org, then check again."
        return dep, updates

    name, path = ("deno", deno) if deno else ("node", node)
    installed, why = _js_version(path, bool(deno))
    # As for FFmpeg: Tapewright's own deno that won't run gives way to any other deno that answers.
    broken = why if deno and not installed and is_own_copy(path) else ""
    if broken:
        other = find_tool("deno", own=False)
        other_installed = _js_version(other, True)[0] if other else ""
        if other_installed:
            deno = path = other
            installed = other_installed
        elif choice == "auto":
            # And on Automatic, a Node.js new enough for yt-dlp, as when there is no deno at all. Otherwise
            # the own deno, found first every time, would hide it for good, and every link would get the deno
            # that won't run. A Node.js that is too old isn't taken: it would trade the button that fetches
            # deno again for a warning before every link that nothing here can fix.
            other = find_tool("node")
            other_installed = _js_version(other, False)[0] if other else ""
            if other_installed and versions.at_least(other_installed, JS_MINIMUM["node"]):
                deno, node, name, path = "", other, "node", other
                installed = other_installed

    def finish():
        if broken and installed:
            dep.detail = f"Tapewright's own copy won't run ({broken}), so another copy is used. {dep.detail}"
        return dep, updates

    dep.name = "deno" if deno else "Node.js"
    dep.path = path
    dep.tool_dirs = [str(Path(path).parent)]
    # yt-dlp splits RUNTIME:PATH on the first colon, so a Windows drive letter is safe here.
    dep.js_args = (["--js-runtimes", f"deno:{path}"] if deno
                   else ["--no-js-runtimes", "--js-runtimes", f"node:{path}"])
    dep.installed = installed

    own = bool(deno) and is_own_copy(path)
    package = winget_package_id(path)
    if own:
        dep.how = OWN_COPY_HOW
    elif package and winget:
        dep.command, dep.how = _winget_command(winget, package), f"winget upgrade {package}"
    elif deno and (os.path.normcase(str(Path(path).parent))
                   == os.path.normcase(str(Path.home() / ".deno" / "bin"))):
        dep.command, dep.how = [path, "upgrade"], "deno upgrade"
    else:
        dep.how = f"Update {dep.name} the way you installed it."

    if not installed:
        # "Can't tell" is never a problem state. A deno that gave no answer while an antivirus scanned
        # it is probably fine, and calling it too old would offer an update it doesn't need.
        if broken:
            # But Tapewright's own copy gets the button that fetches it again, as FFmpeg's does.
            dep.detail = f"Tapewright's own copy of deno won't run: {broken}."
            if WINDOWS:
                dep.latest = _latest("deno", fetch, cache, online, updates)[0]
                dep.action, dep.command = "Install", fetch_command("deno", dep.latest)
            return dep, updates
        dep.detail = f"Couldn't read the version of {dep.name} at {path}: {why}."
        return dep, updates
    minimum = JS_MINIMUM[name]
    supported = versions.at_least(dep.installed, minimum)
    if node:
        if supported:
            dep.state = OK
            dep.detail = "Node.js is checked only against yt-dlp's minimum version."
        else:
            dep.state = UNSUPPORTED
            dep.action = "Update" if dep.command else ""
            dep.detail = f"yt-dlp needs {dep.name} {'.'.join(map(str, minimum))} or newer."
        return finish()  # which says first why Node.js is used, when Tapewright's own deno won't run

    # Looked up even for a deno that is too old, so a fetch replacing it is pinned like any other.
    latest, source, reason = _latest("deno", fetch, cache, online, updates)
    dep.latest = latest
    if not supported:
        dep.state = UNSUPPORTED
        dep.detail = f"yt-dlp needs {dep.name} {'.'.join(map(str, minimum))} or newer."
    elif latest and versions.is_newer(latest, dep.installed):
        dep.state = OUTDATED
        dep.detail = f"deno {latest} is out ({source})."
    elif latest:
        dep.state = OK
        dep.detail = f"Latest release ({source})."
        return finish()
    else:
        dep.detail = f"Couldn't check for updates ({reason})."
        return finish()
    if dep.command:
        dep.action = "Update"
    elif WINDOWS:
        # Only the fetcher updates Tapewright's own deno. Any other copy is offered one of Tapewright's
        # own, which find_tool then picks first, leaving that copy where it is.
        dep.action = "Update" if own else OWN_COPY_ACTION
        dep.command = fetch_command("deno", latest)
        if not own:
            dep.how = "Update deno the way you installed it, or let Tapewright keep its own copy."
    return finish()


# ---------------------------------------------------------------- all together

def _guarded(key, name, required, check, *args):
    try:
        return check(*args)
    except Exception as e:  # a bug in a check must not take the other checks down with it
        return Dep(key, name, required, detail=f"The check itself failed: {e!r}"), {}


def check_all(snapshot, online=True):
    """Check every dependency. Returns ({key: Dep}, cache updates to merge into settings).

    snapshot is a plain dict copy of the settings, so this can run on a worker thread
    without touching the live Settings object.
    """
    cache = dict(snapshot.get("latest_cache") or {})
    deps, updates = {}, {}
    for key, name, required, check, args in (
        ("yt-dlp", "yt-dlp", True, check_ytdlp,
         (snapshot["ytdlp_channel"], snapshot["ytdlp_extras"], snapshot["offline_max_age_days"])),
        ("ffmpeg", "FFmpeg", True, check_ffmpeg, ()),
        ("js", "deno or Node.js", False, check_js, (snapshot["js_runtime"],)),
    ):
        dep, found = _guarded(key, name, required, check, *args, cache, online)
        deps[key] = dep
        updates.update(found)
    return deps, updates


def used_by(kind):
    """The dependencies a job reads: a link uses all three, a local file only FFmpeg."""
    return ("yt-dlp", "ffmpeg", "js") if kind == "url" else ("ffmpeg",)


def problems_for(deps, kind):
    return [deps[k] for k in used_by(kind) if k in deps and deps[k].state in PROBLEMS]


def blocking(problems, policy):
    """A missing required tool always blocks. Anything else blocks only under the "block" policy."""
    return [d for d in problems if (d.state == MISSING and d.required) or policy == "block"]


# ---------------------------------------------------------------- the setup screen

def unusable(dep):
    """True for a helper that is missing, out of date or too old, or that is there but won't run.

    The last is UNKNOWN with no version read, which for FFmpeg means Tapewright's own copy won't run and for
    a JavaScript runtime that no version could be read at all. UNKNOWN with a version is a helper that works
    but couldn't be checked for updates, and is left out. It is not PROBLEMS: the conversion gate and
    setup_needed still leave "can't tell" alone. It is for judging an install, which a copy that won't run
    hasn't achieved.
    """
    return dep.state in PROBLEMS or (dep.state == UNKNOWN and not dep.installed)


def setup_keys(deps):
    """What the setup screen installs, in the order it installs them: each unusable tool whose command is one
    the screen runs (setup_runs), pip or the fetcher.

    A copy that won't run counts, so that when a fetched helper fails that way, Try again has something to
    fetch.
    """
    return [key for key in ("yt-dlp", "ffmpeg", "js")
            if key in deps and unusable(deps[key]) and setup_runs(deps[key].command)]


def setup_needed(deps, settings):
    """Whether the setup screen has something worth offering.

    Without yt-dlp no link converts, and without FFmpeg nothing does, so either one missing brings the
    screen back. The YouTube helper is optional: missing, it counts only until setup has been finished
    once, and never when Settings turned it off. Out of date is the Settings tab's business. A missing
    helper counts only when the screen could install that helper itself: outside Windows nothing fetches
    FFmpeg, and an update for yt-dlp is no reason to show a screen that can't bring FFmpeg back.
    """
    keys = setup_keys(deps)

    def missing(key):
        return key in keys and deps[key].state == MISSING

    return (missing("yt-dlp") or missing("ffmpeg")
            or (not settings["setup_done"] and missing("js") and settings["js_runtime"] != "none"))


# ---------------------------------------------------------------- Tapewright itself

def check_release(cache, online, now=None):
    """(release, cache updates). release is {"version", "url"} when a newer Tapewright is out, else None.

    It never raises, and it is never a Dep: "Update everything", the red warning and the conversion
    gate all walk app.deps, while an old Tapewright converts as well as a new one. GitHub is asked at
    most once a day, since its API limits unauthenticated calls per address and the fetcher needs them
    too, and a 404 (nothing released yet) is remembered like any other answer. Offline, or when GitHub
    can't be reached, the last answer stands. A failed attempt is remembered too, for an hour, so a
    blocked or rate-limited GitHub isn't asked again on every check. The page opened is always
    RELEASES_PAGE, never a URL taken from the answer.
    """
    now = now or datetime.datetime.now()
    updates = {}
    cached = cache.get("tapewright") if isinstance(cache, dict) else None
    cached = cached if isinstance(cached, dict) else {}
    latest = str(cached.get("version") or "")
    try:
        age = now - datetime.datetime.fromisoformat(str(cached.get("checked")))
        # An hour after a failure rather than a day: long enough to spare the allowance the fetcher shares,
        # short enough that a connection that comes back is noticed the same afternoon.
        limit = datetime.timedelta(hours=1) if cached.get("failed") else datetime.timedelta(days=1)
        fresh = datetime.timedelta(0) <= age < limit  # a clock put back isn't fresh
    except (TypeError, ValueError):  # never checked, or a damaged entry
        fresh = False
    if online and not fresh:
        stamp = now.replace(microsecond=0).isoformat()
        try:
            answer = json.loads(fetch_text(RELEASES_API, timeout=15,
                                           headers={"Accept": "application/vnd.github+json"}))
            tag = answer.get("tag_name") if isinstance(answer, dict) else None
            if not isinstance(tag, str) or not versions.parse(tag):
                raise ValueError("GitHub's answer names no version")
            latest = versions.short(tag)
            updates["tapewright"] = {"version": latest, "checked": stamp}
        except Exception as e:  # network, TLS, JSON shape, rate limits: none of it may crash a check
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:  # the repository has no release yet
                latest = ""
                updates["tapewright"] = {"version": "", "checked": stamp}
            else:  # the last answer stands, marked so that it is asked again in an hour
                updates["tapewright"] = {"version": latest, "checked": stamp, "failed": True}
    release = {"version": latest, "url": RELEASES_PAGE} if versions.is_newer(latest, __version__) else None
    return release, updates
