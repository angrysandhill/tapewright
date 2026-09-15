# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""fetch.py, the downloader for Tapewright's own FFmpeg and deno, with no network and nothing installed.

Every download is a zip built here and served by a fake urlopen, or, for the one test that runs fetch.py
as a real child and cancels it, by a server on 127.0.0.1. Every test run is a fake subprocess.run that
answers the way the real programs do. The installing tests patch fetch.supported(), since the real one
says no everywhere but 64-bit Windows.

    python -m unittest tests.test_fetch -v
"""

import ast
import contextlib
import hashlib
import http.client
import http.server
import io
import itertools
import json
import math
import os
import random
import re
import signal
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import zipfile
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tapewright import deps, fetch, procs  # noqa: E402

# The addresses and sentences deps.py and the setup screen rely on, spelled out here rather than read
# from fetch.py, so that changing one there has to be deliberate.
FFMPEG_API = "https://api.github.com/repos/GyanD/codexffmpeg/releases/tags/9.0.1"
FFMPEG_ASSET = "ffmpeg-9.0.1-essentials_build.zip"
FFMPEG_ZIP = "https://github.com/GyanD/codexffmpeg/releases/download/9.0.1/" + FFMPEG_ASSET
GYAN_VERSION = "https://www.gyan.dev/ffmpeg/builds/release-version"
GYAN_SHA256 = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256"
DENO_LATEST = "https://dl.deno.land/release-latest.txt"
DENO_API = "https://api.github.com/repos/denoland/deno/releases/tags/v2.9.6"
DENO_ASSET = "deno-x86_64-pc-windows-msvc.zip"
DENO_ZIP = "https://github.com/denoland/deno/releases/download/v2.9.6/" + DENO_ASSET

NETWORK = "Couldn't reach github.com. Check that you're connected to the internet, then try again."
RATE_LIMITED = "GitHub has had too many requests from this connection. Try again in an hour."
CERTIFICATE = ("A secure connection couldn't be made. Check that this computer's date and time are right, "
               "then try again.")
STALLED = "The download from github.com stopped moving. Check your internet connection, then try again."
ALREADY = "Tapewright is already getting FFmpeg in another window. Wait for it to finish, then try again."
DAMAGED = "The download was damaged on the way, so it was thrown away. Try again."
STEPS_AFTER_LOOKUP = ["Downloading…", "Checking the download…", "Unpacking…", "Testing it…",
                      "Putting it in place…", "Done."]
PROTOCOL = re.compile(r"STEP .+|PROGRESS \d+ \d+|DONE \d+(?:\.\d+)*|ERROR .+")
KEYWORDS = ("STEP ", "PROGRESS ", "DONE ", "ERROR ")

MB = 1024 * 1024
MARGIN = 64 * MB
FFMPEG_UNPACKED, FFMPEG_CEILING = 200 * MB, 250 * MB  # fetch.py's estimate for the programs, and its ceiling
# Every run leaves its tool's lock file behind on purpose (see fetch.lock), so listings name it.
LOCK = ".ffmpeg.lock"

# The first lines are what the real programs printed on 2026-09-14; FFmpeg's configuration is shortened.
FFMPEG_SAYS = ("ffmpeg version 9.0.1-full_build-www.gyan.dev Copyright (c) 2000-2026 the FFmpeg developers\n"
               "configuration: --enable-gpl --enable-version3 --enable-static\n")
FFPROBE_SAYS = "ffprobe version 9.0.1-full_build-www.gyan.dev Copyright (c) 2000-2026 the FFmpeg developers\n"
DENO_SAYS = "deno 2.9.6 (stable, release, x86_64-pc-windows-msvc)\n"
SAYS = {"ffmpeg": FFMPEG_SAYS, "ffprobe": FFPROBE_SAYS, "deno": DENO_SAYS}

FOLDER = "ffmpeg-9.0.1-essentials_build/"
WORKING = ["ffmpeg/ffmpeg.exe", "ffmpeg/ffprobe.exe", "ffmpeg/notes.txt"]


def make_zip(members, method=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", method) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def ffmpeg_zip(drop=(), extra=None, method=zipfile.ZIP_DEFLATED):
    """An FFmpeg release zip laid out the way the recipe expects, with files it must leave alone."""
    members = {FOLDER + "bin/ffmpeg.exe": b"the new ffmpeg", FOLDER + "bin/ffprobe.exe": b"the new ffprobe",
               FOLDER + "bin/ffplay.exe": b"not wanted", FOLDER + "LICENSE": b"GPL",
               FOLDER + "README.txt": b"not wanted either"}
    members.update(extra or {})
    return make_zip({name: data for name, data in members.items() if name[len(FOLDER):] not in drop}, method)


def damaged_zip():
    """An FFmpeg zip whose ffmpeg.exe is compressed data that can't be read, which zlib reports, not zipfile.

    Its hash is whatever the damaged bytes hash to, so it passes the download's own check and fails only
    when it is unpacked.
    """
    data = bytearray(ffmpeg_zip())
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        start = archive.getinfo(FOLDER + "bin/ffmpeg.exe").header_offset
    name_length, extra_length = struct.unpack("<HH", data[start + 26:start + 30])
    data[start + 30 + name_length + extra_length] = 0x07  # a last block of type 3, which deflate reserves
    return bytes(data)


def deno_checksum_file(digest):
    """deno's .zip.sha256sum as it was when read on 2026-09-14: PowerShell's Get-FileHash output."""
    return (f"\r\nAlgorithm : SHA256\r\nHash      : {digest.upper()}\r\n"
            f"Path      : C:\\a\\deno\\deno\\target\\release\\{DENO_ASSET}\r\n\r\n")


def release(asset, data, size=None, digest=True):
    """A GitHub API release, beside a decoy asset, with download addresses that must never be used."""
    return json.dumps({"tag_name": "anything", "assets": [
        {"name": asset + ".sha256sum", "size": 177, "digest": "sha256:" + "0" * 64,
         "browser_download_url": "https://example.com/elsewhere.sha256sum"},
        {"name": asset, "size": len(data) if size is None else size,
         "digest": "sha256:" + hashlib.sha256(data).hexdigest() if digest is True else digest,
         "browser_download_url": "https://example.com/elsewhere.zip"},
    ]}).encode()


def no_space(megabytes):
    return (f"ERROR There isn't enough free space on this computer. The FFmpeg needs about {megabytes} MB. "
            "Make some room, then try again.")


def steps(lines):
    return [line[len("STEP "):] for line in lines if line.startswith("STEP ")]


class Response(io.BytesIO):
    def __init__(self, data, length=True):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))} if length else {}
        self.read_to = 0

    def close(self):
        if not self.closed:  # how far fetch.py read, kept for after its `with` has closed the response
            self.read_to = self.tell()
        super().close()


class Trickle(Response):
    """A download over a poor connection, a few bytes at a time. read() would wait for a whole chunk."""

    def __init__(self, data, step):
        super().__init__(data)
        self.step = step

    def read1(self, size=-1):
        return super().read1(min(size, self.step) if size > 0 else self.step)

    def read(self, size=-1):
        raise AssertionError("read() waits for a whole chunk, so progress would stand still between them")


class Stuck(Response):
    """A download whose connection stays open and sends nothing, until the socket's timeout ends the read."""

    def read1(self, size=-1):
        raise TimeoutError("The read operation timed out")


class Flood(Response):
    """An answer far longer than any lookup's, sent as fast as it is read, with no Content-Length to stop it.

    It does end, at 8 MB, so that a fetch.py reading all of it fails a test instead of hanging it.
    """

    def __init__(self):
        super().__init__(b"", length=False)
        self.left, self.sent = 8 * MB, 0

    def read1(self, size=-1):
        chunk = b" " * min(size if size > 0 else MB, self.left)
        self.left, self.sent = self.left - len(chunk), self.sent + len(chunk)
        return chunk

    def read(self, size=-1):
        raise AssertionError("read() keeps everything until the answer ends")


# The failure of a computer whose clock is wrong, as urllib raises it while connecting.
WRONG_CLOCK = urllib.error.URLError(ssl.SSLCertVerificationError(
    1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate is not yet valid"))
# GitHub's API when its hourly allowance for an address is used up. Header names are case-insensitive.
USED_UP = (403, {"x-ratelimit-remaining": "0"})


class Web:
    """A fake urlopen. Each address answers with bytes; a function making a response; an HTTP status to fail
    with, alone or as (status, headers); or an exception."""

    def __init__(self, pages, default=404):
        self.pages, self.default, self.requests = dict(pages), default, []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        page = self.pages.get(request.full_url, self.default)
        if isinstance(page, int):
            page = (page, {})
        if isinstance(page, tuple):
            status, fields = page
            headers = http.client.HTTPMessage()
            for name, value in fields.items():
                headers[name] = value
            # With a body, as urllib always gives one: before Python 3.10.10, close() on none raised KeyError.
            raise urllib.error.HTTPError(request.full_url, status, "fake", headers, io.BytesIO())
        if isinstance(page, Exception):
            raise page
        return page() if callable(page) else Response(page)

    @property
    def asked(self):
        return [request.full_url for request in self.requests]


def outcomes(folder):
    """Every way a run of fetch.py ends, as {what happened: (exit code, the lines it printed)}.

    Each is a real run through fetch's own emit, with the fake web and test runs above, so whatever reads
    these lines reads what fetch.py really prints rather than a copy typed out again. tests/test_core.py
    replays them through the window. Each run gets a tools folder of its own under folder.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    data, no_ffprobe, junk = ffmpeg_zip(), ffmpeg_zip(drop=["bin/ffprobe.exe"]), b"not a zip at all"
    deno = make_zip({"deno.exe": b"the new deno"})
    offline = urllib.error.URLError("getaddrinfo failed")
    found = {}

    def run(name, tool="ffmpeg", version="9.0.1", web=None, says=None, error=None, exit_code=0, patches=(),
            tools=None, now=None):
        says = dict(SAYS, **(says or {}))

        def test_run(args, **kw):
            if error:
                raise error
            return subprocess.CompletedProcess(args, exit_code, says[Path(args[0]).stem].encode("utf-8"), b"")

        web = web or Web({FFMPEG_API: release(FFMPEG_ASSET, data), FFMPEG_ZIP: data})
        lines = []
        with contextlib.ExitStack() as stack:
            for patch in (mock.patch.object(fetch, "supported", return_value=True), *patches):
                stack.enter_context(patch)
            # No real pause before the swap, which would only make every run that reaches it slower.
            code = fetch.install(tool, version, str(tools or folder / name), urlopen=web, run=test_run,
                                 emit=lines.append, now=now or itertools.count().__next__,
                                 sleep=lambda seconds: None)
        found[name] = (code, lines)

    run("installed")
    run("installed the newest deno", "deno", "latest",
        Web({DENO_LATEST: b"v2.9.6\n", DENO_API: release(DENO_ASSET, deno), DENO_ZIP: deno}))
    run("offline", web=Web({}, default=offline))
    run("gyan.dev unreachable", version="latest", web=Web({}, default=offline))
    run("a certificate that failed its check", web=Web({}, default=WRONG_CLOCK))
    run("rate limited", web=Web({FFMPEG_API: USED_UP, GYAN_VERSION: b"9.1\n"}))
    run("stalled", web=Web({FFMPEG_API: release(FFMPEG_ASSET, data), FFMPEG_ZIP: lambda: Trickle(data, 10)}),
        now=itertools.count(0, 10).__next__)
    run("not on github.com yet", web=Web({}))
    run("damaged", web=Web({FFMPEG_API: release(FFMPEG_ASSET, data, digest="sha256:" + "0" * 64),
                            FFMPEG_ZIP: data}))
    run("no room", patches=[mock.patch.object(fetch.shutil, "disk_usage", return_value=mock.Mock(free=0))])
    run("no ffprobe in it", web=Web({FFMPEG_API: release(FFMPEG_ASSET, no_ffprobe), FFMPEG_ZIP: no_ffprobe}))
    run("not a zip", web=Web({FFMPEG_API: release(FFMPEG_ASSET, junk), FFMPEG_ZIP: junk}))
    run("blocked by Windows", error=PermissionError("blocked"))
    run("no answer from its test run", error=subprocess.TimeoutExpired("ffmpeg.exe", 60))
    run("failed its test run", exit_code=1)
    run("another version", says={"ffprobe": FFPROBE_SAYS.replace("9.0.1", "8.1.1")})
    run("nonfree", says={"ffmpeg": FFMPEG_SAYS + "--enable-nonfree\n"})
    run("in use", patches=[mock.patch.object(fetch.os, "replace", side_effect=PermissionError("in use"))])
    busy = folder / "being got in another window"
    busy.mkdir()
    other = fetch.lock(busy, "ffmpeg")  # held the way another window's run holds it
    try:
        run("already being got in another window", tools=busy)
    finally:
        fetch.unlock(other)
    in_the_way = folder / "a file where the tools folder goes"
    in_the_way.write_bytes(b"")
    run("can't write", tools=in_the_way)
    run("a bug", web=Web({}, default=TypeError("a bug in fetch.py")))
    run("not 64-bit Windows", patches=[mock.patch.object(fetch, "supported", return_value=False)])
    run("an unknown tool", tool="vlc")
    out = io.StringIO()
    with mock.patch.object(sys, "stdout", out):
        found["too few arguments"] = (fetch.main(["ffmpeg", "9.0.1"]), out.getvalue().splitlines())
    return found


class Pieces(unittest.TestCase):
    def test_digests_in_every_form(self):
        digest = hashlib.sha256(b"x").hexdigest()
        deno_file = deno_checksum_file(digest)
        self.assertEqual(len(deno_file.encode()), 177)  # the size of the real file, with its CRLF endings
        # GitHub's field, gyan.dev's bare hex, deno's real file, and other checksum-file shapes, which show
        # that no fixed position is assumed.
        for text in (f"sha256:{digest}", digest.upper(), f"  {digest}\n", deno_file,
                     f"{digest}  {DENO_ASSET}\n",
                     f"SHA256 hash of {DENO_ASSET}:\r\n{digest.upper()}\r\nCertUtil: done.\r\n"):
            self.assertEqual(fetch.parse_digest(text), digest, text)
        for text in ("sha512:" + "ab" * 64, "sha1:" + digest, "sha256:" + digest[:-1], digest + "0",
                     "", None):
            self.assertIsNone(fetch.parse_digest(text), text)

    def test_the_asset_is_picked_by_its_exact_name(self):
        answer = json.loads(release(DENO_ASSET, b"zip"))
        self.assertEqual(fetch.pick_asset(answer, DENO_ASSET), (3, hashlib.sha256(b"zip").hexdigest()))
        self.assertIsNone(fetch.pick_asset(answer, "deno-x86_64-pc-windows-msvc"))
        answer["assets"][1].update(size=True, digest="sha512:" + "0" * 128)
        self.assertEqual(fetch.pick_asset(answer, DENO_ASSET), (None, None))
        for garbage in ({}, {"assets": None}, {"assets": ["x"]}, [], "text", None):
            self.assertIsNone(fetch.pick_asset(garbage, DENO_ASSET))

    def test_members_are_matched_folder_by_folder_and_given_the_recipes_names(self):
        ffmpeg, deno = fetch.RECIPES["ffmpeg"], fetch.RECIPES["deno"]
        names = [FOLDER, FOLDER + "bin/", FOLDER + "bin/ffmpeg.exe", FOLDER + "bin/ffprobe.exe",
                 FOLDER + "doc/bin/ffmpeg.exe", "../bin/../ffprobe.exe", "a/LICENSE", "b/LICENSE"]
        # An optional member found twice is left out rather than guessed at.
        self.assertEqual(fetch.plan_members(names, ffmpeg),
                         {"ffmpeg.exe": FOLDER + "bin/ffmpeg.exe", "ffprobe.exe": FOLDER + "bin/ffprobe.exe"})
        self.assertEqual(fetch.plan_members(["../deno.exe", "DENO.EXE"], deno), {"deno.exe": "DENO.EXE"})
        with self.assertRaises(fetch.Failure) as caught:
            fetch.plan_members(["../deno.exe", "bin/deno.exe"], deno)
        self.assertEqual((caught.exception.code, caught.exception.sentence),
                         (5, "The download didn't contain deno.exe, so nothing was changed."))
        with self.assertRaises(fetch.Failure) as caught:  # which of the two would be a guess
            fetch.plan_members(["a/bin/ffmpeg.exe", "b/bin/ffmpeg.exe", "a/bin/ffprobe.exe"], ffmpeg)
        self.assertEqual(caught.exception.code, 5)

    def test_a_test_run_must_print_this_exact_build(self):
        self.assertEqual(fetch.check_output("ffmpeg", "9.0.1", FFMPEG_SAYS), "")
        self.assertEqual(fetch.check_output("ffprobe", "9.0.1", "\r\n" + FFPROBE_SAYS), "")
        self.assertEqual(fetch.check_output("deno", "2.9.6", DENO_SAYS), "")
        wrong = "The downloaded FFmpeg didn't work when tested, so it wasn't used."
        self.assertEqual(fetch.check_output("ffmpeg", "9.0", FFMPEG_SAYS), wrong)  # 9.0.1 starts with 9.0
        self.assertEqual(fetch.check_output("ffmpeg", "9.0.1", FFMPEG_SAYS.replace("9.0.1", "9.0.10")), wrong)
        self.assertEqual(fetch.check_output("ffprobe", "9.0.1", FFMPEG_SAYS), wrong)
        self.assertEqual(fetch.check_output("ffmpeg", "9.0.1", ""), wrong)
        self.assertEqual(fetch.check_output("deno", "2.9", DENO_SAYS),
                         "The downloaded deno didn't work when tested, so it wasn't used.")
        nonfree = "This FFmpeg build can't be used, so it wasn't kept."
        self.assertEqual(fetch.check_output("ffmpeg", "9.0.1", FFMPEG_SAYS + "--enable-nonfree\n"), nonfree)
        no_gpl = FFMPEG_SAYS.replace("--enable-gpl ", "")
        self.assertEqual(fetch.check_output("ffmpeg", "9.0.1", no_gpl), nonfree)

    def test_only_64_bit_windows_is_supported(self):
        for windows, machine, expected in ((True, "AMD64", True), (True, "ARM64", True),
                                           (True, "x86", False), (False, "x86_64", False)):
            with mock.patch.object(fetch, "WINDOWS", windows), \
                    mock.patch.object(fetch.platform, "machine", return_value=machine):
                self.assertIs(fetch.supported(), expected, (windows, machine))

    def test_the_swap_step_is_the_one_the_window_waits_for(self):
        # deps reads these words to stop offering Cancel; this fails until both say the same.
        self.assertEqual(fetch.SWAP_STEP, deps.SWAP_STEP)


class Folders(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tools = Path(folder.name)

    def make(self, name, content):
        path = self.tools / name / "ffmpeg.exe"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def exe(self):
        return (self.tools / "ffmpeg" / "ffmpeg.exe").read_bytes()

    def test_a_kill_between_the_two_renames_is_undone(self):
        self.make(".ffmpeg-old", b"working")
        self.make(".ffmpeg-new", b"unchecked")
        (self.tools / ".ffmpeg.part").write_bytes(b"half a download")
        (self.tools / ".deno.part").write_bytes(b"another tool's")
        (self.tools / LOCK).write_bytes(b"")  # never cleared away, not even as a leftover
        self.assertEqual(len(fetch.recover(self.tools, "ffmpeg")), 3)
        self.assertEqual(sorted(os.listdir(self.tools)), [".deno.part", LOCK, "ffmpeg"])
        self.assertEqual(self.exe(), b"working")
        self.assertEqual(fetch.recover(self.tools, "ffmpeg"), [])

    def test_a_kill_after_both_renames_keeps_the_new_copy(self):
        self.make("ffmpeg", b"new")
        self.make(".ffmpeg-old", b"previous")
        fetch.recover(self.tools, "ffmpeg")
        self.assertEqual((os.listdir(self.tools), self.exe()), (["ffmpeg"], b"new"))

    def test_swap_replaces_the_copy_and_puts_it_back_when_it_cannot(self):
        self.make("ffmpeg", b"old")
        self.make(".ffmpeg-new", b"new")
        fetch.swap(self.tools, "ffmpeg")
        self.assertEqual((os.listdir(self.tools), self.exe()), (["ffmpeg"], b"new"))

        self.make(".ffmpeg-new", b"newer")
        replace = os.replace

        def refuse_the_new_folder(source, target):
            if Path(source).name == ".ffmpeg-new":
                raise PermissionError("in use")
            return replace(source, target)

        with mock.patch.object(fetch.os, "replace", side_effect=refuse_the_new_folder):
            with self.assertRaises(PermissionError):
                fetch.swap(self.tools, "ffmpeg")
        self.assertEqual((sorted(os.listdir(self.tools)), self.exe()), ([".ffmpeg-new", "ffmpeg"], b"new"))

        (self.tools / "ffmpeg").rename(self.tools / "gone")  # the first install has nothing to move aside
        fetch.swap(self.tools, "ffmpeg")
        self.assertEqual(self.exe(), b"newer")

    def test_one_run_at_a_time_holds_each_tool(self):
        first = fetch.lock(self.tools, "ffmpeg")
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(fetch.lock(self.tools, "ffmpeg"))
            deno = fetch.lock(self.tools, "deno")  # another tool has a lock of its own
            self.assertIsNotNone(deno)
            fetch.unlock(deno)
        finally:
            fetch.unlock(first)
        again = fetch.lock(self.tools, "ffmpeg")
        self.assertIsNotNone(again)
        fetch.unlock(again)
        self.assertEqual(sorted(os.listdir(self.tools)), [".deno.lock", LOCK])


class Install(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.top = Path(folder.name)
        # Deep enough that a zip member climbing out of the tools folder would still land in self.top.
        self.tools = self.top / "deep" / "er" / "tools"
        patch = mock.patch.object(fetch, "supported", return_value=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.runs, self.lines, self.happened = [], [], []

    def pause(self, seconds):
        """In place of time.sleep, noting each pause and the last line printed before it, without waiting."""
        self.happened.append(("pause", seconds, self.lines[-1]))

    def install(self, tool, version, web, says=None, error=None, now=None, during=None):
        """fetch.install with fake test runs and pauses. during, if given, is called with each program before
        it runs."""
        says = dict(SAYS, **(says or {}))

        def run(args, **kw):
            exe = Path(args[0])
            self.runs.append((exe.parent.name, exe.name, exe.is_file(), args[1:], kw.get("timeout"),
                              "creationflags" in kw))
            if during:
                during(exe)
            if error:
                raise error
            return subprocess.CompletedProcess(args, 0, says[exe.stem].encode("utf-8"), b"")

        self.lines = lines = []
        code = fetch.install(tool, version, str(self.tools), urlopen=web, run=run, emit=lines.append,
                             now=now or itertools.count().__next__, sleep=self.pause)
        for line in lines:
            if line.startswith(KEYWORDS):
                self.assertRegex(line, f"^(?:{PROTOCOL.pattern})$")
        self.assertEqual(sum(line.startswith(("DONE ", "ERROR ")) for line in lines), 1, lines)
        self.assertTrue(lines[-1].startswith("DONE " if code == 0 else "ERROR "), lines)
        return code, lines

    def ffmpeg_web(self, data=None):
        data = data or ffmpeg_zip()
        return Web({FFMPEG_API: release(FFMPEG_ASSET, data), FFMPEG_ZIP: data})

    def working_copy(self):
        for name in WORKING:
            path = self.tools / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"working " + name.encode())

    def files(self):
        """Every file under the tools folder but the lock files, which each test checks for itself."""
        return sorted(p.relative_to(self.tools).as_posix() for p in self.tools.rglob("*")
                      if p.is_file() and p.name not in (LOCK, ".deno.lock"))

    def assert_working_copy_untouched(self):
        self.assertEqual(self.files(), WORKING)
        self.assertEqual((self.tools / WORKING[0]).read_bytes(), b"working " + WORKING[0].encode())

    def assert_only_the_working_copy_left(self):
        self.assert_working_copy_untouched()
        self.assertEqual(sorted(os.listdir(self.tools)), [LOCK, "ffmpeg"])  # no .part and no -new

    def test_ffmpeg_end_to_end(self):
        self.working_copy()
        data = ffmpeg_zip()
        web = self.ffmpeg_web(data)
        code, lines = self.install("ffmpeg", "9.0.1", web)
        self.assertEqual((code, lines[-2:]), (0, ["STEP Done.", "DONE 9.0.1"]))
        self.assertEqual(steps(lines), ["Looking up FFmpeg 9.0.1…"] + STEPS_AFTER_LOOKUP)
        self.assertEqual([line for line in lines if line.startswith("PROGRESS ")],
                         [f"PROGRESS {len(data)} {len(data)}"])
        self.assertEqual(self.files(), ["ffmpeg/LICENSE", "ffmpeg/ffmpeg.exe", "ffmpeg/ffprobe.exe"])
        self.assertEqual((self.tools / "ffmpeg" / "ffmpeg.exe").read_bytes(), b"the new ffmpeg")
        self.assertEqual(sorted(os.listdir(self.tools)), [LOCK, "ffmpeg"])
        # The download address comes from the recipe; the ones in GitHub's answer are never used.
        self.assertEqual(web.asked, [FFMPEG_API, FFMPEG_ZIP])
        self.assertEqual({request.get_header("User-agent") for request in web.requests}, {"Tapewright-fetch"})
        self.assertEqual(web.requests[0].get_header("Accept"), "application/vnd.github+json")
        # Both ran from the unpacked folder, before it replaced the working copy.
        self.assertEqual(self.runs, [(".ffmpeg-new", "ffmpeg.exe", True, ["-version"], 60, True),
                                     (".ffmpeg-new", "ffprobe.exe", True, ["-version"], 60, True)])

    def test_the_newest_deno_end_to_end(self):
        data = make_zip({"deno.exe": b"the new deno"})
        web = Web({DENO_LATEST: b"v2.9.6\n", DENO_API: release(DENO_ASSET, data), DENO_ZIP: data})
        code, lines = self.install("deno", "latest", web)
        self.assertEqual((code, lines[-1]), (0, "DONE 2.9.6"))
        self.assertEqual(steps(lines), ["Looking up deno 2.9.6…"] + STEPS_AFTER_LOOKUP)
        self.assertEqual(self.files(), ["deno/deno.exe"])
        self.assertEqual(web.asked, [DENO_LATEST, DENO_API, DENO_ZIP])
        self.assertEqual([run[1:4] for run in self.runs], [("deno.exe", True, ["--version"])])

    def test_the_swap_is_announced_and_starts_only_after_a_pause(self):
        # From that line the window stops offering Cancel, so nothing that can still fail may come after it,
        # and the swap waits SWAP_PAUSE for a kill already on its way (see fetch.SWAP_PAUSE).
        self.working_copy()
        real = fetch.swap

        def swap(*args):
            self.happened.append(("swap", self.lines[-1]))
            return real(*args)

        with mock.patch.object(fetch, "swap", side_effect=swap):
            code, lines = self.install("ffmpeg", "9.0.1", self.ffmpeg_web())
        self.assertEqual((code, self.happened), (0, [("pause", fetch.SWAP_PAUSE, "STEP Putting it in place…"),
                                                     ("swap", "STEP Putting it in place…")]))
        self.assertEqual(lines[-3:], ["STEP Putting it in place…", "STEP Done.", "DONE 9.0.1"])
        self.assertGreaterEqual(fetch.SWAP_PAUSE, 1)
        self.assertIs(fetch.install.__kwdefaults__["sleep"], time.sleep)  # real when the window runs it
        # A run that fails a check never says it, and never pauses.
        self.happened.clear()
        code, lines = self.install("ffmpeg", "9.0.1", self.ffmpeg_web(), says={"ffmpeg": "nothing like it\n"})
        self.assertEqual((code, "STEP Putting it in place…" in lines, self.happened), (3, False, []))

    def test_a_damaged_download_is_thrown_away_and_the_working_copy_kept(self):
        data = ffmpeg_zip()
        for what, answer in (("hash", release(FFMPEG_ASSET, data, digest="sha256:" + "0" * 64)),
                             ("size", release(FFMPEG_ASSET, data, size=len(data) + 1))):
            with self.subTest(what):
                self.working_copy()
                code, lines = self.install("ffmpeg", "9.0.1", Web({FFMPEG_API: answer, FFMPEG_ZIP: data}))
                self.assertEqual((code, lines[-1]), (3, "ERROR " + DAMAGED))
                self.assertEqual(steps(lines)[-1], "Checking the download…")
                self.assert_only_the_working_copy_left()
                self.assertEqual(self.runs, [])

    def test_a_member_that_climbs_out_lands_only_by_its_fixed_name(self):
        data = make_zip({"../bin/ffmpeg.exe": b"ffmpeg by an odd path", "x/bin/ffprobe.exe": b"ffprobe",
                         "../../../evil.exe": b"evil", "x/bin/../../../../evil.exe": b"evil"})
        self.assertIn("../../../evil.exe", zipfile.ZipFile(io.BytesIO(data)).namelist())
        code, _ = self.install("ffmpeg", "9.0.1", self.ffmpeg_web(data))
        self.assertEqual(code, 0)
        everything = sorted(p.relative_to(self.top).as_posix() for p in self.top.rglob("*") if p.is_file())
        self.assertEqual(everything, ["deep/er/tools/" + LOCK, "deep/er/tools/ffmpeg/ffmpeg.exe",
                                      "deep/er/tools/ffmpeg/ffprobe.exe"])
        self.assertEqual((self.tools / "ffmpeg" / "ffmpeg.exe").read_bytes(), b"ffmpeg by an odd path")

    def test_a_download_without_ffprobe_changes_nothing(self):
        self.working_copy()
        code, lines = self.install("ffmpeg", "9.0.1", self.ffmpeg_web(ffmpeg_zip(drop=["bin/ffprobe.exe"])))
        self.assertEqual((code, lines[-1]),
                         (5, "ERROR The download didn't contain ffprobe.exe, so nothing was changed."))
        self.assert_only_the_working_copy_left()
        self.assertEqual(self.runs, [])

    def test_a_download_that_cannot_be_unpacked_changes_nothing(self):
        broken = damaged_zip()
        with zipfile.ZipFile(io.BytesIO(broken)) as archive, self.assertRaises(zlib.error):
            archive.read(FOLDER + "bin/ffmpeg.exe")  # so this really is the damage zipfile doesn't catch
        for what, data in (("not a zip", b"not a zip at all"), ("damaged compressed data", broken)):
            with self.subTest(what):
                self.working_copy()
                code, lines = self.install("ffmpeg", "9.0.1", self.ffmpeg_web(data))
                self.assertEqual((code, lines[-1]), (5, "ERROR " + fetch.NO_UNPACK))
                self.assert_only_the_working_copy_left()
                self.assertEqual(self.runs, [])

    def test_a_file_taken_away_after_its_test_run_never_reaches_the_swap(self):
        # Such as an antivirus removing a program it has just scanned, or anything else but this run.
        for name, change in (("ffmpeg.exe", Path.unlink), ("LICENSE", lambda path: path.write_bytes(b"G"))):
            with self.subTest(name):
                self.working_copy()

                def during(exe, name=name, change=change):
                    if exe.stem == "ffprobe":  # ffmpeg.exe has had its test run by now
                        change(exe.parent / name)

                code, lines = self.install("ffmpeg", "9.0.1", self.ffmpeg_web(), during=during)
                self.assertEqual((code, lines[-1]), (5, "ERROR " + fetch.NO_UNPACK))
                self.assertIn(f"{name} changed in .ffmpeg-new after it was unpacked.", lines)
                self.assertEqual(steps(lines)[-1], "Testing it…")
                self.assert_only_the_working_copy_left()

    def test_a_nonfree_ffmpeg_is_refused(self):
        self.working_copy()
        says = {"ffmpeg": FFMPEG_SAYS.replace("--enable-gpl", "--enable-gpl --enable-nonfree")}
        code, lines = self.install("ffmpeg", "9.0.1", self.ffmpeg_web(), says=says)
        self.assertEqual((code, lines[-1]), (3, "ERROR This FFmpeg build can't be used, so it wasn't kept."))
        self.assert_only_the_working_copy_left()

    def test_a_program_of_another_version_is_refused(self):
        self.working_copy()
        says = {"ffprobe": FFPROBE_SAYS.replace("9.0.1", "8.1.1")}
        code, lines = self.install("ffmpeg", "9.0.1", self.ffmpeg_web(), says=says)
        self.assertEqual((code, lines[-1]),
                         (3, "ERROR The downloaded FFmpeg didn't work when tested, so it wasn't used."))
        self.assert_only_the_working_copy_left()

    def test_a_program_windows_will_not_run_is_explained(self):
        code, lines = self.install("ffmpeg", "9.0.1", self.ffmpeg_web(), error=PermissionError("blocked"))
        self.assertEqual((code, lines[-1]), (
            5, "ERROR Windows stopped FFmpeg from running. This computer may only allow approved programs."))
        self.assertEqual(os.listdir(self.tools), [LOCK])

    def test_a_second_run_of_the_same_tool_touches_nothing(self):
        # Two Tapewright windows can each start getting FFmpeg. The second must not clear away the first's
        # download, which recover() would otherwise do before anything else.
        self.working_copy()
        part = self.tools / ".ffmpeg.part"
        part.write_bytes(b"the other window's download, still arriving")
        other = fetch.lock(self.tools, "ffmpeg")  # held the way the other window's run holds it
        try:
            web = self.ffmpeg_web()
            self.assertEqual(self.install("ffmpeg", "9.0.1", web), (5, ["ERROR " + ALREADY]))
            self.assertEqual((web.asked, self.runs), ([], []))
            self.assertEqual(part.read_bytes(), b"the other window's download, still arriving")
            self.assertEqual(self.files(), [".ffmpeg.part", *WORKING])
            # deno has a lock of its own, so it can be got meanwhile.
            deno = make_zip({"deno.exe": b"the new deno"})
            web = Web({DENO_API: release(DENO_ASSET, deno), DENO_ZIP: deno})
            self.assertEqual(self.install("deno", "2.9.6", web)[1][-1], "DONE 2.9.6")
        finally:
            fetch.unlock(other)
        # Once the other run has let go, the next one goes ahead, clearing its leftovers away first.
        code, lines = self.install("ffmpeg", "9.0.1", self.ffmpeg_web())
        self.assertEqual((code, lines[-1]), (0, "DONE 9.0.1"))
        self.assertIn("Removed .ffmpeg.part, left over from an interrupted update.", lines)

    def test_a_release_github_does_not_have_yet(self):
        not_yet = "ERROR The newest FFmpeg isn't on github.com yet. Try again later."
        web = Web({})
        code, lines = self.install("ffmpeg", "9.0.1", web)
        self.assertEqual((code, lines), (2, ["STEP Looking up FFmpeg 9.0.1…", not_yet]))
        self.assertEqual(web.asked, [FFMPEG_API])
        # A release whose assets don't include this build says the same.
        data = ffmpeg_zip()
        web = Web({FFMPEG_API: release("ffmpeg-9.0.1-full_build.zip", data), FFMPEG_ZIP: data})
        self.assertEqual(self.install("ffmpeg", "9.0.1", web)[1][-1], not_yet)
        self.assertEqual(web.asked, [FFMPEG_API])

    def test_a_rate_limited_api_falls_back_to_the_checksum_beside_the_download(self):
        data = ffmpeg_zip()
        digest = hashlib.sha256(data).hexdigest().encode()
        for status in (USED_UP, 429, 403):
            with self.subTest(status):
                web = Web({FFMPEG_API: status, GYAN_VERSION: b"9.0.1\r\n", GYAN_SHA256: digest,
                           FFMPEG_ZIP: data})
                self.assertEqual(self.install("ffmpeg", "9.0.1", web)[1][-1], "DONE 9.0.1")
                self.assertEqual(web.asked, [FFMPEG_API, GYAN_VERSION, GYAN_SHA256, FFMPEG_ZIP])
        # gyan.dev's checksum is for its newest build only, so once that has moved on it proves nothing. The
        # sentence then says why GitHub gave no digest: a rate limit only when GitHub says so, since a 403
        # can mean other things.
        for status, sentence in ((USED_UP, RATE_LIMITED), (429, RATE_LIMITED), (403, NETWORK)):
            with self.subTest(status):
                web = Web({FFMPEG_API: status, GYAN_VERSION: b"9.1\n", GYAN_SHA256: digest, FFMPEG_ZIP: data})
                code, lines = self.install("ffmpeg", "9.0.1", web)
                self.assertEqual((code, steps(lines), lines[-1]),
                                 (2, ["Looking up FFmpeg 9.0.1…"], "ERROR " + sentence))
                self.assertEqual(web.asked, [FFMPEG_API, GYAN_VERSION])

        # deno's release carries a checksum file of its own, read when GitHub's answer has no digest.
        deno = make_zip({"deno.exe": b"the new deno"})
        text = deno_checksum_file(hashlib.sha256(deno).hexdigest()).encode()
        web = Web({DENO_API: release(DENO_ASSET, deno, digest=None), DENO_ZIP + ".sha256sum": text,
                   DENO_ZIP: deno})
        self.assertEqual(self.install("deno", "2.9.6", web)[1][-1], "DONE 2.9.6")
        self.assertEqual(web.asked, [DENO_API, DENO_ZIP + ".sha256sum", DENO_ZIP])

    def test_offline(self):
        # Asked for the newest, the first address is the publisher's own site, and the sentence names it.
        offline = urllib.error.URLError("getaddrinfo failed")
        gyan = "Couldn't reach www.gyan.dev. Check that you're connected to the internet, then try again."
        deno = "Couldn't reach dl.deno.land. Check that you're connected to the internet, then try again."
        for tool, version, web, sentence in (
                ("ffmpeg", "9.0.1", Web({}, default=offline), NETWORK),
                ("ffmpeg", "latest", Web({}, default=offline), gyan),
                ("deno", "latest", Web({}, default=offline), deno),
                # such as a Wi-Fi sign-in page answering in the site's place
                ("ffmpeg", "latest", Web({GYAN_VERSION: b"<html>Sign in to continue</html>"}), gyan)):
            with self.subTest((tool, version, web.default)):
                code, lines = self.install(tool, version, web)
                self.assertEqual((code, lines[-1]), (2, "ERROR " + sentence))
                self.assertEqual([name for name in os.listdir(self.tools) if not name.endswith(".lock")], [])

    def test_a_certificate_that_fails_its_check_points_at_the_clock(self):
        data = ffmpeg_zip()
        download = {FFMPEG_API: release(FFMPEG_ASSET, data), FFMPEG_ZIP: WRONG_CLOCK}
        for what, version, web in (("every address", "9.0.1", Web({}, default=WRONG_CLOCK)),
                                   ("the publisher's site", "latest", Web({}, default=WRONG_CLOCK)),
                                   ("the download", "9.0.1", Web(download))):
            with self.subTest(what):
                code, lines = self.install("ffmpeg", version, web)
                self.assertEqual((code, lines[-1]), (2, "ERROR " + CERTIFICATE))

    def test_a_download_that_stops_moving_is_given_up(self):
        self.working_copy()
        data = ffmpeg_zip()
        # Ten bytes every ten seconds: 60 bytes in the first minute, far short of 64 KiB. Progress follows
        # each trickle, because read1 hands over what has arrived rather than waiting for a whole chunk.
        web = Web({FFMPEG_API: release(FFMPEG_ASSET, data), FFMPEG_ZIP: lambda: Trickle(data, 10)})
        code, lines = self.install("ffmpeg", "9.0.1", web, now=itertools.count(0, 10).__next__)
        self.assertEqual((code, lines[-1]), (2, "ERROR " + STALLED))
        self.assertEqual([line for line in lines if line.startswith("PROGRESS ")],
                         [f"PROGRESS {10 * n} {len(data)}" for n in range(1, 6)])
        self.assertEqual(steps(lines)[-1], "Downloading…")
        self.assert_only_the_working_copy_left()

        # A read that waits the socket's whole timeout without a byte has stopped moving too.
        web = Web({FFMPEG_API: release(FFMPEG_ASSET, data), FFMPEG_ZIP: lambda: Stuck(data)})
        self.assertEqual(self.install("ffmpeg", "9.0.1", web), (2, self.lines))
        self.assertEqual(self.lines[-1], "ERROR " + STALLED)
        self.assert_only_the_working_copy_left()

        # Slow but moving is left alone: 16 KiB every ten seconds is 96 KiB a minute.
        slow = ffmpeg_zip(extra={FOLDER + "doc/manual.bin": bytes(300 * 1024)}, method=zipfile.ZIP_STORED)
        web = Web({FFMPEG_API: release(FFMPEG_ASSET, slow), FFMPEG_ZIP: lambda: Trickle(slow, 16 * 1024)})
        code, lines = self.install("ffmpeg", "9.0.1", web, now=itertools.count(0, 10).__next__)
        self.assertEqual((code, lines[-1]), (0, "DONE 9.0.1"))

    def test_a_lookup_that_stops_moving_is_no_answer(self):
        # The lookups are read under the download's stall rule, so a trickle can't hold a run at "Looking up…"
        # for ever. Given up, it counts as no answer, the way a connection that failed does.
        data = ffmpeg_zip()
        answer, digest = release(FFMPEG_ASSET, data), hashlib.sha256(data).hexdigest().encode()
        served = []

        def trickle(body):  # a byte at a time, kept to see how far it was read
            def page():
                served.append(Trickle(body, 1))
                return served[-1]
            return page

        def crawl():  # a byte every half minute, each inside TIMEOUT: two bytes in the first minute
            return itertools.count(0, 30).__next__

        # GitHub's answer trickles, so gyan.dev's checksum stands in for its digest.
        web = Web({FFMPEG_API: trickle(answer), GYAN_VERSION: b"9.0.1\n", GYAN_SHA256: digest,
                   FFMPEG_ZIP: data})
        code, lines = self.install("ffmpeg", "9.0.1", web, now=crawl())
        self.assertEqual((code, lines[-1]), (0, "DONE 9.0.1"))
        self.assertEqual(web.asked, [FFMPEG_API, GYAN_VERSION, GYAN_SHA256, FFMPEG_ZIP])
        self.assertIn(f"{FFMPEG_API}: only 2 bytes arrived in 60 seconds", lines)
        self.assertEqual(served[-1].read_to, 2)

        deno = make_zip({"deno.exe": b"the new deno"})
        reach = "Couldn't reach {}. Check that you're connected to the internet, then try again."
        for what, tool, version, pages, sentence in (
                ("GitHub's answer, with nothing to stand in", "ffmpeg", "9.0.1",
                 {FFMPEG_API: trickle(answer), GYAN_VERSION: b"9.1\n"}, NETWORK),
                ("deno's checksum file", "deno", "2.9.6",
                 {DENO_API: release(DENO_ASSET, deno, digest=None),
                  DENO_ZIP + ".sha256sum": trickle(b"0" * 64)}, NETWORK),
                ("gyan.dev's newest version", "ffmpeg", "latest", {GYAN_VERSION: trickle(b"9.0.1\n")},
                 reach.format("www.gyan.dev")),
                ("deno's newest version", "deno", "latest", {DENO_LATEST: trickle(b"v2.9.6\n")},
                 reach.format("dl.deno.land"))):
            with self.subTest(what):
                served.clear()
                web = Web(pages)
                code, lines = self.install(tool, version, web, now=crawl())
                self.assertEqual((code, lines[-1]), (2, "ERROR " + sentence))
                self.assertEqual(served[-1].read_to, 2)
                self.assertNotIn(DENO_ZIP if tool == "deno" else FFMPEG_ZIP, web.asked)

    def test_a_lookup_that_never_ends_or_is_cut_short_is_no_answer(self):
        # The lookups are read up to ANSWER_LIMIT, so an answer that never ends can't fill the memory. Past
        # it, or cut short, an answer counts as no answer.
        data = ffmpeg_zip()
        digest = hashlib.sha256(data).hexdigest().encode()
        reach = "Couldn't reach {}. Check that you're connected to the internet, then try again."
        served = []

        def flood():
            served.append(Flood())
            return served[-1]

        # GitHub's answer never ends, so gyan.dev's checksum stands in for its digest.
        web = Web({FFMPEG_API: flood, GYAN_VERSION: b"9.0.1\n", GYAN_SHA256: digest, FFMPEG_ZIP: data})
        code, lines = self.install("ffmpeg", "9.0.1", web)
        self.assertEqual((code, lines[-1]), (0, "DONE 9.0.1"))
        self.assertEqual(web.asked, [FFMPEG_API, GYAN_VERSION, GYAN_SHA256, FFMPEG_ZIP])
        self.assertIn(f"{FFMPEG_API}: more than {fetch.ANSWER_LIMIT:,} bytes arrived", lines)
        self.assertLessEqual(served[-1].sent, fetch.ANSWER_LIMIT + fetch.CHUNK)

        deno = make_zip({"deno.exe": b"the new deno"})
        for what, tool, version, pages, sentence in (
                ("deno's checksum file", "deno", "2.9.6",
                 {DENO_API: release(DENO_ASSET, deno, digest=None), DENO_ZIP + ".sha256sum": flood}, NETWORK),
                ("gyan.dev's newest version", "ffmpeg", "latest", {GYAN_VERSION: flood},
                 reach.format("www.gyan.dev"))):
            with self.subTest(what):
                served.clear()
                code, lines = self.install(tool, version, Web(pages))
                self.assertEqual((code, lines[-1]), (2, "ERROR " + sentence))
                self.assertLessEqual(served[-1].sent, fetch.ANSWER_LIMIT + fetch.CHUNK)

        # Cut short: read1 just stops where read() would raise, and "v2.9" still reads as a version.
        def cut_short():
            response = Response(b"v2.9")
            response.headers["Content-Length"] = str(len(b"v2.9.6\n"))
            return response

        web = Web({DENO_LATEST: cut_short})
        code, lines = self.install("deno", "latest", web)
        self.assertEqual((code, lines[-1]), (2, "ERROR " + reach.format("dl.deno.land")))
        self.assertEqual(web.asked, [DENO_LATEST])  # never went on to look up a deno 2.9

    def test_leftovers_are_put_right_before_anything_else(self):
        old = self.tools / ".ffmpeg-old"
        old.mkdir(parents=True)
        (old / "ffmpeg.exe").write_bytes(b"the working ffmpeg")
        (self.tools / ".ffmpeg.part").write_bytes(b"half a download")
        (self.tools / ".ffmpeg-new").mkdir()
        (self.tools / ".ffmpeg-new" / "ffmpeg.exe").write_bytes(b"never checked")
        code, lines = self.install("ffmpeg", "9.0.1", Web({}))
        self.assertEqual(code, 2)  # the release isn't there, which is beside the point here
        self.assertEqual(sorted(os.listdir(self.tools)), [LOCK, "ffmpeg"])
        self.assertEqual((self.tools / "ffmpeg" / "ffmpeg.exe").read_bytes(), b"the working ffmpeg")
        # A log line for each thing put right, and only then the first step.
        self.assertEqual(lines.index("STEP Looking up FFmpeg 9.0.1…"), 3)

    def test_only_on_64_bit_windows(self):
        web = Web({})
        with mock.patch.object(fetch, "supported", return_value=False):
            result = self.install("ffmpeg", "latest", web)
        unsupported = "ERROR Tapewright can only download its own FFmpeg on 64-bit Windows."
        self.assertEqual(result, (1, [unsupported]))
        self.assertEqual((self.tools.exists(), web.asked), (False, []))

    def test_wrong_arguments(self):
        web = Web({})
        for tool, version in (("vlc", "latest"), ("ffmpeg", "nine"), ("ffmpeg", "9.0.1/../x"), ("deno", ""),
                              ("deno", "Latest"), ("ffprobe", "9.0.1")):
            with self.subTest((tool, version)):
                self.assertEqual(self.install(tool, version, web), (1, ["ERROR " + fetch.BAD_ARGUMENTS]))
        self.assertEqual((self.tools.exists(), web.asked), (False, []))

    def test_progress_is_reported_at_most_twice_a_second(self):
        big = {FOLDER + "doc/manual.html": random.Random(1).randbytes(20000)}
        data = ffmpeg_zip(extra=big, method=zipfile.ZIP_STORED)
        size, chunks = len(data), math.ceil(len(data) / 1024)

        def reported(lines):
            return [line for line in lines if line.startswith("PROGRESS ")]

        with mock.patch.object(fetch, "CHUNK", 1024):
            # The clock is read once as the download starts and once for each chunk, a quarter of a second
            # apart. The first chunk is always reported, so the start's reading shifts nothing.
            quarter_seconds = itertools.count(0, 0.25).__next__
            _, lines = self.install("ffmpeg", "9.0.1", self.ffmpeg_web(data), now=quarter_seconds)
            expected = [min(n * 1024, size) for n in range(1, chunks + 1, 2)]
            expected += [] if expected[-1] == size else [size]
            self.assertEqual(reported(lines), [f"PROGRESS {done} {size}" for done in expected])
            self.assertEqual(lines[-1], "DONE 9.0.1")
            # With a clock that never moves: the first chunk, and then only the end.
            _, lines = self.install("ffmpeg", "9.0.1", self.ffmpeg_web(data), now=lambda: 7.0)
            self.assertEqual(reported(lines), [f"PROGRESS 1024 {size}", f"PROGRESS {size} {size}"])

    def test_not_enough_space(self):
        data = ffmpeg_zip()
        web = self.ffmpeg_web(data)
        with mock.patch.object(fetch.shutil, "disk_usage", return_value=mock.Mock(free=10 * MB)):
            code, lines = self.install("ffmpeg", "9.0.1", web)
        # Refused before the download, for the zip and the programs unpacked from it together.
        megabytes = math.ceil((len(data) + FFMPEG_UNPACKED + MARGIN) / MB)
        self.assertEqual((code, lines[-1]), (4, no_space(megabytes)))
        self.assertEqual(web.asked, [FFMPEG_API])

    def test_not_enough_space_to_unpack(self):
        self.working_copy()
        # Stored, so that the download is over a megabyte and changes the figure.
        data = ffmpeg_zip(extra={FOLDER + "doc/manual.bin": bytes(3 * MB // 2)}, method=zipfile.ZIP_STORED)
        programs = len(b"the new ffmpeg") + len(b"the new ffprobe") + len(b"GPL")
        # Room for the download, and then, with the download on the disk, none for the programs.
        free = [mock.Mock(free=10 ** 12), mock.Mock(free=0)]
        with mock.patch.object(fetch.shutil, "disk_usage", side_effect=free) as disk_usage:
            code, lines = self.install("ffmpeg", "9.0.1", self.ffmpeg_web(data))
        # The sentence counts the download too: it is deleted before anyone reads the sentence.
        self.assertEqual((code, lines[-1]), (4, no_space(math.ceil((len(data) + programs + MARGIN) / MB))))
        self.assertEqual((disk_usage.call_count, steps(lines)[-1]), (2, "Unpacking…"))
        self.assert_only_the_working_copy_left()

    def test_a_download_of_unknown_size_is_held_to_the_recipes_ceiling(self):
        # GitHub's hourly limit is used up, so the API gave no size, and the server sent no Content-Length.
        data = ffmpeg_zip()
        digest = hashlib.sha256(data).hexdigest().encode()
        served = []

        def web():
            def response():
                served.append(Response(data, length=False))
                return served[-1]
            return Web({FFMPEG_API: 429, GYAN_VERSION: b"9.0.1\n", GYAN_SHA256: digest, FFMPEG_ZIP: response})

        # Room for less than the ceiling: refused as soon as it is clear no size is coming, before any byte.
        with mock.patch.object(fetch.shutil, "disk_usage", return_value=mock.Mock(free=FFMPEG_CEILING)):
            code, lines = self.install("ffmpeg", "9.0.1", web())
        megabytes = math.ceil((FFMPEG_CEILING + FFMPEG_UNPACKED + MARGIN) / MB)
        self.assertEqual((code, lines[-1], served[-1].read_to), (4, no_space(megabytes), 0))
        self.assertEqual(os.listdir(self.tools), [LOCK])

        # Longer than the ceiling: the reading stops just past it, and the download is damaged.
        with mock.patch.dict(fetch.RECIPES["ffmpeg"], ceiling=len(data) // 2), \
                mock.patch.object(fetch, "CHUNK", 64):
            code, lines = self.install("ffmpeg", "9.0.1", web())
        self.assertEqual((code, lines[-1]), (3, "ERROR " + DAMAGED))
        self.assertLessEqual(served[-1].read_to, len(data) // 2 + 64)
        self.assertEqual(os.listdir(self.tools), [LOCK])

        # Within it, only the SHA-256 can tell, and here it matches.
        self.assertEqual(self.install("ffmpeg", "9.0.1", web())[1][-1], "DONE 9.0.1")


class Script(unittest.TestCase):
    PATH = ROOT / "tapewright" / "fetch.py"

    def test_it_uses_only_the_standard_library_and_never_tapewright(self):
        imported = set()
        for node in ast.walk(ast.parse(self.PATH.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").partition(".")[0] if node.level == 0 else ".")
        self.assertEqual(sorted(imported - set(sys.stdlib_module_names)), [])

    def test_it_runs_by_its_path_from_any_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            done = subprocess.run([sys.executable, str(self.PATH), "ffmpeg", "latest"], cwd=folder,
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        lines = done.stdout.decode("utf-8").splitlines()
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(lines[-1], "ERROR " + fetch.BAD_ARGUMENTS)


class ReadByTheWindow(unittest.TestCase):
    """What the rest of Tapewright reads from fetch.py, checked against what fetch.py really prints."""

    @classmethod
    def setUpClass(cls):
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        cls.runs = outcomes(folder.name)

    def test_every_line_is_read_as_what_it_is(self):
        kinds = {"STEP": "step", "PROGRESS": "progress", "DONE": "done", "ERROR": "error"}
        seen = set()
        for name, (code, lines) in self.runs.items():
            with self.subTest(name):
                read = [deps.parse_fetch_line(line) for line in lines]
                for line, result in zip(lines, read):
                    kind = kinds.get(line.partition(" ")[0])
                    # Free text, such as an address or what a test run printed, must never pass for a step.
                    self.assertEqual(result and result[0], kind, line)
                    seen.add(kind)
                    if kind == "progress" and result[2]:
                        self.assertLessEqual(result[1], result[2], line)
                ends = [result for result in read if result and result[0] in ("done", "error")]
                self.assertEqual(ends, [read[-1]], lines)
                self.assertEqual(read[-1][0], "done" if code == 0 else "error")
        self.assertEqual(seen - {None}, set(kinds.values()))

    def test_every_failure_reaches_the_update_log_in_its_own_words(self):
        # What App's update worker does with a fetch: keep the last ERROR sentence, and give it to
        # update_failure_text with the exit code. tests/test_core.py replays these runs through the window.
        dep = deps.Dep("ffmpeg", "FFmpeg", True, state=deps.MISSING, action="Install",
                       command=deps.fetch_command("ffmpeg", "9.0.1"))
        sentences = {name: value for name, value in vars(fetch).items()
                     if name.isupper() and isinstance(value, str) and " " in value and value.endswith(".")}
        self.assertIn("NETWORK_ERROR", sentences)
        reached = set()
        for name, (code, lines) in self.runs.items():
            errors = [result[1] for result in map(deps.parse_fetch_line, lines)
                      if result and result[0] == "error"]
            with self.subTest(name):
                if code == 0:
                    self.assertEqual(errors, [])
                    continue
                self.assertIn(code, (1, 2, 3, 4, 5))
                self.assertEqual(deps.update_failure_text(dep, code, errors[-1]), errors[-1])
                matched = [key for key, sentence in sentences.items()
                           if re.fullmatch(re.sub(r"\\\{\w+\\\}", ".+", re.escape(sentence)), errors[-1])]
                self.assertEqual(len(matched), 1, errors[-1])
                reached.update(matched)
        # A sentence added to fetch.py fails here until a run in outcomes() ends with it.
        self.assertEqual(sorted(set(sentences) - reached), [])

    def test_what_a_check_offers_is_a_command_fetch_accepts(self):
        """From a check to a download: the command deps builds, handed to fetch.main as a child would be."""
        data, deno = ffmpeg_zip(), make_zip({"deno.exe": b"the new deno"})
        web = Web({GYAN_VERSION: b"9.0.1\n", FFMPEG_API: release(FFMPEG_ASSET, data), FFMPEG_ZIP: data,
                   DENO_LATEST: b"v2.9.6\n", DENO_API: release(DENO_ASSET, deno), DENO_ZIP: deno})
        answers = {deps.FFMPEG_RELEASE: "9.0.1\r\n", deps.DENO_RELEASE: "v2.9.6\n"}
        real = fetch.install

        def install(*args):  # main's own call, with only the network, the test runs and the pause swapped
            return real(*args, urlopen=web, run=lambda cmd, **kw: subprocess.CompletedProcess(
                cmd, 0, SAYS[Path(cmd[0]).stem].encode("utf-8"), b""), sleep=lambda seconds: None)

        with tempfile.TemporaryDirectory() as folder:
            tools = Path(folder) / "tools"
            with mock.patch.dict(os.environ, {deps.TOOLS_DIR_ENV: str(tools)}), \
                    mock.patch.object(deps, "WINDOWS", True), \
                    mock.patch.object(deps, "find_tool", return_value=""), \
                    mock.patch.object(deps, "fetch_text", side_effect=lambda url, **kw: answers[url]):
                # Online the checks pin the version they found; offline with nothing cached they have none.
                commands = [dep.command for online in (True, False)
                            for dep, _ in (deps.check_ffmpeg({}, online), deps.check_js("auto", {}, online))]
                commands.append(deps.fetch_command("ffmpeg", "9.0.1-essentials_build"))
            self.assertEqual([command[2:4] for command in commands],
                             [["ffmpeg", "9.0.1"], ["deno", "2.9.6"], ["ffmpeg", "latest"],
                              ["deno", "latest"], ["ffmpeg", "9.0.1"]])
            for command in commands:
                with self.subTest(command[2:4]):
                    self.assertEqual(command[0], procs.python_exe())
                    self.assertEqual(Path(command[1]).resolve(), Path(fetch.__file__).resolve())
                    out = io.StringIO()
                    with mock.patch.object(sys, "stdout", out), \
                            mock.patch.object(fetch, "install", side_effect=install), \
                            mock.patch.object(fetch, "supported", return_value=True):
                        code = fetch.main(command[2:])
                    done = "DONE 2.9.6" if command[2] == "deno" else "DONE 9.0.1"
                    self.assertEqual((code, out.getvalue().splitlines()[-1]), (0, done))
            self.assertEqual(sorted(os.listdir(tools)), [".deno.lock", LOCK, "deno", "ffmpeg"])


def alive(pid):
    """Whether the process pid is still running."""
    if os.name == "nt":
        listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=30).stdout
        return f'"{pid}"'.encode() in listed
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # killed, but not yet reaped by whoever inherited it, which its PID still answers for
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def ended(pid, within=10):
    """Whether pid ends within that many seconds. A killed process can take a moment to leave the lists."""
    deadline = time.monotonic() + within
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def kill(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


# fetch.main() as `python fetch.py` runs it, with each request sent to the test's server instead of the
# internet and each test run answered the way the real program would. fetch.py has no setting that points
# it at another server, and gets none for tests, so this is swapped in from outside.
CHILD = """\
import json, os, pathlib, subprocess, sys, time, urllib.request

sys.path.insert(0, os.environ["TEST_FETCH_FOLDER"])
import fetch

SERVER, SAYS = os.environ["TEST_FETCH_SERVER"], json.loads(os.environ["TEST_FETCH_SAYS"])
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PID_FILE = os.environ.get("TEST_FETCH_GRANDCHILD")
# Writes its own PID, since a Python launcher can stand between this child and the process that sleeps.
SLEEPER = ("import os, pathlib, sys, time; "
           "pathlib.Path(sys.argv[1] + '.tmp').write_text(str(os.getpid())); "
           "os.replace(sys.argv[1] + '.tmp', sys.argv[1]); time.sleep(120)")

if PID_FILE:
    # A grandchild of the Runner, like FFmpeg under yt-dlp. It shares none of this child's pipes, so only a
    # kill of the whole tree reaches it, and the Runner never waits on it for the end of its output.
    subprocess.Popen([sys.executable, "-c", SLEEPER, PID_FILE], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 30
    while not os.path.exists(PID_FILE) and time.monotonic() < deadline:
        time.sleep(0.05)


def urlopen(request, timeout=None):
    url = SERVER + "/" + request.full_url.partition("://")[2]
    return DIRECT.open(urllib.request.Request(url, headers=dict(request.header_items())), timeout=timeout)


def run(args, **kw):
    return subprocess.CompletedProcess(args, 0, SAYS[pathlib.Path(args[0]).stem].encode("utf-8"), b"")


fetch.supported = lambda: True
fetch.install.__kwdefaults__.update(urlopen=urlopen, run=run)
sys.exit(fetch.main())
"""
STALL_AT = 2 * 1024 * 1024  # well into the download, so PROGRESS lines come before the stall


class Cancelled(unittest.TestCase):
    """fetch.py the way the window runs it: a child of procs.Runner, cancelled partway through a download.

    The download comes over a real socket from 127.0.0.1, and Cancel is the real kill of the whole tree,
    which has to reach a grandchild too.
    """

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.tools = self.folder / "tools"
        # Stored rather than compressed, so the zip is at least as big as the random bytes inside it.
        self.zip = ffmpeg_zip(extra={FOLDER + "doc/manual.bin": random.Random(3).randbytes(3 * 1024 * 1024)},
                              method=zipfile.ZIP_STORED)
        self.stall, self.go, self.asked = threading.Event(), threading.Event(), []
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(self.go.set)  # runs first, so a download still held back lets go of its thread
        self.server = f"http://127.0.0.1:{server.server_address[1]}"

    def handler(self):
        test, stored = self, "/objects/" + FFMPEG_ASSET
        pages = {"/" + FFMPEG_API.partition("://")[2]: release(FFMPEG_ASSET, self.zip),
                 # GitHub answers a release download with a redirect to where the file is kept.
                 "/" + FFMPEG_ZIP.partition("://")[2]: stored, stored: self.zip}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                test.asked.append((self.path, self.headers.get("User-Agent")))
                page = pages.get(self.path)
                if page is None:
                    self.send_error(404)
                    return
                self.send_response(302 if isinstance(page, str) else 200)
                if isinstance(page, str):
                    self.send_header("Location", page)
                    page = b""
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                sent = 0
                try:
                    if page is test.zip and test.stall.is_set():
                        self.wfile.write(page[:STALL_AT])
                        test.go.wait(60)  # until the test has seen the download cancelled
                        sent = STALL_AT
                    self.wfile.write(page[sent:])
                except OSError:  # the child was killed while this was sending, which is the point
                    pass

            def log_message(self, *args):
                pass

        return Handler

    def run_child(self, runner, command, on_line, grandchild=None):
        """runner.run, with the child pointed at the server, and never left hanging if it goes wrong.

        grandchild, if given, is where the child's own sleeping child writes its PID.
        """
        watchdog = threading.Timer(60, runner.cancel)
        environment = {"TEST_FETCH_FOLDER": str(ROOT / "tapewright"), "TEST_FETCH_SERVER": self.server,
                       "TEST_FETCH_SAYS": json.dumps(SAYS)}
        if grandchild:
            environment["TEST_FETCH_GRANDCHILD"] = str(grandchild)
        with mock.patch.dict(os.environ, environment):
            watchdog.start()
            try:
                return runner.run(command, on_line)
            finally:
                watchdog.cancel()

    def test_the_working_copy_survives_and_the_next_run_clears_up(self):
        working = self.tools / "ffmpeg"
        working.mkdir(parents=True)
        before = {"ffmpeg.exe": b"the working ffmpeg", "ffprobe.exe": b"the working ffprobe"}
        for name, content in before.items():
            (working / name).write_bytes(content)
        child = self.folder / "child.py"
        child.write_text(CHILD, encoding="utf-8")
        with mock.patch.dict(os.environ, {deps.TOOLS_DIR_ENV: str(self.tools)}):
            command = deps.fetch_command("ffmpeg", "9.0.1")
        command[1] = str(child)  # in fetch.py's place, starting fetch.main() the same way

        self.stall.set()
        runner, lines, cancels = procs.Runner(), [], []
        pid_file = self.folder / "grandchild.pid"

        def on_line(line):
            lines.append(line)
            if line.startswith("PROGRESS ") and not cancels:
                cancels.append(threading.Thread(target=runner.cancel))  # from elsewhere, as the button does
                cancels[0].start()

        with self.assertRaises(procs.Cancelled):
            self.run_child(runner, command, on_line, grandchild=pid_file)
        for thread in cancels:
            thread.join(30)
        # The whole tree went, the grandchild too, which a kill of the child alone would have left running.
        # It sleeps for longer than the watchdog waits, so only Cancel can have ended it this soon.
        pid = int(pid_file.read_text())
        gone = ended(pid)
        if not gone:
            self.addCleanup(kill, pid)
        self.assertTrue(gone, f"the grandchild, PID {pid}, was still running after Cancel")
        # Read the way the window reads them, after a real trip through a pipe.
        read = [deps.parse_fetch_line(line) for line in lines]
        self.assertEqual([result[1] for result in read if result and result[0] == "step"],
                         ["Looking up FFmpeg 9.0.1…", "Downloading…"], lines)
        self.assertEqual([result[0] for result in read if result and result[0] != "step"][:1], ["progress"])
        self.assertFalse([result for result in read if result and result[0] in ("done", "error")])
        # Killed mid-download: the half-finished file is left for the next run, and the copy in use untouched.
        self.assertEqual(sorted(os.listdir(self.tools)), [LOCK, ".ffmpeg.part", "ffmpeg"])
        self.assertLess((self.tools / ".ffmpeg.part").stat().st_size, len(self.zip))
        self.assertEqual({path.name: path.read_bytes() for path in working.iterdir()}, before)

        self.stall.clear()
        self.go.set()
        lines = []
        # The killed run held the tool's lock, and the system has let go of it: otherwise this run would end
        # at once, saying another window is getting FFmpeg.
        code = self.run_child(procs.Runner(), command, lines.append)
        self.assertEqual((code, lines[-2:]), (0, ["STEP Done.", "DONE 9.0.1"]), lines)
        self.assertIn("Removed .ffmpeg.part, left over from an interrupted update.", lines)
        self.assertEqual(sorted(os.listdir(self.tools)), [LOCK, "ffmpeg"])
        self.assertEqual(sorted(path.name for path in working.iterdir()),
                         ["LICENSE", "ffmpeg.exe", "ffprobe.exe"])
        self.assertEqual((working / "ffmpeg.exe").read_bytes(), b"the new ffmpeg")
        # The redirect was followed, as a real download from GitHub needs.
        self.assertIn("/objects/" + FFMPEG_ASSET, [path for path, _ in self.asked])
        self.assertEqual({agent for _, agent in self.asked}, {"Tapewright-fetch"})


if __name__ == "__main__":
    unittest.main()
