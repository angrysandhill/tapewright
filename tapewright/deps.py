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
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from tapewright import __version__, procs, versions

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


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def fetch_text(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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


def find_tool(name):
    """shutil.which, then the places installers use before a restart has refreshed PATH.

    A winget upgrade of FFmpeg moves it to a new versioned folder and rewrites PATH in the
    registry, which this already-running process never sees.
    """
    found = shutil.which(name)
    if found:
        return found
    candidates = []
    home = Path.home()
    if WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            winget = Path(local) / "Microsoft" / "WinGet"
            candidates.append(winget / "Links" / f"{name}.exe")
            if name in ("ffmpeg", "ffprobe"):
                candidates += sorted(winget.glob(f"Packages/Gyan.FFmpeg*/*/bin/{name}.exe"), reverse=True)
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
    return shutil.which("winget") or "" if WINDOWS else ""


def _winget_command(winget, verb, package):
    return [winget, verb, "--id", package, "--exact", "--disable-interactivity"]


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
    dep.how = "pip, into the Python running Tapewright"
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

def check_ffmpeg(cache, online):
    dep = Dep("ffmpeg", "FFmpeg", required=True)
    updates = {}
    winget = _winget()
    dep.path, dep.ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if not dep.path:
        dep.state = MISSING
        dep.detail = "Every conversion needs FFmpeg."
        if winget:
            dep.action, dep.command = "Install", _winget_command(winget, "install", "Gyan.FFmpeg")
            dep.how = "winget install Gyan.FFmpeg"
        else:
            dep.how = "Install FFmpeg from ffmpeg.org, make sure ffmpeg is on PATH, then check again."
        return dep, updates
    dep.tool_dirs = [str(Path(dep.path).parent)]

    _, out = procs.run_capture([dep.path, "-version"])
    m = re.search(r"ffmpeg version (\S+)", out)
    full = m.group(1) if m else ""
    dep.installed = versions.short(full) if full else "unknown version"
    package = winget_package_id(dep.path)
    if package and winget:
        dep.command = _winget_command(winget, "upgrade", package)
        dep.how = f"winget upgrade {package}"
    else:
        dep.how = "Update FFmpeg the way you installed it."

    if not dep.ffprobe:
        dep.state = MISSING
        dep.action = "Update" if dep.command else ""
        dep.detail = f"ffmpeg is at {dep.path}, but ffprobe (part of every FFmpeg build) wasn't found."
        return dep, updates
    if not WINDOWS:
        dep.state = OK
        dep.detail = "Your package manager keeps FFmpeg updated, so there is no release to compare with."
        return dep, updates
    if not full or "git" in full or re.match(r"^(N-|\d{4}-\d{2}-\d{2})", full):
        dep.detail = f"{full or 'This'} is a development build, with no release number to compare."
        return dep, updates

    def fetch():
        text = fetch_text(FFMPEG_RELEASE, timeout=15).strip()
        if not versions.parse(text):
            raise ValueError(f"unexpected answer from gyan.dev: {text[:40]!r}")
        return text

    latest, source, reason = _latest("ffmpeg", fetch, cache, online, updates)
    dep.latest = latest
    if latest and versions.is_newer(latest, dep.installed):
        dep.state = OUTDATED
        dep.action = "Update" if dep.command else ""
        dep.detail = f"FFmpeg {latest} is out ({source})."
    elif latest:
        dep.state = OK
        dep.detail = f"Latest release ({source})."
    else:
        dep.detail = f"Couldn't check for updates ({reason})."
    return dep, updates


# ---------------------------------------------------------------- JavaScript runtime

def check_js(choice, cache, online):
    dep = Dep("js", "JavaScript runtime", required=False)
    updates = {}
    if choice == "none":
        dep.state, dep.installed = OK, "turned off"
        dep.detail = "Turned off in Settings. YouTube downloads may fail or be missing formats."
        dep.js_args = ["--no-js-runtimes"]
        return dep, updates
    winget = _winget()
    deno = find_tool("deno") if choice in ("auto", "deno") else ""
    node = find_tool("node") if choice in ("auto", "node") and not deno else ""
    if not (deno or node):
        wanted = {"auto": "deno (or Node.js 22+)", "deno": "deno", "node": "Node.js 22+"}[choice]
        dep.state = MISSING
        dep.detail = (f"No {wanted} found. yt-dlp uses it to solve YouTube's JavaScript "
                      "challenges; without one, downloads can fail or miss formats.")
        if winget and choice != "node":
            dep.action, dep.command = "Install deno", _winget_command(winget, "install", "DenoLand.Deno")
            dep.how = "winget install DenoLand.Deno"
        else:
            dep.how = "Install deno from deno.com or Node.js from nodejs.org, then check again."
        return dep, updates

    name, path = ("deno", deno) if deno else ("node", node)
    dep.name = "deno" if deno else "Node.js"
    dep.path = path
    dep.tool_dirs = [str(Path(path).parent)]
    # yt-dlp splits RUNTIME:PATH on the first colon, so a Windows drive letter is safe here.
    dep.js_args = (["--js-runtimes", f"deno:{path}"] if deno
                   else ["--no-js-runtimes", "--js-runtimes", f"node:{path}"])

    _, out = procs.run_capture([path, "--version"])
    first = out.strip().splitlines()[0] if out.strip() else ""
    words = first.split()
    dep.installed = versions.short(words[1] if deno and len(words) > 1 else first)
    minimum = JS_MINIMUM[name]
    too_old = not versions.at_least(dep.installed, minimum)

    package = winget_package_id(path)
    if package and winget:
        dep.command, dep.how = _winget_command(winget, "upgrade", package), f"winget upgrade {package}"
    elif deno and (os.path.normcase(str(Path(path).parent))
                   == os.path.normcase(str(Path.home() / ".deno" / "bin"))):
        dep.command, dep.how = [path, "upgrade"], "deno upgrade"
    else:
        dep.how = f"Update {dep.name} the way you installed it."

    if too_old:
        dep.state = UNSUPPORTED
        dep.action = "Update" if dep.command else ""
        dep.detail = f"yt-dlp needs {dep.name} {'.'.join(map(str, minimum))} or newer."
        return dep, updates
    if node:
        dep.state = OK
        dep.detail = "Node.js is checked only against yt-dlp's minimum version."
        return dep, updates

    def fetch():
        text = fetch_text(DENO_RELEASE, timeout=15).strip()
        if not versions.parse(text):
            raise ValueError(f"unexpected answer from dl.deno.land: {text[:40]!r}")
        return versions.short(text)

    latest, source, reason = _latest("deno", fetch, cache, online, updates)
    dep.latest = latest
    if latest and versions.is_newer(latest, dep.installed):
        dep.state = OUTDATED
        dep.action = "Update" if dep.command else ""
        dep.detail = f"deno {latest} is out ({source})."
    elif latest:
        dep.state = OK
        dep.detail = f"Latest release ({source})."
    else:
        dep.detail = f"Couldn't check for updates ({reason})."
    return dep, updates


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
        ("js", "JavaScript runtime", False, check_js, (snapshot["js_runtime"],)),
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
