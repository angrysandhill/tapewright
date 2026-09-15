# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tapewright's own copy of FFmpeg or deno: downloaded, checked, test-run, and only then put in place.

    python <path to this file> ffmpeg|deno <version|latest> <tools folder>

App.run_updates starts this as a child through procs.Runner, the way it starts pip, so Cancel reaches it
the same way (taskkill /F /T, at any byte), and a tool still can't be replaced while a job is using it.
It is run by its path and never imports tapewright: `-m tapewright.fetch` would depend on the working
folder, which nothing here controls. Standard library only, like the rest of the app.

Everything it prints is one line, and deps.parse_fetch_line reads exactly these:

    STEP <sentence>            what it is doing now
    PROGRESS <done> <total>    bytes downloaded, at most twice a second and once at the end
    DONE <version>             the last line on success
    ERROR <sentence>           the last line on failure, before a non-zero exit

Any other line is detail for the update log. The window shows the ERROR sentence, never the exit code, which
only sorts failures roughly: 0 done; 1 wrong arguments or not 64-bit Windows; 2 network, a rate limit, a
certificate, a stalled download, or a release not on GitHub yet; 3 a download or test-run output that failed
a check; 4 disk space; 5 everything else (unpacking, a test run that couldn't start, timed out or exited with
an error, an unwritable folder, a refused swap, another window already getting the tool, or the unexpected).

The working copy isn't touched until the new one has passed every check, so a failure or a kill anywhere
before the final swap leaves the copy that already worked in use. The swap is announced by SWAP_STEP and
starts SWAP_PAUSE after it, and two runs for the same tool never overlap (see lock()).
"""

import contextlib
import errno
import fnmatch
import hashlib
import http.client
import json
import math
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import fcntl
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MB = 1024 * 1024

RECIPES = {
    "ffmpeg": {
        "label": "FFmpeg",
        "latest": "https://www.gyan.dev/ffmpeg/builds/release-version",  # plain text, such as 9.0.1
        "repo": "GyanD/codexffmpeg",
        "tag": "{v}",
        "asset": "ffmpeg-{v}-essentials_build.zip",
        # (pattern inside the zip, the fixed name it is written as, whether the download is useless
        # without it). The 9.0.1 zip holds ffmpeg-9.0.1-essentials_build/bin/ffmpeg.exe (seen in a real
        # download on 2026-09-15); the folder is still a pattern, and a missing member fails with its name.
        "members": [("*/bin/ffmpeg.exe", "ffmpeg.exe", True), ("*/bin/ffprobe.exe", "ffprobe.exe", True),
                    ("*/LICENSE", "LICENSE", False)],
        "tests": ("ffmpeg", "ffprobe"),
        # gyan.dev's own checksum, for when the GitHub API can't answer. It always describes gyan.dev's
        # newest release, so it only counts while that is still the version being installed.
        "checksum": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256",
        # About what the members take once unpacked: 9.0.1's came to 196 MB in a real download. It is used
        # only to refuse a disk that is too full before the download starts; the exact figure from the zip
        # is checked again at unpacking.
        "unpacked": 200 * MB,
        # The most a download may be when neither GitHub nor the server says how big it is: about twice
        # the 9.0.1 zip's 111 MB, so a body that never ends can't fill the disk.
        "ceiling": 250 * MB,
    },
    "deno": {
        "label": "deno",
        "latest": "https://dl.deno.land/release-latest.txt",  # plain text, such as v2.9.6
        "repo": "denoland/deno",
        "tag": "v{v}",
        "asset": "deno-x86_64-pc-windows-msvc.zip",
        "members": [("deno.exe", "deno.exe", True)],
        "tests": ("deno",),
        "checksum": "",  # the release's own .zip.sha256sum asset, beside the zip
        "unpacked": 120 * MB,
        "ceiling": 100 * MB,  # about twice the 2.9.6 zip's 43 MB
    },
}

# Each program a recipe test-runs: (its version option, how the first line it prints starts).
TESTS = {
    "ffmpeg": ("-version", "ffmpeg version {v}"),
    "ffprobe": ("-version", "ffprobe version {v}"),
    "deno": ("--version", "deno {v} "),
}

OK, USAGE, NETWORK, VERIFY, DISK, UNPACK = 0, 1, 2, 3, 4, 5

# The STEP printed just before swap(); deps.SWAP_STEP is the same text. From that line on, the window stops
# offering Cancel and waits for the run to end rather than kill it, because a kill between swap()'s two
# renames leaves the tool missing until the next run puts it back.
SWAP_STEP = "Putting it in place…"
# Seconds between that line and the swap. A Cancel clicked just before the window reads the line is still
# accepted, and its kill takes 66-84 ms to land (measured with taskkill /F /T), long enough for the swap to
# have started. Waiting well past that lets such a kill land while nothing has been moved. It makes that
# much less likely, not impossible: a busy computer can be slower to start taskkill.
SWAP_PAUSE = 1.0

# The sentences after ERROR, which the window shows as they are.
NETWORK_ERROR = "Couldn't reach {host}. Check that you're connected to the internet, then try again."
RATE_LIMITED = "GitHub has had too many requests from this connection. Try again in an hour."
CERTIFICATE = ("A secure connection couldn't be made. Check that this computer's date and time are right, "
               "then try again.")
STALLED = "The download from github.com stopped moving. Check your internet connection, then try again."
NOT_YET = "The newest {label} isn't on github.com yet. Try again later."
DAMAGED = "The download was damaged on the way, so it was thrown away. Try again."
NO_SPACE = ("There isn't enough free space on this computer. The {label} needs about {mb} MB. "
            "Make some room, then try again.")
NO_MEMBER = "The download didn't contain {name}, so nothing was changed."
BLOCKED = "Windows stopped {label} from running. This computer may only allow approved programs."
FAILED_TEST = "The downloaded {label} didn't work when tested, so it wasn't used."
NONFREE = "This FFmpeg build can't be used, so it wasn't kept."
UNSUPPORTED = "Tapewright can only download its own {label} on 64-bit Windows."
ALREADY = "Tapewright is already getting {label} in another window. Wait for it to finish, then try again."
# Failures nobody expects in normal use. Each still says that nothing was changed, because nothing was.
BAD_ARGUMENTS = "Tapewright started its downloader the wrong way, so nothing was changed."
NO_UNPACK = "The download couldn't be unpacked, so nothing was changed."
CANT_WRITE = "Tapewright couldn't save files in its own folder, so nothing was changed."
IN_USE = "{label} is in use, so it wasn't replaced. Close anything that might be using it, then try again."
UNEXPECTED = "Something went wrong while getting {label}, so nothing was changed. The details are above."

HEADERS = {"User-Agent": "Tapewright-fetch"}
API_HEADERS = dict(HEADERS, Accept="application/vnd.github+json")
TIMEOUT = 30                  # seconds without a byte before a request fails, or a download counts as stalled
TEST_TIMEOUT = 60
CHUNK = MB
MARGIN = 64 * MB              # room left over after the download and after unpacking
PROGRESS_EVERY = 0.5          # seconds
# A download, or the answer to a lookup, that brings fewer than STALL_BYTES in STALL_SECONDS has stopped
# moving, even when a trickle of bytes keeps every read inside TIMEOUT.
STALL_SECONDS = 60
STALL_BYTES = 64 * 1024
# The most a lookup's answer may be. GitHub's answer for a release, a version number and a checksum file are
# all a few kilobytes, so anything past this isn't one, and an answer that never ends can't fill the memory.
ANSWER_LIMIT = MB

_VERSION = re.compile(r"\d+(?:\.\d+)*")
_HEX64 = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{64}(?![0-9A-Za-z])")
_OTHER_ALGORITHM = re.compile(r"(?i)(?:sha|md|blake)[\w-]*")


class Failure(Exception):
    """Stops the run with an exit code and the sentence for its ERROR line."""

    def __init__(self, code, sentence):
        super().__init__(sentence)
        self.code, self.sentence = code, sentence


class TooLong(ValueError):
    """An answer longer than it can be. A ValueError, like any other answer that can't be used."""


# ---------------------------------------------------------------- pure helpers

def parse_digest(text):
    """The SHA-256 in text as lowercase hex, or None.

    Takes GitHub's "sha256:<hex>", bare hex (gyan.dev's file), or a checksum file. deno's .zip.sha256sum
    held PowerShell's Get-FileHash output when it was read on 2026-09-14 (Algorithm, Hash and Path lines,
    the hash in upper case), so the first word of exactly 64 hex digits is taken rather than a fixed
    position. A digest labeled with another algorithm is never read as SHA-256.
    """
    if not isinstance(text, str):
        return None
    prefix, colon, _ = text.strip().partition(":")
    if colon and _OTHER_ALGORITHM.fullmatch(prefix) and prefix.lower() != "sha256":
        return None
    m = _HEX64.search(text)
    return m.group(0).lower() if m else None


def pick_asset(release, name):
    """(size, sha256) of the asset named exactly name in a GitHub release, or None when there isn't one.

    Either value is None when GitHub doesn't give a usable one. Its browser_download_url is never read:
    the download address is built from the recipe, so an answer can't point the download elsewhere.
    """
    assets = release.get("assets") if isinstance(release, dict) else None
    for asset in assets if isinstance(assets, list) else ():
        if isinstance(asset, dict) and asset.get("name") == name:
            size = asset.get("size")
            usable = isinstance(size, int) and not isinstance(size, bool) and size > 0
            return (size if usable else None), parse_digest(asset.get("digest"))
    return None


def _matches(member, pattern):
    """Folder by folder, so the * in */bin/ffmpeg.exe is one folder and never several."""
    parts, wanted = member.replace("\\", "/").lower().split("/"), pattern.lower().split("/")
    return len(parts) == len(wanted) and all(fnmatch.fnmatchcase(p, w) for p, w in zip(parts, wanted))


def plan_members(zip_names, recipe):
    """{fixed name: member in the zip} for the recipe's members, each matched exactly once.

    A required member that is missing, or matched twice, raises Failure; an optional one is left out.
    """
    plan = {}
    for pattern, fixed, required in recipe["members"]:
        found = [name for name in zip_names if _matches(name, pattern)]
        if len(found) == 1:
            plan[fixed] = found[0]
        elif required:
            raise Failure(UNPACK, NO_MEMBER.format(name=fixed) if not found else NO_UNPACK)
    return plan


def check_output(tool, version, text):
    """"" when a test run printed what the downloaded build should, otherwise the sentence to fail with.

    tool is the program that ran: "ffmpeg", "ffprobe" or "deno". The version must match exactly, since
    "ffmpeg version 9.0.10" starts with "ffmpeg version 9.0.1" too. FFmpeg must also be a GPL build and
    never a nonfree one, which nobody may pass on (see Licensing in AGENTS.md).
    """
    label = next(recipe["label"] for recipe in RECIPES.values() if tool in recipe["tests"])
    first = next((line.lstrip() for line in text.splitlines() if line.strip()), "")
    start = TESTS[tool][1].format(v=version)
    if not first.startswith(start) or re.match(r"\.?\d", first[len(start):]):
        return FAILED_TEST.format(label=label)
    words = text.split()
    if tool == "ffmpeg" and ("--enable-nonfree" in words or "--enable-gpl" not in words):
        return NONFREE
    return ""


def supported():
    """64-bit Windows, which every recipe's download is built for. Windows 11 on ARM runs x64 programs."""
    return WINDOWS and platform.machine().lower() in ("amd64", "x86_64", "arm64")


def _remove(path):
    """Delete a file or folder if it is there, and never fail over it: this is only ever tidying up."""
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def lock(tools_dir, tool):
    """Take tool for this run, so that no other run uses its staging names at the same time.

    Returns the lock file's descriptor, for unlock(), or None when another run already has the tool. Two
    Tapewright windows can each start a fetch of FFmpeg, and without this the second one's recover() would
    clear away the first one's download or unpacked folder while the first was still checking it. The hold
    is a lock on byte 0 of tools_dir/.<tool>.lock, which the system lets go of when the process ends, even
    by taskkill, so a killed run never leaves the tool locked. The file itself is never deleted, and
    recover() doesn't touch it: a run that had opened it just before a delete would lock a file no later
    run can find, and both would go ahead.
    """
    descriptor = os.open(Path(tools_dir) / f".{tool}.lock", os.O_RDWR | os.O_CREAT)
    try:
        if WINDOWS:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)  # from where os.open leaves the position: byte 0
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(descriptor)
        # Windows refuses a locked byte with EACCES, and flock with EAGAIN. Anything else is a real failure.
        if e.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLOCK):
            return None
        raise
    return descriptor


def unlock(descriptor):
    """Let go of what lock() took. Never fails: the system lets go when the process ends in any case."""
    with contextlib.suppress(OSError):
        if WINDOWS:
            # Unlocked outright rather than only closed, because Windows may take a while to release the
            # lock on a closed file, and the next run can be right behind this one.
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    with contextlib.suppress(OSError):
        os.close(descriptor)


def recover(tools_dir, tool):
    """Undo what an interrupted run left behind. Returns what it did, as lines for the log.

    A kill between swap()'s two renames leaves no <tool> folder and the working copy at .<tool>-old, so
    it goes back. A kill after both leaves .<tool>-old beside the new copy, where it would stop the next
    swap. The .part download and the .<tool>-new folder are only ever this program's own, and the caller
    holds the tool's lock, so no other run is using them. The .<tool>.lock file is never among these.
    """
    root = Path(tools_dir)
    live, old = root / tool, root / f".{tool}-old"
    notes = []
    if old.is_dir():
        if not os.path.lexists(live):
            os.replace(old, live)
            notes.append(f"Put back the copy of {tool} that an interrupted update had moved aside.")
        else:
            _remove(old)
            notes.append(f"Removed {old.name}, left over from an earlier update.")
    for stale in (root / f".{tool}.part", root / f".{tool}-new"):
        if os.path.lexists(stale):
            _remove(stale)
            notes.append(f"Removed {stale.name}, left over from an interrupted update.")
    return notes


def swap(tools_dir, tool):
    """Put the checked .<tool>-new folder in place of <tool>, then delete the old copy.

    The working copy is moved aside only here, and moved back if the new one can't take its place.
    """
    root = Path(tools_dir)
    live, old, new = root / tool, root / f".{tool}-old", root / f".{tool}-new"
    moved = os.path.lexists(live)
    if moved:
        os.replace(live, old)
    try:
        os.replace(new, live)
    except OSError:
        if moved:
            os.replace(old, live)
        raise
    _remove(old)


# ---------------------------------------------------------------- the run

def _version(text):
    """'v2.9.6' -> '2.9.6'. "" for anything that isn't a plain dotted number."""
    text = text.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    return text if _VERSION.fullmatch(text) else ""


def _open(urlopen, url, headers=HEADERS):
    try:
        return urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        e.close()  # an HTTP error is also the server's answer, and holds its connection open like one
        raise


def _read_bounded(response, limit, now):
    """Yield response's body as it arrives, piece by piece, each with the clock reading taken after it.

    The download and every lookup are read here, so both follow the same two rules. A body that goes on past
    limit bytes raises TooLong once the piece that crossed it has been handed over, so no answer can fill
    the disk or the memory. One that brings fewer than STALL_BYTES in STALL_SECONDS raises TimeoutError, as
    a read that gets no byte in TIMEOUT already does, so a trickle ends the way silence does.
    """
    done = 0
    since, since_done = now(), 0  # where the stall rule's current minute began, and the bytes by then
    while done <= limit:
        # read1 hands over whatever has arrived. read would wait for a whole CHUNK, and on a slow link the
        # progress bar would then sit still for most of a minute at a time.
        chunk = response.read1(CHUNK)
        if not chunk:
            return
        done += len(chunk)
        tick = now()
        if tick - since >= STALL_SECONDS:
            if done - since_done < STALL_BYTES:
                raise TimeoutError(f"only {done - since_done:,} bytes arrived in {tick - since:.0f} seconds")
            since, since_done = tick, done
        yield chunk, tick
    raise TooLong(f"more than {limit:,} bytes arrived")


def _get(urlopen, url, now, headers=HEADERS):
    """The text at url, read under the download's stall rule and never past ANSWER_LIMIT.

    A stall raises TimeoutError and an answer past the limit TooLong, a ValueError, which every caller counts
    as no answer, the way it counts a connection that failed.
    """
    with _open(urlopen, url, headers) as response:
        data = b"".join(chunk for chunk, _ in _read_bounded(response, ANSWER_LIMIT, now))
        length = str(response.headers.get("Content-Length") or "")
        if length.isdigit() and len(data) < int(length):
            # read() raises this for an answer cut short, but read1 only stops. A download cut short fails its
            # size and SHA-256 check, but nothing checks an answer: "v2.9.6" cut to "v2.9" is still a version.
            raise http.client.IncompleteRead(data, int(length) - len(data))
        return data.decode("utf-8", "replace")


def _no_answer(error, host):
    """The sentence for a request to host that failed before host could answer it."""
    # To a computer whose clock is wrong, every certificate looks expired or not yet valid, and checking
    # the connection won't fix that. urllib wraps the certificate error in a URLError while connecting.
    if any(isinstance(cause, ssl.SSLCertVerificationError)
           for cause in (error, getattr(error, "reason", None))):
        return CERTIFICATE
    return NETWORK_ERROR.format(host=host)


def _mb(size):
    return math.ceil(size / MB)


def _size(path):
    """A file's size, or None when it isn't there as a file."""
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _check_space(root, needed, label, also=0):
    """Fail unless needed bytes, and MARGIN beside them, are free.

    also is space the run already fills and gives back before it ends: the download, while it is unpacked.
    That isn't free, so it isn't compared, but the sentence counts it. The failed run deletes the download
    before anyone reads the sentence, and a figure without it would be too small to clear enough room.
    """
    if shutil.disk_usage(root).free < needed + MARGIN:
        raise Failure(DISK, NO_SPACE.format(label=label, mb=_mb(also + needed + MARGIN)))


def _write_failure(error, needed, label, emit):
    """The Failure for an OSError while writing. needed is what the whole run needs, without MARGIN."""
    emit(f"{getattr(error, 'filename', None) or 'writing'}: {error}")
    if error.errno == errno.ENOSPC:
        return Failure(DISK, NO_SPACE.format(label=label, mb=_mb(needed + MARGIN)))
    return Failure(UNPACK, CANT_WRITE)


def _newest(recipe, urlopen, emit, now):
    url = recipe["latest"]
    host = urllib.parse.urlsplit(url).hostname  # the publisher's own site, which a filter may block alone
    try:
        text = _get(urlopen, url, now)
    except (OSError, http.client.HTTPException, ValueError) as e:  # ValueError: an answer far too long
        emit(f"{url}: {e}")
        raise Failure(NETWORK, _no_answer(e, host)) from e
    version = _version(text)
    if not version:  # such as a Wi-Fi sign-in page answering in its place
        emit(f"{url} answered {text.strip()[:40]!r}, which isn't a version number.")
        raise Failure(NETWORK, NETWORK_ERROR.format(host=host))
    emit(f"The newest {recipe['label']} is {version}, according to {url}")
    return version


def _published_checksum(recipe, version, url, urlopen, emit, now):
    """The SHA-256 the publisher lists beside the download, for when the GitHub API gave none."""
    label = recipe["label"]
    source = recipe["checksum"] or f"{url}.sha256sum"
    asking = source  # where a failure is logged: the version file is asked first, when there is one
    try:
        if recipe["checksum"]:
            asking = recipe["latest"]
            current = _version(_get(urlopen, asking, now))
            if current != version:
                emit(f"{source} describes {label} {current or 'of an unknown version'}, not {version}.")
                return None
            asking = source
        text = _get(urlopen, source, now)
    except urllib.error.HTTPError as e:
        emit(f"{asking}: {e}")
        if e.code == 404 and not recipe["checksum"]:  # it sits beside the zip, so the release is missing
            raise Failure(NETWORK, NOT_YET.format(label=label)) from e
        return None
    except (OSError, http.client.HTTPException, ValueError) as e:  # ValueError: an answer far too long
        emit(f"{asking}: {e}")
        return None
    sha256 = parse_digest(text)
    emit(f"SHA-256 from {source}: {sha256}" if sha256 else f"{source} doesn't hold a SHA-256.")
    return sha256


def _expected(recipe, version, url, urlopen, emit, now):
    """(size or None, sha256) that the download must match: GitHub's digest first, a checksum file second."""
    label = recipe["label"]
    tag, asset = recipe["tag"].format(v=version), recipe["asset"].format(v=version)
    api = f"https://api.github.com/repos/{recipe['repo']}/releases/tags/{tag}"
    size = sha256 = None
    # The sentence for when no checksum file can stand in for GitHub's digest either: why GitHub gave none.
    unanswered = NETWORK_ERROR.format(host="github.com")
    try:
        release = json.loads(_get(urlopen, api, now, API_HEADERS))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise Failure(NETWORK, NOT_YET.format(label=label)) from e
        emit(f"{api}: {e}")
        # GitHub's hourly allowance for this address is used up, which waiting fixes and checking the
        # connection doesn't. A 403 can mean other things, so it counts only when GitHub says none is left.
        remaining = str((e.headers or {}).get("X-RateLimit-Remaining", "")).strip()
        if e.code == 429 or (e.code == 403 and remaining == "0"):
            unanswered = RATE_LIMITED
    except (OSError, http.client.HTTPException, ValueError) as e:
        emit(f"{api}: {e}")
        unanswered = _no_answer(e, "github.com")
    else:
        found = pick_asset(release, asset)
        if found is None:
            raise Failure(NETWORK, NOT_YET.format(label=label))
        size, sha256 = found
        if sha256:
            emit(f"SHA-256 from GitHub: {sha256}")
    if not sha256:
        sha256 = _published_checksum(recipe, version, url, urlopen, emit, now)
        if not sha256:
            raise Failure(NETWORK, unanswered)
    return size, sha256


def _download(url, part, size, recipe, root, urlopen, emit, now):
    """Stream url into part, hashing as it goes. Returns (bytes received, their SHA-256)."""
    label = recipe["label"]
    try:
        response = _open(urlopen, url)
    except urllib.error.HTTPError as e:
        emit(f"{url}: {e}")
        sentence = NOT_YET.format(label=label) if e.code == 404 else NETWORK_ERROR.format(host="github.com")
        raise Failure(NETWORK, sentence) from e
    except (OSError, http.client.HTTPException) as e:
        emit(f"{url}: {e}")
        raise Failure(NETWORK, _no_answer(e, "github.com")) from e
    with response:
        length = str(response.headers.get("Content-Length") or "")
        total = size or (int(length) if length.isdigit() else 0)
        # With no size from GitHub or from the server, the recipe's ceiling stands in for one: for the space
        # it needs, and as the most that is read before the download counts as damaged.
        limit = total or recipe["ceiling"]
        if not size:  # no size from GitHub, so this is the first chance to check the space
            _check_space(root, limit + recipe["unpacked"], label)
        emit(f"{url} ({total:,} bytes)" if total else url)
        digest, done, shown, last = hashlib.sha256(), 0, None, None
        pieces = _read_bounded(response, limit, now)
        try:
            with open(part, "wb") as out:
                while True:
                    try:
                        chunk, tick = next(pieces, (b"", None))
                    except TimeoutError as e:  # no byte in TIMEOUT seconds, or too few in STALL_SECONDS
                        emit(f"{url}: {e}")
                        raise Failure(NETWORK, STALLED) from e
                    except TooLong:
                        # Longer than it can be: damaged, as its size or SHA-256 will show, and no reason to
                        # go on filling the disk.
                        emit(f"{url} sent more than {limit:,} bytes, so the rest wasn't read.")
                        break
                    except (OSError, http.client.HTTPException) as e:
                        emit(f"{url}: {e!r}")
                        raise Failure(NETWORK, NETWORK_ERROR.format(host="github.com")) from e
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if last is None or tick - last >= PROGRESS_EVERY:
                        emit(f"PROGRESS {done} {total}")
                        shown, last = done, tick
        except OSError as e:  # reading failures became Failure above, so this is the disk
            raise _write_failure(e, limit + recipe["unpacked"], label, emit) from e
    if shown != done:
        emit(f"PROGRESS {done} {total}")
    return done, digest.hexdigest()


def _unpack(part, new, recipe, root, label, emit, made):
    """Write the recipe's members, and nothing else, into the new folder under their fixed names.

    Returns {fixed name: bytes written}, which the folder is checked against again just before the swap.
    """
    unpacked, written = recipe["unpacked"], {}
    try:
        with zipfile.ZipFile(part) as archive:
            plan = plan_members(archive.namelist(), recipe)
            unpacked = sum(archive.getinfo(member).file_size for member in plan.values())
            _check_space(root, unpacked, label, also=part.stat().st_size)
            new.mkdir()
            made.append(new)
            for fixed, member in plan.items():
                # By its fixed name, never by the path inside the zip, so no member can land anywhere else.
                with archive.open(member) as source, open(new / fixed, "xb") as target:
                    shutil.copyfileobj(source, target, CHUNK)
                    written[fixed] = target.tell()
                emit(f"Unpacked {member} as {fixed}")
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as e:
        # Not a zip, a bad CRC, a damaged compressed stream (which zlib reports, not zipfile), or a
        # compression or encryption zipfile can't read.
        emit(f"{part.name}: {e!r}")
        raise Failure(UNPACK, NO_UNPACK) from e
    except OSError as e:
        raise _write_failure(e, (part.stat().st_size if part.exists() else 0) + unpacked, label, emit) from e
    return written


def _test(exe, program, version, label, run, emit):
    """Run the unpacked program from its new folder before it replaces anything.

    A download that matched its checksum can still be refused here: a computer that allows only approved
    programs may block anything under a user's own folders.
    """
    try:
        result = run([str(exe), TESTS[program][0]], stdin=subprocess.DEVNULL, capture_output=True,
                     timeout=TEST_TIMEOUT, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        emit(f"{exe.name} gave no answer within {TEST_TIMEOUT} seconds")
        raise Failure(UNPACK, FAILED_TEST.format(label=label)) from e
    except OSError as e:
        emit(f"{exe.name}: {e}")
        raise Failure(UNPACK, BLOCKED.format(label=label)) from e
    text = "".join(data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")
                   for data in (result.stdout, result.stderr))
    first = next((line.strip() for line in text.splitlines() if line.strip()), "nothing")
    emit(f"{exe.name} printed: {first}")  # prefixed, so no output can pass for a STEP or ERROR line
    if result.returncode != 0:
        emit(f"{exe.name} exited with code {result.returncode}")
        raise Failure(UNPACK, FAILED_TEST.format(label=label))
    problem = check_output(program, version, text)
    if problem:
        raise Failure(VERIFY, problem)


def _install(tool, version, tools_dir, urlopen, run, emit, now, sleep, made, held):
    recipe = RECIPES.get(tool)
    version = version if version == "latest" else _version(str(version))
    if recipe is None or not version:
        raise Failure(USAGE, BAD_ARGUMENTS)
    label = recipe["label"]
    if not supported():
        raise Failure(USAGE, UNSUPPORTED.format(label=label))

    root = Path(tools_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
        descriptor = lock(root, tool)
        if descriptor is None:
            # Refused before recover(), which would clear away the other run's files while it still uses them.
            raise Failure(UNPACK, ALREADY.format(label=label))
        held.callback(unlock, descriptor)
        for note in recover(root, tool):
            emit(note)
    except OSError as e:
        emit(f"{root}: {e}")
        raise Failure(UNPACK, CANT_WRITE) from e

    if version == "latest":
        version = _newest(recipe, urlopen, emit, now)
    emit(f"STEP Looking up {label} {version}…")
    tag, asset = recipe["tag"].format(v=version), recipe["asset"].format(v=version)
    url = f"https://github.com/{recipe['repo']}/releases/download/{tag}/{asset}"
    size, sha256 = _expected(recipe, version, url, urlopen, emit, now)
    if size:
        _check_space(root, size + recipe["unpacked"], label)

    emit("STEP Downloading…")
    part = root / f".{tool}.part"
    made.append(part)
    got_size, got_sha256 = _download(url, part, size, recipe, root, urlopen, emit, now)

    emit("STEP Checking the download…")
    if (size and got_size != size) or got_sha256 != sha256:
        emit(f"Expected {size or 'some'} bytes with SHA-256 {sha256}, "
             f"but got {got_size} bytes with SHA-256 {got_sha256}.")
        raise Failure(VERIFY, DAMAGED)

    emit("STEP Unpacking…")
    new = root / f".{tool}-new"
    written = _unpack(part, new, recipe, root, label, emit, made)
    _remove(part)

    emit("STEP Testing it…")
    for program in recipe["tests"]:
        _test(new / f"{program}.exe", program, version, label, run, emit)

    # Looked at again after the test runs, which can take seconds each while an antivirus scans a new
    # program: a file taken away or changed in that time must never reach the swap.
    changed = [name for name, size in written.items() if _size(new / name) != size]
    if changed:
        emit(f"{', '.join(changed)} changed in {new.name} after it was unpacked.")
        raise Failure(UNPACK, NO_UNPACK)

    emit(f"STEP {SWAP_STEP}")
    sleep(SWAP_PAUSE)  # so that a kill already on its way lands before anything is moved
    try:
        swap(root, tool)
    except OSError as e:
        emit(f"{root / tool}: {e}")
        raise Failure(UNPACK, IN_USE.format(label=label)) from e
    return version


def install(tool, version, tools_dir, *, urlopen=urllib.request.urlopen, run=subprocess.run, emit=print,
            now=time.monotonic, sleep=time.sleep):
    """Get one tool into tools_dir/<tool> and return the exit code. The last line emitted is DONE or ERROR.

    On a failure only what this run created is removed: recover() has already cleared anything older. The
    tool's lock is let go only after that, so another run can't start on the same staging names while this
    one is still clearing them away. now is the clock the stall rule reads, and sleep waits out SWAP_PAUSE.
    """
    made = []
    with contextlib.ExitStack() as held:
        try:
            version = _install(tool, version, tools_dir, urlopen, run, emit, now, sleep, made, held)
        except Failure as failure:
            code, sentence = failure.code, failure.sentence
        except Exception as e:  # a bug must still end in an ERROR line, or the window has nothing to show
            emit(f"{type(e).__name__}: {e}")
            label = RECIPES[tool]["label"] if tool in RECIPES else "the helper"
            code, sentence = UNPACK, UNEXPECTED.format(label=label)
        else:
            emit("STEP Done.")
            emit(f"DONE {version}")
            return OK
        for path in made:
            _remove(path)
        emit(f"ERROR {sentence}")
        return code


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    try:  # the window reads each line as UTF-8, and needs it the moment it is printed
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):  # no console at all, or a stand-in such as a test's StringIO
        pass
    if len(args) != 3:
        print("usage: python fetch.py ffmpeg|deno <version|latest> <tools folder>")
        print(f"ERROR {BAD_ARGUMENTS}")
        return USAGE
    return install(*args)


if __name__ == "__main__":
    sys.exit(main())
