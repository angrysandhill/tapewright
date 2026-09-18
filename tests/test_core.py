# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""Everything but fetch.py, with no network and no installed tools. The window is only ever built withdrawn.

    python -m unittest discover -s tests -v
"""

import ast
import datetime
import importlib
import io
import itertools
import os
import re
import sys
import tempfile
import tokenize
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# The window tests replay test_fetch's runs of fetch.py. discover already puts tests/ on the path, but
# `python -m unittest tests.test_core` doesn't.
sys.path.insert(1, str(ROOT / "tests"))

from tapewright import config, deps, help_content, jobs, launch, procs, versions  # noqa: E402

try:  # the window half draws with Tk; a Python built without it can still run every other test
    import tkinter
except ImportError:
    tkinter = deck = theme = None
else:  # outside the try, so an ImportError from Tapewright's own modules fails instead of skipping
    from tapewright import deck, theme  # noqa: E402


class Versions(unittest.TestCase):
    def test_shapes_from_each_project(self):
        self.assertEqual(versions.parse("2026.08.19"), (2026, 8, 19))
        self.assertEqual(versions.parse("2026.8.30.232658.dev0"), (2026, 8, 30, 232658))
        self.assertEqual(versions.parse("v2.9.6"), (2, 9, 6))
        self.assertEqual(versions.parse("8.1.1-full_build-www.gyan.dev"), (8, 1, 1))
        self.assertIsNone(versions.parse("N-121234-gabcdef"))
        self.assertIsNone(versions.parse(""))

    def test_numbers_not_strings(self):
        self.assertTrue(versions.is_newer("2026.8.30", "2026.8.4"))
        self.assertFalse(versions.is_newer("2026.8.19", "2026.08.19"))
        self.assertTrue(versions.is_newer("2026.8.30.232658.dev0", "2026.08.19"))
        self.assertFalse(versions.is_newer("8.1", "8.1.0"))
        self.assertFalse(versions.is_newer("not a version", "1.0"))

    def test_release_date(self):
        self.assertEqual(versions.release_date("2026.08.19"), datetime.date(2026, 8, 19))
        self.assertIsNone(versions.release_date("2.9.6"))


class Settings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "settings.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_no_file_means_defaults(self):
        s = config.Settings(self.path)
        s.load()
        self.assertEqual(s.data, config.DEFAULTS)
        self.assertIsNone(s.problem)

    def test_unreadable_file_is_set_aside_not_overwritten(self):
        self.path.write_text("{ not json", encoding="utf-8")
        s = config.Settings(self.path)
        s.load()
        self.assertIsNotNone(s.problem)
        bad = Path(self.dir.name) / "settings.json.bad"
        self.assertEqual(bad.read_text(encoding="utf-8"), "{ not json")

    def test_wrong_types_and_choices_are_ignored(self):
        self.path.write_text('{"check_on_startup": 1, "offline_max_age_days": true, '
                             '"ytdlp_channel": "beta", "outdated_policy": "block", "future": 3}',
                             encoding="utf-8")
        s = config.Settings(self.path)
        s.load()
        self.assertIs(s["check_on_startup"], True)
        self.assertEqual(s["offline_max_age_days"], 60)
        self.assertEqual(s["ytdlp_channel"], "stable")
        self.assertEqual(s["outdated_policy"], "block")
        self.assertEqual(s["future"], 3)

    def test_round_trip(self):
        s = config.Settings(self.path)
        s["mp3_quality"] = "192k"
        self.assertIsNone(s.save())
        again = config.Settings(self.path)
        again.load()
        self.assertEqual(again["mp3_quality"], "192k")


class Classify(unittest.TestCase):
    def test_links(self):
        self.assertEqual(jobs.classify(" https://youtu.be/abc "), ("url", "https://youtu.be/abc"))
        self.assertEqual(jobs.classify("youtu.be/abc"), ("url", "https://youtu.be/abc"))
        self.assertEqual(jobs.classify("ftp://x/y")[0], "error")

    def test_files(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "clip.mp4"
            f.write_bytes(b"x")
            self.assertEqual(jobs.classify(f'"{f}"'), ("file", f))
            self.assertEqual(jobs.classify(d)[0], "error")
            self.assertEqual(jobs.classify(str(Path(d) / "missing.mp4"))[0], "error")
        self.assertEqual(jobs.classify("   ")[0], "error")


class Progress(unittest.TestCase):
    # Real lines from yt-dlp 2026.08.19 with DL_TEMPLATE.
    def test_audio_download(self):
        line = "MGDL;NA;NA;none;opus;downloading;1637624;3275248;NA;106914.4;30"
        fraction, text = jobs.parse_download(line)
        self.assertAlmostEqual(fraction, 0.5)
        self.assertTrue(text.startswith("Downloading audio: 50% of 3.1 MB"), text)

    def test_video_download_and_finish(self):
        video = "MGDL;NA;NA;avc1.640028;none;downloading;1024;29443173;NA;482743.3;61"
        audio_done = "MGDL;NA;NA;none;mp4a.40.2;finished;3265702;3265702;NA;11852186.6;NA"
        self.assertIn("video", jobs.parse_download(video)[1])
        self.assertEqual(jobs.parse_download(audio_done)[0], 1.0)

    def test_unknown_size_and_playlist(self):
        fraction, text = jobs.parse_download("MGDL;3;10;avc1;none;downloading;5000000;NA;NA;NA;NA")
        self.assertIsNone(fraction)
        self.assertIn("(3 of 10)", text)
        self.assertIn("4.8 MB", text)

    def test_other_lines(self):
        self.assertIsNone(jobs.parse_download("[download] Destination: x.webm"))
        self.assertEqual(jobs.parse_postprocess("MGPP;ExtractAudio;started"),
                         ("Converting to MP3", "started"))


class Commands(unittest.TestCase):
    def job(self, **kw):
        base = dict(target="mp3", kind="url", source="https://youtu.be/abc", out_dir=Path("out"),
                    ffmpeg="C:/ff/ffmpeg.exe", ffprobe="C:/ff/ffprobe.exe")
        base.update(kw)
        return jobs.Job(**base)

    @staticmethod
    def contains(args, seq):
        return any(args[i:i + len(seq)] == seq for i in range(len(args)))

    def test_choices_match_settings(self):
        self.assertEqual(tuple(jobs.MP3_QUALITY), config.CHOICES["mp3_quality"])
        self.assertEqual(tuple(jobs.MP4_MODE), config.CHOICES["mp4_mode"])
        self.assertEqual(tuple(jobs.MP4_HEIGHT), config.CHOICES["mp4_max_height"])

    def test_mp3_link(self):
        args = jobs.ytdlp_command(self.job())
        self.assertTrue(self.contains(args, ["--print", jobs.FILE_TEMPLATE]))
        self.assertTrue(self.contains(args, ["--print", jobs.NAME_TEMPLATE]))
        self.assertIn("--no-quiet", args)
        self.assertTrue(self.contains(args, ["-x", "--audio-format", "mp3", "--audio-quality", "0"]))
        self.assertTrue(self.contains(args, ["--ffmpeg-location", "C:/ff/ffmpeg.exe"]))
        self.assertIn("--no-post-overwrites", args)
        self.assertEqual(args[-2:], ["--", "https://youtu.be/abc"])

    def test_mp4_resolution_cap_comes_before_quality(self):
        args = jobs.ytdlp_command(self.job(target="mp4", mp4_max_height="360"))
        sort = args[args.index("-S") + 1]
        self.assertEqual(sort, "vcodec:h264,lang,res:360,quality,fps,hdr:12,acodec:aac")
        best = jobs.ytdlp_command(self.job(target="mp4", mp4_mode="best", mp4_max_height="best"))
        self.assertNotIn("-S", best)

    def test_local_mp4_plans(self):
        h264 = {"codec_name": "h264", "pix_fmt": "yuv420p"}
        vp9 = {"codec_name": "vp9", "pix_fmt": "yuv420p"}
        aac, opus = {"codec_name": "aac"}, {"codec_name": "opus"}
        self.assertEqual(jobs.mp4_plans("compatible", h264, aac),
                         [("Copying the streams into MP4", ["-c", "copy"])])
        self.assertEqual(jobs.mp4_plans("compatible", vp9, opus)[0][1], jobs.ENCODE_VIDEO + jobs.ENCODE_AUDIO)
        self.assertEqual(jobs.mp4_plans("compatible", h264, opus)[0][1], ["-c:v", "copy"] + jobs.ENCODE_AUDIO)
        self.assertEqual(jobs.mp4_plans("compatible", vp9, None)[0][1], jobs.ENCODE_VIDEO)
        self.assertEqual([p[1] for p in jobs.mp4_plans("best", vp9, opus)],
                         [["-c", "copy"], jobs.ENCODE_VIDEO + jobs.ENCODE_AUDIO])

    def test_reserve_never_reuses_a_name(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "song.mp3").write_bytes(b"keep me")
            first = jobs.reserve_output(d, "song", "mp3")
            second = jobs.reserve_output(d, "song", "mp3")
            self.assertEqual((first.name, second.name), ("song (1).mp3", "song (2).mp3"))
            self.assertEqual((Path(d) / "song.mp3").read_bytes(), b"keep me")


class FakeRunner:
    """Feeds canned yt-dlp output to a job, as procs.Runner would from the real process."""

    def __init__(self, lines, code=0):
        self.lines, self.code = lines, code

    def run(self, args, on_line):
        for line in self.lines:
            on_line(line)
        return self.code


class Download(unittest.TestCase):
    def run_lines(self, lines, code=0, events=None):
        job = jobs.Job(target="mp3", kind="url", source="https://youtu.be/abc", out_dir=Path("."),
                       ffmpeg="ffmpeg", ffprobe="ffprobe")
        emit = (lambda *event: events.append(event)) if events is not None else (lambda *event: None)
        return jobs.run_job(job, FakeRunner(lines, code), emit)

    def test_the_deck_hears_each_phase_and_the_title(self):
        events = []
        self.run_lines(["MGNAME;Song; the remix",
                        "[info] Writing video thumbnail 41 to: C:/x/Song; the remix [abc].webp",
                        "[download] Destination: C:/x/Song; the remix [abc].fhls-1080p.mp4",
                        "MGDL;NA;NA;none;opus;downloading;1024;3275248;NA;106914.4;30",
                        "MGPP;ExtractAudio;started", "MGFILE;C:/x/Song; the remix [abc].mp3"],
                       events=events)
        phases = [value for kind, value in events if kind == "phase"]
        changes = [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]]
        self.assertEqual(changes, ["load", "play", "record"])
        # The title comes whole from yt-dlp, semicolon and all, never from a file name.
        self.assertEqual([value for kind, value in events if kind == "name"], ["Song; the remix"])

    def test_a_local_file_loads_then_records(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "clip.mp4"
            src.write_bytes(b"x")
            job = jobs.Job(target="mp3", kind="file", source=str(src), out_dir=Path(d),
                           ffmpeg="ffmpeg", ffprobe="ffprobe")
            events = []
            lines = ['{"streams": [{"index": 0, "codec_type": "audio", "codec_name": "aac"}], '
                     '"format": {"duration": "10"}}',
                     "out_time_us=5000000", "progress=continue", "progress=end"]
            self.assertTrue(jobs.run_job(job, FakeRunner(lines), lambda *e: events.append(e)).ok)
            self.assertEqual([v for k, v in events if k == "phase"], ["load", "record"])
            self.assertEqual([v for k, v in events if k == "name"], [])

    def test_saved(self):
        result = self.run_lines(["[ExtractAudio] Destination: C:/x/song.mp3", "MGFILE;C:/x/song.mp3"])
        self.assertEqual((result.ok, result.message), (True, "Saved song.mp3"))

    def test_already_present_is_not_reported_as_saved(self):
        result = self.run_lines(["[download] C:/x/song.mp3 has already been downloaded",
                                 "MGFILE;C:/x/song.mp3"])
        self.assertEqual((result.ok, result.message), (True, "Already in the folder: song.mp3"))

    def test_failure_reports_yt_dlp_error(self):
        result = self.run_lines(["[youtube] abc: Downloading webpage",
                                 "ERROR: [youtube] abc: Video unavailable"], 1)
        self.assertEqual((result.ok, result.message), (False, "[youtube] abc: Video unavailable"))


class Look(unittest.TestCase):
    @unittest.skipIf(theme is None, "a Python built without Tk")
    def test_every_listed_pair_is_readable(self):
        self.assertAlmostEqual(theme.contrast("#000000", "#ffffff"), 21.0)
        for foreground, background, minimum in theme.CONTRAST:
            ratio = theme.contrast(theme.C[foreground], theme.C[background])
            self.assertGreaterEqual(ratio, minimum, f"{foreground} on {background} is only {ratio:.2f}:1")

    def test_colors_are_only_spelled_out_in_theme(self):
        found = []
        for path in (ROOT / "tapewright").glob("*.py"):
            if path.name == "theme.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?|white|black", node.value)):
                    found.append(f"{path.name}:{node.lineno} {node.value}")
        self.assertEqual(found, [], "use theme.C, so the palette stays in one place")

    @unittest.skipIf(theme is None, "a Python built without Tk")
    def test_the_cassette_is_the_same_drawing_at_every_size(self):
        colors = set(theme.C.values()) | {None}
        for size in (16, 24, 32, 48, 64, 256):
            with self.subTest(size=size):
                pixels = theme.cassette_pixels(size)
                self.assertEqual(len(pixels), size)
                for row in pixels:
                    self.assertEqual(len(row), size)
                    self.assertLessEqual(set(row), colors)

                def at(x, y):
                    """The pixel over a point of the 48-unit design."""
                    return pixels[int(y * size / 48)][int(x * size / 48)]

                # Every part wider than the smallest size's pixels is where the design puts it.
                self.assertEqual((at(17, 28), at(31, 28)), (theme.C["hub_hole"],) * 2)
                self.assertEqual((at(24, 28), at(24, 16.5), at(24, 35.5)),
                                 (theme.C["window"], theme.C["paper"], theme.C["shell"]))
                self.assertEqual((pixels[0][0], pixels[-1][-1]), (None, None))

    @unittest.skipIf(theme is None, "a Python built without Tk")
    def test_the_cassette_at_48_pixels_is_one_pixel_to_a_unit(self):
        c = theme.C
        pixels = theme.cassette_pixels(48)
        # By name first, so a failure says which part moved.
        known = {(17, 28): "hub_hole", (31, 28): "hub_hole", (18, 28): "hub", (21, 28): "tape",
                 (22, 28): "window", (24, 16): "paper", (24, 21): "shell", (2, 10): "shell_edge",
                 (45, 37): "shell_edge", (0, 0): None, (1, 20): None, (47, 47): None}
        for (x, y), name in known.items():
            self.assertEqual(pixels[y][x], c[name] if name else None, f"({x}, {y}) should be {name}")

    @unittest.skipIf(deck is None, "a Python built without Tk")
    def test_the_reels_always_hold_the_same_tape(self):
        hub, full = 10.0, 30.0
        self.assertEqual(deck.reel_radii(0, hub, full), (full, hub))
        self.assertEqual(deck.reel_radii(1, hub, full), (hub, full))
        for fraction in (0.1, 0.5, 0.9):
            supply, take = deck.reel_radii(fraction, hub, full)
            self.assertAlmostEqual(supply ** 2 + take ** 2, hub ** 2 + full ** 2)
        slow, fast = deck.reel_speeds(0.0, hub, full)
        self.assertGreater(fast, slow)            # the nearly empty take-up reel spins faster
        self.assertTrue(slow > 0 and fast > 0)
        # Playing turns both reels clockwise, and rewinding turns them back.
        self.assertTrue(all(deck.MODES[mode][1] > 0 for mode in ("load", "play", "record")))
        self.assertLess(deck.MODES["rewind"][1], 0)

    @unittest.skipIf(deck is None, "a Python built without Tk")
    def test_counter_and_digits(self):
        self.assertEqual(deck.counter_text(3725), "1:02:05")
        self.assertEqual(deck.counter_text(36059), "0:00:59")
        self.assertEqual(set(deck.SEGMENTS), set("0123456789"))
        self.assertTrue(all(set(lit) <= set("abcdefg") for lit in deck.SEGMENTS.values()))


class Leftovers(unittest.TestCase):
    def test_announcements(self):
        cases = [
            ('[Merger] Merging formats into "C:/x/a b.mp4"', Path("C:/x/a b.mp4")),
            ("[download] Destination: C:/x/a.f137.mp4", Path("C:/x/a.f137.mp4")),
            ('[ThumbnailsConvertor] Converting thumbnail "C:/x/a.webp" to png', Path("C:/x/a.png")),
        ]
        for line, path in cases:
            self.assertEqual(jobs.announced_path(line), path)
        self.assertIsNone(jobs.announced_path("[download]  50.0% of 3.12MiB"))

    def test_only_new_unfinished_files_go(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            old, new, done = d / "old.mp3", d / "new [x].webm", d / "done.mp3"
            companions = [d / "new [x].webm.part-Frag3", d / "new [x].webm.ytdl", d / "new [x].temp.webm"]
            for p in [old, new, done, d / "unrelated.txt", d / "old.temp.mp3"] + companions:
                p.write_bytes(b"x")
            removed = jobs.remove_unfinished({old: True, new: False, done: False}, keep={done})
            self.assertEqual(removed, sorted(p.name for p in [new] + companions))
            for p in (old, done, d / "unrelated.txt", d / "old.temp.mp3"):
                self.assertTrue(p.exists(), p.name)


class Lines(unittest.TestCase):
    class Chunks:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        def read1(self, n):
            return self.chunks.pop(0) if self.chunks else b""

    def test_cr_lf_and_split_crlf(self):
        stream = self.Chunks([b"a\r", b"\nb\rc", b"\n", "d\xe9".encode("utf-8")])
        self.assertEqual(list(procs.iter_lines(stream)), ["a", "b", "c", "d\xe9"])


class Cancel(unittest.TestCase):
    """procs.Runner's cancel, against a stand-in for Popen, so no process starts."""

    def test_a_program_that_exited_0_before_the_kill_landed_was_not_cancelled(self):
        for code in (0, 3):
            with self.subTest(code=code):
                runner, lines = procs.Runner(), []
                child = mock.Mock(stdout=io.BufferedReader(io.BytesIO(b"DONE 9.0.1\n")))
                child.poll.return_value = code  # already gone, so there is nothing left to kill
                # Cancel lands once the program has exited, just before run() looks at the flag.
                child.wait.side_effect = lambda timeout=None, code=code: runner.cancel() or code
                with mock.patch.object(procs.subprocess, "Popen", return_value=child):
                    if code:  # a killed program never exits 0
                        self.assertRaises(procs.Cancelled, runner.run, ["fetch.py"], lines.append)
                    else:
                        self.assertEqual(runner.run(["fetch.py"], lines.append), 0)
                self.assertEqual(lines, ["DONE 9.0.1"])
                with mock.patch.object(procs.subprocess, "Popen") as popen:
                    self.assertRaises(procs.Cancelled, runner.run, ["the next program"], lines.append)
                popen.assert_not_called()  # either way, nothing after it starts

    def test_waiting_for_the_program_running_now(self):
        runner = procs.Runner()
        self.assertTrue(runner.wait(10))  # nothing is running
        runner._popen = child = mock.Mock()
        self.assertTrue(runner.wait(10))
        child.wait.assert_called_once_with(timeout=10)
        child.wait.side_effect = procs.subprocess.TimeoutExpired("fetch.py", 10)
        self.assertFalse(runner.wait(10))


class Deps(unittest.TestCase):
    def test_install_command_is_pinned(self):
        self.assertEqual(deps.ytdlp_install_command("2026.8.19", True)[-1], "yt-dlp[default]==2026.8.19")
        self.assertEqual(deps.ytdlp_install_command("2026.8.19", False)[-1], "yt-dlp==2026.8.19")
        self.assertEqual(deps.ytdlp_install_command("", True)[-1], "yt-dlp[default]")

    @unittest.skipUnless(os.name == "nt", "winget is Windows-only")
    def test_winget_package_from_path(self):
        path = (r"C:\Users\x\AppData\Local\Microsoft\WinGet\Packages"
                r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe")
        self.assertEqual(deps.winget_package_id(path), "Gyan.FFmpeg")
        self.assertEqual(deps.winget_package_id(r"C:\tools\ffmpeg.exe"), "")

    def test_problems_and_blocking(self):
        found = {
            "yt-dlp": deps.Dep("yt-dlp", "yt-dlp", True, state=deps.OUTDATED),
            "ffmpeg": deps.Dep("ffmpeg", "FFmpeg", True, state=deps.OK),
            "js": deps.Dep("js", "deno", False, state=deps.MISSING),
        }
        url = deps.problems_for(found, "url")
        self.assertEqual([d.key for d in url], ["yt-dlp", "js"])
        self.assertEqual(deps.problems_for(found, "file"), [])
        self.assertEqual(deps.blocking(url, "warn"), [])  # a missing *optional* tool only warns
        self.assertEqual(len(deps.blocking(url, "block")), 2)
        found["ffmpeg"].state = deps.MISSING
        self.assertEqual([d.key for d in deps.blocking(deps.problems_for(found, "file"), "warn")], ["ffmpeg"])

    def test_winget_commands_can_not_stop_to_ask(self):
        args = deps._winget_command("winget", "Gyan.FFmpeg")
        self.assertEqual(args[:5], ["winget", "upgrade", "--id", "Gyan.FFmpeg", "--exact"])
        for flag in ("--disable-interactivity", "--accept-source-agreements", "--accept-package-agreements"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--source") + 1], "winget")
        winget_exe = r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\winget.EXE"
        self.assertTrue(deps.is_winget([winget_exe, "upgrade"]))
        self.assertFalse(deps.is_winget(deps.ytdlp_install_command("2026.9.10", True)))
        self.assertFalse(deps.is_winget([]))

    def test_winget_exit_codes_arrive_with_either_sign(self):
        unsigned = 0x8A150046  # APPINSTALLER_CLI_ERROR_SOURCE_AGREEMENTS_NOT_ACCEPTED
        self.assertEqual(unsigned - 2 ** 32, -1978335162)  # the signed form returnCodes.md lists
        self.assertIn("terms", deps.winget_message(unsigned))
        self.assertEqual(deps.winget_message(unsigned), deps.winget_message(-1978335162))
        self.assertIn("try again later", deps.winget_message(deps.WINGET_UPDATE_NOT_APPLICABLE))
        self.assertEqual(deps.winget_message(0), "")
        self.assertIn("output is above", deps.winget_message(0x8A15FFFF))
        self.assertTrue(all(0x8A150000 <= code <= 0x8A15FFFF for code in deps.WINGET_MESSAGES))

    def test_update_confirmation_is_in_plain_words(self):
        pip = deps.Dep("yt-dlp", "yt-dlp", True, action="Update",
                       command=deps.ytdlp_install_command("2026.9.10", True))
        text = deps.describe_update(pip)
        self.assertEqual(text.split(". ")[0], "Update the Downloader (yt-dlp) to version 2026.9.10")
        self.assertIn("pypi.org", text)
        install = deps.Dep("yt-dlp", "yt-dlp", True, action="Install",
                           command=deps.ytdlp_install_command("", True))
        self.assertTrue(deps.describe_update(install).startswith(
            "Install the newest version of the Downloader (yt-dlp)."))
        stable = deps.Dep("yt-dlp", "yt-dlp", True, action="Switch to stable",
                          command=deps.ytdlp_install_command("2026.9.10", True))
        self.assertTrue(deps.describe_update(stable).startswith(
            "Switch the Downloader (yt-dlp) to 2026.9.10,"))
        winget = deps.Dep("ffmpeg", "FFmpeg", True, action="Update",
                          command=deps._winget_command("winget", "Gyan.FFmpeg"))
        text = deps.describe_update(winget)
        self.assertIn("Converter (FFmpeg)", text)
        self.assertIn("terms", text)  # winget accepts them for the user, so the box has to say so
        deno = deps.Dep("js", "deno", False, action="Update",
                        command=[r"C:\Users\x\.deno\bin\deno.exe", "upgrade"])
        self.assertIn("deno's own updater", deps.describe_update(deno))
        for dep in (pip, install, stable, winget, deno):
            self.assertNotIn("--", deps.describe_update(dep))

    def test_labels_say_what_each_tool_is_for(self):
        self.assertEqual(deps.Dep("yt-dlp", "yt-dlp", True).label, "Downloader (yt-dlp)")
        self.assertEqual(deps.Dep("js", "Node.js", False).label, "YouTube helper (Node.js)")
        self.assertEqual(deps.Dep("other", "Other", False).label, "Other")

    def test_a_missing_youtube_helper_is_named_after_what_would_fill_the_gap(self):
        with mock.patch.object(deps, "find_tool", return_value=""), \
                mock.patch.object(deps, "_winget", return_value="winget"):
            with mock.patch.object(deps, "WINDOWS", True):
                dep, _ = deps.check_js("auto", {}, False)
                self.assertEqual((dep.label, dep.action), ("YouTube helper (deno)", "Install deno"))
                dep, _ = deps.check_js("node", {}, False)
                self.assertEqual((dep.label, dep.command), ("YouTube helper (Node.js)", []))
            with mock.patch.object(deps, "WINDOWS", False):
                dep, _ = deps.check_js("auto", {}, False)
                self.assertEqual((dep.label, dep.state), ("YouTube helper (deno or Node.js)", deps.MISSING))
                dep, _ = deps.check_js("deno", {}, False)
                self.assertEqual((dep.label, dep.state), ("YouTube helper (deno)", deps.MISSING))
        dep, _ = deps.check_js("none", {}, False)
        self.assertEqual((dep.label, dep.state), ("YouTube helper (deno or Node.js)", deps.OK))

    def test_a_youtube_helper_whose_version_cannot_be_read_is_unknown_not_too_old(self):
        deno = r"C:\Users\x\AppData\Local\Microsoft\WinGet\Packages\DenoLand.Deno_x\deno.exe"
        timed_out = (None, "deno.exe gave no answer within 30s")
        with mock.patch.object(deps, "find_tool", side_effect=lambda name: deno if name == "deno" else ""), \
                mock.patch.object(deps, "_winget", return_value="winget"), \
                mock.patch.object(procs, "run_capture", return_value=timed_out):
            dep, _ = deps.check_js("auto", {}, False)
        self.assertEqual((dep.state, dep.installed, dep.action), (deps.UNKNOWN, "", ""))
        self.assertIn("gave no answer", dep.detail)
        self.assertEqual(deps.problems_for({"js": dep}, "url"), [])

    def test_try_again_later_only_for_a_tool_that_is_behind(self):
        upgrade = deps._winget_command("winget", "Gyan.FFmpeg")
        behind = deps.Dep("ffmpeg", "FFmpeg", True, state=deps.OUTDATED, command=upgrade)
        self.assertIn("try again later", deps.update_failure_text(behind, deps.WINGET_UPDATE_NOT_APPLICABLE))
        # A current FFmpeg with its ffprobe gone gets the same code, and waiting would never fix it.
        broken = deps.Dep("ffmpeg", "FFmpeg", True, state=deps.MISSING, command=upgrade)
        text = deps.update_failure_text(broken, deps.WINGET_UPDATE_NOT_APPLICABLE - 2 ** 32)
        self.assertNotIn("try again later", text)
        self.assertIn("winget uninstall --id Gyan.FFmpeg", text)
        self.assertTrue(text.endswith("(winget exit code 0x8A15002B)"), text)
        too_old = deps.Dep("js", "deno", False, state=deps.UNSUPPORTED,
                           command=deps._winget_command("winget", "DenoLand.Deno"))
        text = deps.update_failure_text(too_old, deps.WINGET_UPDATE_NOT_APPLICABLE)
        self.assertNotIn("try again later", text)
        self.assertIn("winget uninstall --id DenoLand.Deno", text)
        pip = deps.Dep("yt-dlp", "yt-dlp", True, state=deps.OUTDATED,
                       command=deps.ytdlp_install_command("", True))
        self.assertEqual(deps.update_failure_text(pip, 1),
                         "the command exited with code 1; its output is above.")

    def test_offline_falls_back_to_cache_then_to_age(self):
        def offline(*_):
            raise OSError("no network")

        cached = {"yt-dlp:stable": {"version": "2026.9.1", "checked": "2026-09-02T10:00:00"}}
        with mock.patch.object(procs, "run_capture", return_value=(0, "2026.08.19\n")), \
                mock.patch.object(deps, "latest_ytdlp", side_effect=offline):
            dep, _ = deps.check_ytdlp("stable", True, 60, cached, True)
            self.assertEqual((dep.state, dep.latest), (deps.OUTDATED, "2026.9.1"))
            dep, _ = deps.check_ytdlp("stable", True, 60, {}, True)
            old = versions.release_date("2026.08.19")
            expected = deps.OUTDATED if (datetime.date.today() - old).days > 60 else deps.UNKNOWN
            self.assertEqual(dep.state, expected)
            self.assertIn("no network", dep.detail)


WINGET_SOURCE = "_Microsoft.Winget.Source_8wekyb3d8bbwe"  # how winget ends each package's folder name


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


class FindTool(unittest.TestCase):
    """Where tools are found on a pretend Windows profile, with nothing on PATH."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.packages = self.root / "Microsoft" / "WinGet" / "Packages"
        self.tools = self.root / "Tapewright" / "tools"
        for patch in (mock.patch.object(deps, "WINDOWS", True),
                      mock.patch.object(deps.shutil, "which", return_value=None),
                      mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root),
                                                   deps.TOOLS_DIR_ENV: str(self.tools)}),
                      mock.patch.object(deps.Path, "home", return_value=self.root / "home")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_versioned_folders_sort_as_versions_whatever_order_they_arrive_in(self):
        def exe(folder):
            return Path("Packages") / "Gyan.FFmpeg" / folder / "bin" / "ffmpeg.exe"

        newest_first = [exe("ffmpeg-10.1-full_build"), exe("ffmpeg-10.0-full_build"),
                        exe("ffmpeg-9.0.1-full_build"), exe("unversioned")]
        self.assertGreater("ffmpeg-9.0.1-full_build", "ffmpeg-10.1-full_build")  # by name, 9.0.1 would win
        for order in itertools.permutations(newest_first):
            self.assertEqual(deps._newest_first(order), newest_first)

    def test_the_newest_ffmpeg_folder_is_the_one_found(self):
        package = self.packages / f"Gyan.FFmpeg{WINGET_SOURCE}"
        for version in ("9.0.1", "10.1", "10.0"):
            touch(package / f"ffmpeg-{version}-full_build" / "bin" / "ffmpeg.exe")
        newest = package / "ffmpeg-10.1-full_build" / "bin" / "ffmpeg.exe"
        self.assertEqual(deps.find_tool("ffmpeg"), str(newest))

    def test_a_winget_deno_with_no_link_is_found_before_the_home_copy(self):
        touch(self.root / "home" / ".deno" / "bin" / "deno.exe")
        packaged = touch(self.packages / f"DenoLand.Deno{WINGET_SOURCE}" / "deno.exe")
        self.assertEqual(deps.find_tool("deno"), str(packaged))
        packaged.unlink()
        self.assertEqual(deps.find_tool("deno"), str(self.root / "home" / ".deno" / "bin" / "deno.exe"))

    def test_ffprobe_beside_ffmpeg_comes_before_a_separate_search(self):
        bin_dir = self.packages / f"Gyan.FFmpeg{WINGET_SOURCE}" / "ffmpeg-9.0.1-full_build" / "bin"
        ffmpeg = touch(bin_dir / "ffmpeg.exe")
        ffprobe = touch(bin_dir / "ffprobe.exe")
        elsewhere = touch(self.root / "other" / "ffprobe.exe")
        found = {"ffmpeg": str(ffmpeg), "ffprobe": str(elsewhere)}
        with mock.patch.object(deps, "find_tool", side_effect=found.get), \
                mock.patch.object(deps, "_winget", return_value=""), \
                mock.patch.object(procs, "run_capture", return_value=(0, "ffmpeg version 9.0.1-full_build")):
            dep, _ = deps.check_ffmpeg({}, False)
            self.assertEqual(dep.ffprobe, str(ffprobe))
            # Deliberately still found elsewhere: calling this FFmpeg missing would block every
            # conversion for a setup that works for local files.
            ffprobe.unlink()
            dep, _ = deps.check_ffmpeg({}, False)
            self.assertEqual(dep.ffprobe, str(elsewhere))

    def test_tapewrights_own_copy_is_found_before_path(self):
        on_path = touch(self.root / "bin" / "ffmpeg.exe")
        with mock.patch.object(deps.shutil, "which", return_value=str(on_path)):
            self.assertEqual(deps.find_tool("ffmpeg"), str(on_path))  # no own copy yet
            own = touch(self.tools / "ffmpeg" / "ffmpeg.exe")
            # An older ffmpeg on PATH must not shadow it, or "Update" would never seem to work.
            self.assertEqual(deps.find_tool("ffmpeg"), str(own))
            # Unless it won't run: then the rest of the search goes on without it.
            self.assertEqual(deps.find_tool("ffmpeg", own=False), str(on_path))
        with mock.patch.object(deps.shutil, "which", return_value=str(own)):  # the tools folder put on PATH
            self.assertEqual(deps.find_tool("ffmpeg", own=False), "")
        self.assertTrue(deps.is_own_copy(str(own)))
        self.assertFalse(deps.is_own_copy(str(on_path)))
        self.assertFalse(deps.is_own_copy(""))
        deno = touch(self.tools / "deno" / "deno.exe")
        touch(self.root / "home" / ".deno" / "bin" / "deno.exe")
        self.assertEqual(deps.find_tool("deno"), str(deno))
        self.assertEqual(deps.own_copy("ffprobe"), self.tools / "ffmpeg" / "ffprobe.exe")
        self.assertIsNone(deps.own_copy("node"))

    def test_the_tools_folder(self):
        self.assertEqual(deps.tools_dir(), self.tools)  # the variable wins, on any system
        with mock.patch.object(deps, "WINDOWS", False):
            self.assertEqual(deps.tools_dir(), self.tools)
        with mock.patch.dict(os.environ):
            del os.environ[deps.TOOLS_DIR_ENV]
            self.assertEqual(deps.tools_dir(), self.root / "Tapewright" / "tools")
            del os.environ["LOCALAPPDATA"]
            local = self.root / "home" / "AppData" / "Local"
            self.assertEqual(deps.tools_dir(), local / "Tapewright" / "tools")
            with mock.patch.object(deps, "WINDOWS", False):
                # Elsewhere a package manager looks after FFmpeg, so there is no folder and no own copy.
                self.assertIsNone(deps.tools_dir())
                self.assertIsNone(deps.own_copy("ffmpeg"))
                self.assertFalse(deps.is_own_copy(str(self.tools / "ffmpeg" / "ffmpeg.exe")))
                self.assertEqual(deps.find_tool("ffmpeg"), "")


class HelperCommands(unittest.TestCase):
    """Which command each kind of FFmpeg and deno copy is offered, on a pretend Windows with no network."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.fetched = []
        latest = {deps.FFMPEG_RELEASE: "9.1\n", deps.DENO_RELEASE: "v2.9.6\n"}

        def fetch_text(url, timeout=20, headers=None):
            self.fetched.append(url)
            return latest[url]

        for patch in (mock.patch.object(deps, "WINDOWS", True),
                      mock.patch.object(deps, "_winget", return_value="winget"),
                      mock.patch.object(deps, "fetch_text", side_effect=fetch_text),
                      mock.patch.dict(os.environ, {deps.TOOLS_DIR_ENV: str(self.root / "tools")}),
                      mock.patch.object(deps.Path, "home", return_value=self.root / "home")):
            patch.start()
            self.addCleanup(patch.stop)
        self.packages = self.root / "Microsoft" / "WinGet" / "Packages"
        self.winget_ffmpeg = str(self.packages / f"Gyan.FFmpeg{WINGET_SOURCE}"
                                 / "ffmpeg-9.0.1-full_build" / "bin" / "ffmpeg.exe")

    def own(self, name):
        return str(touch(deps.own_copy(name)))

    def ffmpeg(self, path, version="9.0.1", ffprobe=""):
        """check_ffmpeg, online, finding ffmpeg at path and ffprobe (if not beside it) at ffprobe."""
        found = {"ffmpeg": path, "ffprobe": ffprobe}
        output = (0, f"ffmpeg version {version}-essentials_build-www.gyan.dev Copyright (c) 2000-2026")
        with mock.patch.object(deps, "find_tool", side_effect=found.get), \
                mock.patch.object(procs, "run_capture", return_value=output):
            return deps.check_ffmpeg({}, True)[0]

    def js(self, choice, deno="", node="", output=""):
        found = {"deno": deno, "node": node}
        with mock.patch.object(deps, "find_tool", side_effect=found.get), \
                mock.patch.object(procs, "run_capture", return_value=(0, output)):
            return deps.check_js(choice, {}, True)[0]

    @staticmethod
    def deno(version):
        return f"deno {version} (stable, release, x86_64-pc-windows-msvc)\nv8 14.0.0\ntypescript 5.9.2\n"

    def test_a_missing_ffmpeg_gets_tapewrights_own_copy_at_the_version_just_looked_up(self):
        dep = self.ffmpeg("")
        self.assertEqual((dep.state, dep.action, dep.latest), (deps.MISSING, "Install", "9.1"))
        self.assertEqual(dep.command, deps.fetch_command("ffmpeg", "9.1"))
        self.assertEqual(dep.how, "Tapewright's own copy, from github.com")
        self.assertEqual(self.fetched, [deps.FFMPEG_RELEASE])

    def test_ffmpeg_is_updated_the_way_its_copy_came(self):
        own = self.own("ffmpeg")
        self.own("ffprobe")
        fetch = deps.fetch_command("ffmpeg", "9.1")
        dep = self.ffmpeg(own)
        self.assertEqual((dep.state, dep.action, dep.command), (deps.OUTDATED, "Update", fetch))
        self.assertEqual(dep.ffprobe, str(deps.own_copy("ffprobe")))
        dep = self.ffmpeg(own, "9.1")
        self.assertEqual((dep.state, dep.action, dep.command), (deps.OK, "", []))
        deps.own_copy("ffprobe").unlink()
        dep = self.ffmpeg(own, "9.1")
        self.assertEqual((dep.state, dep.action, dep.command), (deps.MISSING, "Install", fetch))

        dep = self.ffmpeg(self.winget_ffmpeg, ffprobe=str(self.root / "elsewhere" / "ffprobe.exe"))
        upgrade = deps._winget_command("winget", "Gyan.FFmpeg")
        self.assertEqual((dep.state, dep.action, dep.command), (deps.OUTDATED, "Update", upgrade))
        # Upgrading can't bring back the ffprobe gone from winget's copy, so Tapewright offers its own.
        dep = self.ffmpeg(self.winget_ffmpeg)
        self.assertEqual((dep.state, dep.action, dep.command), (deps.MISSING, deps.OWN_COPY_ACTION, fetch))

        on_path = str(self.root / "bin" / "ffmpeg.exe")
        ffprobe = str(self.root / "bin" / "ffprobe.exe")
        elsewhere = "Update FFmpeg the way you installed it, or let Tapewright keep its own copy."
        for dep, state in ((self.ffmpeg(on_path, ffprobe=ffprobe), deps.OUTDATED),
                           (self.ffmpeg(on_path), deps.MISSING)):
            self.assertEqual((dep.state, dep.action, dep.command), (state, deps.OWN_COPY_ACTION, fetch))
            self.assertEqual(dep.how, elsewhere)
        dep = self.ffmpeg(on_path, "9.1", ffprobe=ffprobe)
        self.assertEqual((dep.state, dep.action, dep.command), (deps.OK, "", []))

    def test_deno_is_updated_the_way_its_copy_came(self):
        fetch = deps.fetch_command("deno", "2.9.6")
        dep = self.js("auto")
        self.assertEqual((dep.label, dep.state, dep.action, dep.command),
                         ("YouTube helper (deno)", deps.MISSING, "Install deno", fetch))
        self.assertEqual(self.js("node").command, [])  # Node.js never gets an install button

        own = self.own("deno")
        winget = str(self.packages / f"DenoLand.Deno{WINGET_SOURCE}" / "deno.exe")
        home = str(self.root / "home" / ".deno" / "bin" / "deno.exe")
        elsewhere = str(self.root / "bin" / "deno.exe")
        upgrade = deps._winget_command("winget", "DenoLand.Deno")
        for version, state in (("2.9.5", deps.OUTDATED), ("2.2.0", deps.UNSUPPORTED)):
            with self.subTest(version=version):
                dep = self.js("auto", deno=own, output=self.deno(version))
                self.assertEqual((dep.state, dep.action, dep.command), (state, "Update", fetch))
                dep = self.js("deno", deno=winget, output=self.deno(version))
                self.assertEqual((dep.state, dep.action, dep.command), (state, "Update", upgrade))
                dep = self.js("auto", deno=home, output=self.deno(version))
                self.assertEqual((dep.state, dep.action, dep.command), (state, "Update", [home, "upgrade"]))
                dep = self.js("auto", deno=elsewhere, output=self.deno(version))
                self.assertEqual((dep.state, dep.action, dep.command), (state, deps.OWN_COPY_ACTION, fetch))
        self.assertEqual(self.js("auto", deno=own, output=self.deno("2.9.6")).command, [])
        self.assertEqual(self.js("auto", deno=elsewhere, output=self.deno("2.9.6")).state, deps.OK)
        node = self.js("auto", node=str(self.root / "bin" / "node.exe"), output="v20.11.0\n")
        self.assertEqual((node.state, node.action, node.command), (deps.UNSUPPORTED, "", []))

    def test_tapewrights_own_copy_that_wont_run_gives_way_or_is_fetched_again(self):
        own, own_deno = self.own("ffmpeg"), self.own("deno")
        self.own("ffprobe")
        other = self.root / "bin" / "ffmpeg.exe"
        home_deno = self.root / "home" / ".deno" / "bin" / "deno.exe"
        for path in (other, other.with_name("ffprobe.exe"), home_deno):
            touch(path)
        blocked = "could not run ffmpeg.exe: [WinError 1260] This program is blocked by group policy."
        answers = {own: (None, blocked), str(other): (0, "ffmpeg version 9.1-essentials_build-www.gyan.dev"),
                   own_deno: (1, ""), str(home_deno): (0, self.deno("2.9.6"))}
        on_path = {"ffmpeg": str(other), "ffprobe": str(other.with_name("ffprobe.exe"))}
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}), \
                mock.patch.object(deps.shutil, "which", side_effect=on_path.get), \
                mock.patch.object(procs, "run_capture", side_effect=lambda args, **kw: answers[str(args[0])]):
            # Another copy that answers is used, and the reason is said first.
            dep = deps.check_ffmpeg({}, True)[0]
            self.assertEqual((dep.state, dep.path, dep.ffprobe, dep.command),
                             (deps.OK, str(other), on_path["ffprobe"], []))
            self.assertTrue(dep.detail.startswith(
                f"Tapewright's own copy won't run ({blocked[:-1]}), so another copy is used. Latest release"),
                dep.detail)
            js = deps.check_js("auto", {}, True)[0]
            self.assertEqual((js.state, js.path, js.js_args), (deps.OK, str(home_deno),
                                                               ["--js-runtimes", f"deno:{home_deno}"]))
            self.assertIn("so another copy is used", js.detail)

            # With nothing else, it is never called out of date, but it gets the button that fetches it again.
            on_path.clear()
            home_deno.unlink()
            dep = deps.check_ffmpeg({}, True)[0]
            self.assertEqual((dep.state, dep.installed, dep.action, dep.command),
                             (deps.UNKNOWN, "", "Install", deps.fetch_command("ffmpeg", "9.1")))
            self.assertEqual(dep.detail, f"Tapewright's own copy of FFmpeg won't run: {blocked[:-1]}.")
            js = deps.check_js("auto", {}, True)[0]
            self.assertEqual((js.state, js.action, js.command),
                             (deps.UNKNOWN, "Install", deps.fetch_command("deno", "2.9.6")))
            self.assertEqual(js.detail, "Tapewright's own copy of deno won't run: "
                                        "it exited with code 1 and printed nothing.")
            self.assertEqual(deps.problems_for({"ffmpeg": dep, "js": js}, "url"), [])

            # On Automatic, a Node.js that answers takes the own deno's place, as it does with no deno at
            # all, and the reason is said first there too.
            node = str(self.root / "nodejs" / "node.exe")
            on_path["node"], answers[node] = node, (0, "v22.3.0\n")
            reason = ("Tapewright's own copy won't run (it exited with code 1 and printed nothing), "
                      "so another copy is used.")
            js = deps.check_js("auto", {}, True)[0]
            self.assertEqual((js.state, js.name, js.path, js.action, js.command),
                             (deps.OK, "Node.js", node, "", []))
            self.assertEqual(js.js_args, ["--no-js-runtimes", "--js-runtimes", f"node:{node}"])
            self.assertTrue(js.detail.startswith(f"{reason} Node.js is checked only"), js.detail)
            # A Node.js too old for yt-dlp isn't taken: the own deno keeps its button that fetches it again,
            # rather than trading it for a warning before every link that nothing here can fix.
            answers[node] = (0, "v20.11.0\n")
            js = deps.check_js("auto", {}, True)[0]
            self.assertEqual((js.state, js.path, js.action, js.command),
                             (deps.UNKNOWN, own_deno, "Install", deps.fetch_command("deno", "2.9.6")))
            self.assertEqual(deps.problems_for({"js": js}, "url"), [])
            # Set to deno, Node.js is never one to use, so the button that fetches deno again stays.
            js = deps.check_js("deno", {}, True)[0]
            self.assertEqual((js.state, js.path, js.action, js.command),
                             (deps.UNKNOWN, own_deno, "Install", deps.fetch_command("deno", "2.9.6")))

        # "Development build" only when a version was read; any other copy that names none just says so.
        on_path = {"ffmpeg": str(other), "ffprobe": str(other.with_name("ffprobe.exe"))}
        development = (0, "ffmpeg version N-121000-g1a2b3c4d5e Copyright")
        for output, words in ((development, "is a development build"),
                              ((0, ""), "Couldn't read the version of FFmpeg at"),
                              ((None, "ffmpeg.exe gave no answer within 30s"), "gave no answer within 30s.")):
            with self.subTest(output=output), mock.patch.object(deps, "find_tool", side_effect=on_path.get), \
                    mock.patch.object(procs, "run_capture", return_value=output):
                dep = deps.check_ffmpeg({}, True)[0]
                self.assertEqual((dep.state, dep.action, dep.command), (deps.UNKNOWN, "", []))
                self.assertIn(words, dep.detail)
                self.assertEqual("development build" in dep.detail, bool(output[0] == 0 and output[1]))

    def test_nothing_is_fetched_outside_windows(self):
        with mock.patch.object(deps, "WINDOWS", False), mock.patch.object(deps, "_winget", return_value=""):
            dep = self.ffmpeg("")
            self.assertEqual((dep.state, dep.command), (deps.MISSING, []))
            self.assertIn("ffmpeg.org", dep.how)
            dep = self.ffmpeg(str(self.root / "bin" / "ffmpeg"))
            self.assertEqual((dep.state, dep.command), (deps.MISSING, []))
            self.assertEqual(self.js("auto").command, [])
            dep = self.js("auto", deno=str(self.root / "bin" / "deno"), output=self.deno("2.2.0"))
            self.assertEqual((dep.state, dep.command), (deps.UNSUPPORTED, []))
        self.assertNotIn(deps.FFMPEG_RELEASE, self.fetched)

    def test_ytdlp_says_where_pip_puts_it(self):
        with mock.patch.object(procs, "run_capture", return_value=(0, "2026.09.10\n")), \
                mock.patch.object(deps, "latest_ytdlp", return_value="2026.9.10"):
            dep, _ = deps.check_ytdlp("stable", True, 60, {}, True)
        self.assertEqual(dep.how, "pip, into the Python that runs Tapewright")


class Fetcher(unittest.TestCase):
    """How the rest of Tapewright talks to fetch.py: its command line, its output and the words around it."""

    INSTALL = ("Install the Converter (FFmpeg), about 110 MB from github.com. Tapewright checks the download "
               "before using it and keeps it in its own folder, so nothing else on this computer changes.")

    @staticmethod
    def dep(key, name, action, command, state=deps.MISSING):
        return deps.Dep(key, name, key != "js", state=state, action=action, command=command)

    def test_the_fetcher_runs_by_path_with_the_version_the_check_found(self):
        tools = Path(tempfile.gettempdir()) / "tapewright-test-tools"
        with mock.patch.dict(os.environ, {deps.TOOLS_DIR_ENV: str(tools)}):
            command = deps.fetch_command("ffmpeg", "9.0.1")
            self.assertEqual(command,
                             [procs.python_exe(), str(deps.FETCH_SCRIPT), "ffmpeg", "9.0.1", str(tools)])
            self.assertEqual(deps.fetch_command("deno", "")[2:4], ["deno", "latest"])
        self.assertTrue(deps.FETCH_SCRIPT.is_absolute())
        self.assertEqual(deps.FETCH_SCRIPT, Path(deps.__file__).with_name("fetch.py"))
        self.assertTrue(deps.is_fetch(command))
        self.assertFalse(deps.is_fetch(deps.ytdlp_install_command("2026.9.10", True)))
        self.assertFalse(deps.is_fetch(deps._winget_command("winget", "Gyan.FFmpeg")))
        self.assertFalse(deps.is_fetch([r"C:\Users\x\.deno\bin\deno.exe", "upgrade"]))
        self.assertFalse(deps.is_fetch([]))

    def test_the_confirmation_says_what_is_downloaded_and_that_it_is_checked(self):
        install = self.dep("ffmpeg", "FFmpeg", "Install", deps.fetch_command("ffmpeg", "9.0.1"))
        self.assertEqual(deps.describe_update(install), self.INSTALL)
        update = self.dep("ffmpeg", "FFmpeg", "Update", deps.fetch_command("ffmpeg", "9.1"), deps.OUTDATED)
        self.assertTrue(deps.describe_update(update).startswith(
            "Update the Converter (FFmpeg) to version 9.1, about 110 MB from github.com. "))
        newest = self.dep("js", "deno", "Update", deps.fetch_command("deno", ""), deps.UNSUPPORTED)
        self.assertTrue(deps.describe_update(newest).startswith(
            "Update the YouTube helper (deno) to its newest version, about 45 MB from github.com. "))
        own = self.dep("ffmpeg", "FFmpeg", deps.OWN_COPY_ACTION, deps.fetch_command("ffmpeg", "9.1"),
                       deps.OUTDATED)
        self.assertTrue(deps.describe_update(own).startswith(
            "Get Tapewright's own copy of the Converter (FFmpeg), about 110 MB from github.com. "))
        deno = self.dep("js", "deno", "Install deno", deps.fetch_command("deno", "2.9.6"))
        self.assertTrue(deps.describe_update(deno).startswith(
            "Install the YouTube helper (deno), about 45 MB from github.com. "))
        for dep in (install, update, newest, own, deno):
            self.assertNotIn("--", deps.describe_update(dep))
            self.assertIn("nothing else on this computer changes", deps.describe_update(dep))

    def test_download_hints(self):
        pip = self.dep("yt-dlp", "yt-dlp", "Install", deps.ytdlp_install_command("2026.9.10", True))
        self.assertEqual(deps.download_hint(pip), "a few MB from pypi.org")
        ffmpeg = self.dep("ffmpeg", "FFmpeg", "Install", deps.fetch_command("ffmpeg", "9.1"))
        self.assertEqual(deps.download_hint(ffmpeg), "about 110 MB from github.com")
        deno = self.dep("js", "deno", "Install deno", deps.fetch_command("deno", "latest"))
        self.assertEqual(deps.download_hint(deno), "about 45 MB from github.com")
        for command in (deps._winget_command("winget", "Gyan.FFmpeg"),
                        [r"C:\Users\x\.deno\bin\deno.exe", "upgrade"], []):
            self.assertEqual(deps.download_hint(self.dep("ffmpeg", "FFmpeg", "Update", command)), "")

    def test_a_failed_or_cancelled_download_is_explained(self):
        fetch = self.dep("ffmpeg", "FFmpeg", "Install", deps.fetch_command("ffmpeg", "9.1"))
        sentence = "Couldn't reach github.com. Check that you're connected to the internet, then try again."
        self.assertEqual(deps.update_failure_text(fetch, 2, sentence), sentence)
        self.assertEqual(deps.update_failure_text(fetch, 2),
                         "the download stopped with code 2; its output is above.")
        self.assertEqual(deps.cancel_text(fetch), "cancelled. Nothing was replaced.")
        pip = self.dep("yt-dlp", "yt-dlp", "Update", deps.ytdlp_install_command("", True), deps.OUTDATED)
        self.assertEqual(deps.update_failure_text(pip, 1, sentence),
                         "the command exited with code 1; its output is above.")
        self.assertEqual(deps.cancel_text(pip),
                         "update cancelled. It may be half-installed; run the update again.")
        upgrade = deps._winget_command("winget", "Gyan.FFmpeg")
        winget = self.dep("ffmpeg", "FFmpeg", "Update", upgrade)
        self.assertEqual(deps.update_failure_text(winget, deps.WINGET_UPDATE_NOT_APPLICABLE, sentence),
                         deps.update_failure_text(winget, deps.WINGET_UPDATE_NOT_APPLICABLE))
        self.assertEqual(deps.cancel_text(winget), deps.cancel_text(pip))
        # Cancelled after the fetcher said it was putting the helper in place: a kill already on its way can
        # land during the swap or after it, so nothing is promised, and the check after the batch says.
        swapped = "cancelled while it was being put in place. The check after it shows what is there now."
        self.assertEqual(deps.cancel_text(fetch, swapped=True), swapped)
        self.assertEqual(deps.cancel_text(pip, swapped=True), deps.cancel_text(pip))

    def test_every_kind_of_fetcher_line(self):
        step = "Looking up FFmpeg 9.0.1…"
        self.assertEqual(deps.parse_fetch_line(f"STEP {step}"), ("step", step))
        self.assertEqual(deps.parse_fetch_line("PROGRESS 48000000 111253802\r"),
                         ("progress", 48000000, 111253802))
        self.assertEqual(deps.parse_fetch_line("DONE 9.0.1"), ("done", "9.0.1"))
        error = "The download was damaged on the way, so it was thrown away. Try again."
        self.assertEqual(deps.parse_fetch_line(f"ERROR {error}"), ("error", error))
        url = ("https://github.com/GyanD/codexffmpeg/releases/download/9.0.1/"
               "ffmpeg-9.0.1-essentials_build.zip")
        for junk in ("", "   ", "STEP", "STEP ", "DONE", "ERROR", "PROGRESS", "PROGRESS 12",
                     "PROGRESS 12 of 100", "PROGRESS -1 100", "PROGRESS 1 2 3", "progress 1 2",
                     "Step Downloading…", "ERROR: [youtube] abc: Video unavailable", "Downloading STEP 1",
                     url):
            self.assertIsNone(deps.parse_fetch_line(junk), junk)


class Setup(unittest.TestCase):
    @staticmethod
    def found(ytdlp=deps.OK, ffmpeg=deps.OK, js=deps.OK, commands=True):
        """Each helper with a problem gets the command a check on Windows gives it: pip, or the fetcher."""
        fixes = {"yt-dlp": deps.ytdlp_install_command("2026.9.10", True),
                 "ffmpeg": deps.fetch_command("ffmpeg", "9.0.1"), "js": deps.fetch_command("deno", "2.9.6")}

        def dep(key, name, state):
            command = fixes[key] if commands and state != deps.OK else []
            return deps.Dep(key, name, key != "js", state=state, command=command)

        # Deliberately out of order: the screen installs in its own order, whatever the check's.
        return {"js": dep("js", "deno", js), "ffmpeg": dep("ffmpeg", "FFmpeg", ffmpeg),
                "yt-dlp": dep("yt-dlp", "yt-dlp", ytdlp)}

    def test_what_the_setup_screen_installs_and_in_what_order(self):
        found = self.found(deps.OUTDATED, deps.MISSING, deps.UNSUPPORTED)
        self.assertEqual(deps.setup_keys(found), ["yt-dlp", "ffmpeg", "js"])
        found["ffmpeg"].command = []  # nothing here can install it
        found["js"].state, found["js"].installed = deps.UNKNOWN, "2.9.6"  # runs; not checked for updates
        self.assertEqual(deps.setup_keys(found), ["yt-dlp"])
        self.assertEqual(deps.setup_keys({}), [])
        # A winget upgrade accepts terms for the user, and deno's own updater replaces a copy nothing
        # checks, so both are left to the Settings tab, which asks first.
        others = self.found(ffmpeg=deps.OUTDATED, js=deps.UNSUPPORTED)
        others["ffmpeg"].command = deps._winget_command("winget", "Gyan.FFmpeg")
        others["js"].command = [r"C:\Users\x\.deno\bin\deno.exe", "upgrade"]
        self.assertEqual(deps.setup_keys(others), [])
        others["yt-dlp"].state, others["yt-dlp"].command = deps.OUTDATED, deps.ytdlp_install_command("", True)
        self.assertEqual(deps.setup_keys(others), ["yt-dlp"])
        self.assertTrue(deps.is_pip(others["yt-dlp"].command))
        for command in (deps.fetch_command("ffmpeg", "9.0.1"), others["ffmpeg"].command,
                        others["js"].command, []):
            self.assertFalse(deps.is_pip(command))

    def test_a_copy_that_wont_run_is_unusable_and_the_screen_can_fetch_it_again(self):
        for state, installed, expected in ((deps.OK, "9.0.1", False), (deps.OUTDATED, "9.0.1", True),
                                           (deps.MISSING, "", True), (deps.UNSUPPORTED, "2.2.0", True),
                                           (deps.UNKNOWN, "9.0.1", False), (deps.UNKNOWN, "", True)):
            with self.subTest(state=state, installed=installed):
                dep = deps.Dep("ffmpeg", "FFmpeg", True, state=state, installed=installed)
                self.assertIs(deps.unusable(dep), expected)
        # Tapewright's own FFmpeg that won't run, as check_ffmpeg reports it: no version, a fetch command.
        found = self.found()
        ffmpeg = found["ffmpeg"]
        ffmpeg.state, ffmpeg.command = deps.UNKNOWN, deps.fetch_command("ffmpeg", "9.0.1")
        self.assertEqual(deps.setup_keys(found), ["ffmpeg"])
        # Still "can't tell", which never brings the screen back by itself or warns before a conversion.
        self.assertIs(deps.setup_needed(found, {"setup_done": False, "js_runtime": "auto"}), False)
        self.assertEqual(deps.problems_for(found, "file"), [])
        ffmpeg.installed = "9.0.1"  # it runs, and only the online check failed
        self.assertEqual(deps.setup_keys(found), [])
        ffmpeg.installed, ffmpeg.command = "", deps._winget_command("winget", "Gyan.FFmpeg")
        self.assertEqual(deps.setup_keys(found), [])  # the Settings tab's to run, as for any winget copy

    def test_when_the_setup_screen_is_needed(self):
        fresh = {"setup_done": False, "js_runtime": "auto"}
        done = {"setup_done": True, "js_runtime": "auto"}
        off = {"setup_done": False, "js_runtime": "none"}
        ok, missing, behind, too_old = deps.OK, deps.MISSING, deps.OUTDATED, deps.UNSUPPORTED
        for ytdlp, ffmpeg, js, settings, needed in (
            (ok, ok, ok, fresh, False),
            (ok, missing, ok, done, True),  # a required tool, however long ago setup was finished
            (missing, ok, ok, done, True),
            (ok, ok, missing, fresh, True),  # the optional helper, only until setup is finished once
            (ok, ok, missing, done, False),
            (ok, ok, missing, off, False),  # and never when it is turned off in Settings
            (behind, ok, too_old, fresh, False),  # out of date is the Settings tab's business
            (behind, missing, ok, done, True),
        ):
            with self.subTest(ytdlp=ytdlp, ffmpeg=ffmpeg, js=js, settings=settings):
                self.assertIs(deps.setup_needed(self.found(ytdlp, ffmpeg, js), settings), needed)
        # Missing, but nothing here could install it: the screen would offer a button that can't help.
        self.assertIs(deps.setup_needed(self.found(ffmpeg=missing, commands=False), fresh), False)
        # Outside Windows nothing fetches FFmpeg, and an update for yt-dlp is no reason to bring back a screen
        # that can't install the helper that is missing.
        posix = self.found(ytdlp=behind, ffmpeg=missing)
        posix["ffmpeg"].command = []
        self.assertIs(deps.setup_needed(posix, done), False)
        posix["yt-dlp"].state = missing
        self.assertIs(deps.setup_needed(posix, done), True)
        defaults = config.Settings(Path("unused.json"))  # never loaded or saved
        self.assertIs(deps.setup_needed(self.found(ffmpeg=missing), defaults), True)

    def test_the_setup_screen_hears_every_event_the_app_sends(self):
        # Read from the source rather than a running window, so it holds on a Python without Tk too. A new
        # kind of event fails here until the screen does something with it.
        def tree(name):
            return ast.parse((ROOT / "tapewright" / name).read_text(encoding="utf-8"))

        calls = [node for node in ast.walk(tree("app.py")) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "_notify"]
        named = [node.args[0].value for node in calls if node.args and isinstance(node.args[0], ast.Constant)]
        self.assertEqual(len(named), len(calls), "every event App sends is named by a literal")
        sent = set(named)
        on_update = next(node for node in ast.walk(tree("setup_screen.py"))
                         if isinstance(node, ast.FunctionDef) and node.name == "on_update")
        handled = {node.comparators[0].value for node in ast.walk(on_update)
                   if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                   and node.left.id == "event" and isinstance(node.comparators[0], ast.Constant)}
        self.assertEqual(sent, {"start", "line", "done", "finished"})
        self.assertEqual(handled, sent)


class Release(unittest.TestCase):
    """Whether a newer Tapewright is out, as a copy at version 0.1.1 would see it."""

    NOW = datetime.datetime(2026, 9, 14, 12, 0, 0)

    def check(self, answer, cache=None, online=True):
        """(release, updates, the URLs asked), with GitHub answering answer: text, or an error to raise."""
        asked = []

        def fetch_text(url, timeout=20, headers=None):
            asked.append(url)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with mock.patch.object(deps, "__version__", "0.1.1"), \
                mock.patch.object(deps, "fetch_text", side_effect=fetch_text):
            release, updates = deps.check_release(cache if cache is not None else {}, online, now=self.NOW)
        return release, updates, asked

    def http_error(self, code, message):
        """An HTTPError like urlopen's, closed when the test ends, since an unclosed one warns."""
        error = urllib.error.HTTPError(deps.RELEASES_API, code, message, {}, io.BytesIO())
        self.addCleanup(error.close)
        return error

    def test_a_newer_release_is_offered_and_remembered(self):
        release, updates, asked = self.check('{"tag_name": "v0.2.0", "html_url": "https://example.com/"}')
        # The page is always Tapewright's own, never a URL taken from the answer.
        self.assertEqual(release, {"version": "0.2.0", "url": deps.RELEASES_PAGE})
        self.assertEqual(updates, {"tapewright": {"version": "0.2.0", "checked": "2026-09-14T12:00:00"}})
        self.assertEqual(asked, [deps.RELEASES_API])

    def test_the_same_or_an_older_release_is_not(self):
        for tag, version in (("v0.1.1", "0.1.1"), ("0.1.1", "0.1.1"), ("v0.1.0", "0.1.0")):
            release, updates, _ = self.check(f'{{"tag_name": "{tag}"}}')
            self.assertIsNone(release)
            self.assertEqual(updates["tapewright"]["version"], version)

    def test_garbage_or_no_answer_is_never_a_release_and_never_raises(self):
        rate_limited = self.http_error(403, "rate limit exceeded")
        failed = {"tapewright": {"version": "", "checked": "2026-09-14T12:00:00", "failed": True}}
        for answer in ("<html>busy</html>", "[]", "{}", '{"tag_name": null}', '{"tag_name": "nightly"}',
                       OSError("no network"), rate_limited):
            with self.subTest(answer=answer):
                self.assertEqual(self.check(answer), (None, failed, [deps.RELEASES_API]))
        # A failed check falls back to the last answer, however old, and carries it forward.
        cache = {"tapewright": {"version": "0.2.0", "checked": "2026-09-01T09:00:00"}}
        release, updates, _ = self.check(OSError("no network"), cache)
        self.assertEqual((release["version"], updates["tapewright"]["version"]), ("0.2.0", "0.2.0"))
        self.assertEqual(self.check("[]", {"tapewright": "junk"}), (None, failed, [deps.RELEASES_API]))

    def test_a_failure_is_asked_again_after_an_hour_not_a_day(self):
        failed = {"version": "0.2.0", "checked": "2026-09-14T11:30:00", "failed": True}
        newer = '{"tag_name": "v0.3.0"}'
        # Half an hour after a failure, GitHub isn't asked, and the last answer stands.
        release, updates, asked = self.check(newer, {"tapewright": failed})
        self.assertEqual((release["version"], updates, asked), ("0.2.0", {}, []))
        # An hour after it, GitHub is asked again, and an answer clears the mark.
        an_hour_ago = {"tapewright": dict(failed, checked="2026-09-14T11:00:00")}
        release, updates, asked = self.check(newer, an_hour_ago)
        self.assertEqual((release["version"], asked), ("0.3.0", [deps.RELEASES_API]))
        self.assertEqual(updates, {"tapewright": {"version": "0.3.0", "checked": "2026-09-14T12:00:00"}})
        # Another failure starts the hour again, still with the last answer.
        two_hours_ago = {"tapewright": dict(failed, checked="2026-09-14T10:00:00")}
        _, updates, _ = self.check(OSError("no network"), two_hours_ago)
        self.assertEqual(updates, {"tapewright": dict(failed, checked="2026-09-14T12:00:00")})
        # A clock put back is never fresh.
        later = {"tapewright": dict(failed, checked="2026-09-14T12:30:00")}
        self.assertEqual(self.check(newer, later)[2], [deps.RELEASES_API])

    def test_no_release_yet(self):
        not_found = self.http_error(404, "Not Found")
        release, updates, _ = self.check(not_found)
        self.assertIsNone(release)
        # Remembered like an answer, so a repository with no release isn't asked again all day.
        self.assertEqual(updates, {"tapewright": {"version": "", "checked": "2026-09-14T12:00:00"}})
        self.assertEqual(self.check(not_found, {"tapewright": updates["tapewright"]}), (None, {}, []))

    def test_offline_asks_nothing_and_uses_the_last_answer(self):
        self.assertEqual(self.check('{"tag_name": "v0.2.0"}', online=False), (None, {}, []))
        cache = {"tapewright": {"version": "0.2.0", "checked": "2026-08-01T09:00:00"}}
        release, updates, asked = self.check('{"tag_name": "v0.3.0"}', cache, online=False)
        self.assertEqual((release["version"], updates, asked), ("0.2.0", {}, []))

    def test_asked_at_most_once_a_day(self):
        newer = '{"tag_name": "v0.2.0"}'
        cache = {"tapewright": {"version": "0.1.1", "checked": "2026-09-14T08:00:00"}}
        self.assertEqual(self.check(newer, cache), (None, {}, []))
        cache["tapewright"]["checked"] = "2026-09-13T11:00:00"  # 25 hours ago
        release, updates, asked = self.check(newer, cache)
        self.assertEqual((release["version"], asked), ("0.2.0", [deps.RELEASES_API]))
        for checked in ("2026-09-15T08:00:00", "yesterday", None):  # a clock put back, or a damaged cache
            cache["tapewright"]["checked"] = checked
            self.assertEqual(self.check(newer, cache)[2], [deps.RELEASES_API], checked)


class Bundled(unittest.TestCase):
    def test_the_installers_python_keeps_its_children_to_itself(self):
        with tempfile.TemporaryDirectory() as folder:
            stray = {"PYTHONHOME": str(Path(folder) / "other"), "PYTHONPATH": str(Path(folder) / "lib")}
            with mock.patch.object(procs.sys, "executable", str(Path(folder) / "python.exe")), \
                    mock.patch.dict(os.environ, stray):
                os.environ.pop("PYTHONNOUSERSITE", None)
                self.assertFalse(procs.bundled())
                env = procs.child_env()
                # A Python someone installed themselves keeps its environment, as it always has.
                self.assertEqual({key: env[key] for key in stray}, stray)
                self.assertNotIn("PYTHONNOUSERSITE", env)

                (Path(folder) / procs.RUNTIME_MARKER).write_text("3.14.6\n", encoding="utf-8")
                self.assertTrue(procs.bundled())
                env = procs.child_env([folder])
                self.assertNotIn("PYTHONHOME", env)
                self.assertNotIn("PYTHONPATH", env)
                self.assertEqual(env["PYTHONNOUSERSITE"], "1")
                self.assertEqual((env["PYTHONUTF8"], env["PYTHONIOENCODING"], env["PYTHONUNBUFFERED"]),
                                 ("1", "utf-8", "1"))
                self.assertTrue(env["PATH"].startswith(folder))
        with mock.patch.object(procs.sys, "executable", ""):
            self.assertFalse(procs.bundled())


class Launch(unittest.TestCase):
    def test_an_old_python_is_explained(self):
        with mock.patch.object(sys, "version_info", (3, 9, 18, "final", 0)):
            reason, fix = launch.problem()
        self.assertIn("needs Python 3.10 or newer, and this copy of Python is 3.9", reason)
        self.assertIn("open Tapewright again", fix)

    def test_a_python_without_tkinter_is_explained(self):
        with mock.patch.dict(sys.modules, {"tkinter": None}):  # None in sys.modules makes the import fail
            reason, fix = launch.problem()
        self.assertIn("no Tkinter", reason)
        self.assertIn("Tkinter", fix)

    @unittest.skipIf(tkinter is None, "a Python built without Tk")
    def test_a_usable_python_starts(self):
        self.assertIsNone(launch.problem())

    def test_the_explanation_reaches_a_console_and_on_windows_a_box(self):
        expected = "Tapewright can't start, because it is a test.\n\nNothing to fix."
        # ctypes is faked for both branches, so no real, modal message box can ever open here.
        for os_name, boxes, stderr in (("nt", 1, mock.Mock()), ("posix", 0, mock.Mock()), ("nt", 1, None)):
            fake_ctypes, fake_os = mock.Mock(), mock.Mock()
            fake_os.name = os_name
            with mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}), \
                    mock.patch.object(launch, "os", fake_os), mock.patch.object(sys, "stderr", stderr):
                launch.explain("it is a test.", "Nothing to fix.")
            box = fake_ctypes.windll.user32.MessageBoxW
            self.assertEqual(box.call_count, boxes)
            if boxes:
                self.assertTrue(box.call_args.args[1].startswith(expected))
            if stderr is not None:
                written = "".join(call.args[0] for call in stderr.write.call_args_list)
                self.assertTrue(written.startswith(expected))

    def test_main_explains_and_never_opens_the_window(self):
        window = mock.Mock()
        window.main.return_value = 0
        with mock.patch.dict(sys.modules, {"tapewright.app": window}):
            with mock.patch.object(launch, "problem", return_value=("a reason.", "a fix.")), \
                    mock.patch.object(launch, "explain") as explain:
                self.assertEqual(launch.main(), 1)
            explain.assert_called_once_with("a reason.", "a fix.")
            window.main.assert_not_called()
            with mock.patch.object(launch, "problem", return_value=None):
                self.assertEqual(launch.main(), 0)
            window.main.assert_called_once_with()

    def test_every_way_in_goes_through_launch(self):
        for path in (ROOT / "Tapewright.pyw", ROOT / "tapewright" / "__main__.py"):
            imports = [(node.module, [alias.name for alias in node.names])
                       for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                       if isinstance(node, ast.ImportFrom)]
            self.assertIn(("tapewright.launch", ["main"]), imports, path.name)
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('tapewright = "tapewright.launch:main"', pyproject)

    def test_a_window_that_cannot_open_gets_the_fix_for_its_cause(self):
        with mock.patch.dict(os.environ, {"TCL_LIBRARY": "", "TK_LIBRARY": ""}):
            no_display = "no display name and no $DISPLAY environment variable"
            self.assertIn("desktop", launch.tk_failure_fix(no_display))
            self.assertIn("desktop", launch.tk_failure_fix('couldn\'t connect to display ":0"'))
            self.assertIn("Reinstalling Python", launch.tk_failure_fix("Can't find a usable init.tcl"))
        with mock.patch.dict(os.environ, {"TCL_LIBRARY": r"C:\ActiveTcl\lib\tcl8.6", "TK_LIBRARY": ""}):
            fix = launch.tk_failure_fix("Can't find a usable init.tcl")
            self.assertIn("TCL_LIBRARY", fix)
            self.assertNotIn("TK_LIBRARY", fix)

    def test_the_launchers_parse_on_any_python_3(self):
        # They run before the version check, so a newer Python's syntax here would fail under
        # pythonw.exe, which shows nothing at all. ast.parse on a new Python accepts syntax an old
        # one rejects, so this catches only the constructs most likely to slip in, not every one.
        newer = (ast.JoinedStr, ast.NamedExpr, ast.AnnAssign) + tuple(
            getattr(ast, name) for name in ("Match", "TryStar", "TypeAlias") if hasattr(ast, name))
        for path in (ROOT / "Tapewright.pyw", ROOT / "tapewright" / "__init__.py",
                     ROOT / "tapewright" / "__main__.py", ROOT / "tapewright" / "launch.py"):
            source = path.read_text(encoding="utf-8")
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.NUMBER:  # 1_000 is Python 3.6
                    self.assertNotIn("_", token.string, f"{path.name}:{token.start[0]}")
            for node in ast.walk(ast.parse(source)):
                where = f"{path.name}:{getattr(node, 'lineno', '?')}"
                self.assertNotIsInstance(node, newer, where)
                if isinstance(node, ast.arguments):
                    self.assertEqual(node.posonlyargs, [], where)
                    # Python 3.9 evaluates an annotation such as tuple[str, str] | None at import.
                    every = node.args + node.kwonlyargs + [a for a in (node.vararg, node.kwarg) if a]
                    self.assertEqual([a.arg for a in every if a.annotation is not None], [], where)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.assertIsNone(node.returns, where)
                if isinstance(node, ast.With):  # with (a as x, b as y): is Python 3.9
                    self.assertFalse(re.match(r"with\s*\(", ast.get_source_segment(source, node)), where)


@unittest.skipIf(tkinter is None, "a Python built without Tk")
class Window(unittest.TestCase):
    """The window half. The real window is built withdrawn, so nothing appears on screen.

    Skipped without tkinter. With no display to open a window on (not Windows, and no DISPLAY), only
    the tests that open one skip, so a Python 3.10 run still imports the whole package. Anywhere
    else, a Tk that can't open a window is a failure.
    """

    @classmethod
    def setUpClass(cls):
        # Imported here rather than at the top, so an app that can't import fails only this class.
        from tapewright import app
        cls.tk, cls.app = tkinter, app
        # Kept before new_app replaces it for each test, for the tests that run the real one with fakes.
        cls.real_check_dependencies = staticmethod(app.App.check_dependencies)

    def test_every_module_imports(self):
        for path in sorted((ROOT / "tapewright").glob("*.py")):
            if path.stem not in ("__init__", "__main__"):
                importlib.import_module(f"tapewright.{path.stem}")

    def test_a_tk_that_cannot_open_a_window_is_explained(self):
        error = self.tk.TclError("no display name and no $DISPLAY environment variable")
        # Of tkinter, only Tk is patched: the except clause reads tk.TclError, which must stay real.
        with mock.patch.object(self.app.tk, "Tk", side_effect=error), \
                mock.patch.object(self.app, "_enable_dpi_awareness"), \
                mock.patch.object(self.app, "_set_app_user_model_id"), \
                mock.patch.object(self.app.config, "Settings"), \
                mock.patch.object(self.app.launch, "explain") as explain:
            self.assertEqual(self.app.main(), 1)
        reason, fix = explain.call_args.args
        self.assertIn("no display name", reason)
        self.assertIn("desktop", fix)

    def new_app(self, settings=None, scale=None, setup_done=False):
        """The real App on a withdrawn root, before any check has come back. No check ever runs.

        scale stands in for Windows' display scaling, 1.25 for 125%. It is set before any font or size exists,
        as a DPI-aware Tapewright would find it.
        """
        try:
            root = self.tk.Tk()
        except self.tk.TclError as e:
            if os.name == "nt" or os.environ.get("DISPLAY"):
                raise  # a broken Tk where a window can open is a failure, not a reason to skip
            self.skipTest(f"no display to open a window on: {e}")
        root.withdraw()
        if scale:
            root.tk.call("tk", "scaling", scale * 96 / 72)  # Tk counts pixels per point, at 72 points an inch
        self.addCleanup(root.destroy)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # Commands built from here on name a tools folder of this test's own: App tidies up the folder a
        # cancelled download names, and that must never be Tapewright's real one.
        tools = mock.patch.dict(os.environ, {deps.TOOLS_DIR_ENV: str(Path(folder.name) / "tools")})
        tools.start()
        self.addCleanup(tools.stop)
        no_checks = mock.patch.object(self.app.App, "check_dependencies")  # no tools, no network
        no_checks.start()
        self.addCleanup(no_checks.stop)
        settings = settings or config.Settings(Path(folder.name) / "settings.json")
        if setup_done:
            settings["setup_done"] = True
        return self.app.App(root, settings)

    def open_window(self):
        """The real App on a withdrawn root, with yt-dlp and FFmpeg out of date and no checks run."""
        app = self.new_app()
        ytdlp = deps.Dep("yt-dlp", "yt-dlp", True, state=deps.OUTDATED, installed="2026.8.19",
                         latest="2026.9.10", action="Update",
                         command=deps.ytdlp_install_command("2026.9.10", True))
        ffmpeg = deps.Dep("ffmpeg", "FFmpeg", True, state=deps.OUTDATED, installed="9.0.1", latest="9.1",
                          action="Update", command=deps._winget_command("winget", "Gyan.FFmpeg"))
        js = deps.Dep("js", "deno", False, state=deps.OK, installed="2.9.6")
        app._check_finished({"yt-dlp": ytdlp, "ffmpeg": ffmpeg, "js": js}, {}, False)
        app.root.update_idletasks()
        return app

    @staticmethod
    def helpers(ytdlp=deps.OK, ffmpeg=deps.OK, js=deps.OK):
        """The helpers as a check on Windows reports them, each problem with the command that fixes it."""
        def dep(key, name, state, command):
            fixable = state in deps.PROBLEMS
            installed = "" if state == deps.MISSING else "1.0"
            return deps.Dep(key, name, key != "js", state=state, installed=installed,
                            action="Install" if fixable else "", command=command if fixable else [])

        return {"yt-dlp": dep("yt-dlp", "yt-dlp", ytdlp, deps.ytdlp_install_command("2026.9.10", True)),
                "ffmpeg": dep("ffmpeg", "FFmpeg", ffmpeg, deps.fetch_command("ffmpeg", "9.0.1")),
                "js": dep("js", "deno", js, deps.fetch_command("deno", "2.9.6"))}

    @staticmethod
    def pump(app):
        """Run what workers posted, in order, as the window's timer would."""
        while not app._events.empty():
            fn, args = app._events.get_nowait()
            fn(*args)

    @staticmethod
    def text(widget, option="text"):
        return str(widget.cget(option))  # ttk can hand back a Tcl object rather than a str

    def test_the_window_names_tools_by_role_and_explains_each_update(self):
        app = self.open_window()
        self.assertEqual(app.settings_tab.rows["yt-dlp"]["name"].cget("text"), "Downloader (yt-dlp)")
        self.assertIn("Converter (FFmpeg) is out of date", app.mp3_tab.banner.label.cget("text"))

        asked = []
        app.confirm = lambda title, message: asked.append(message) or False
        self.assertFalse(app.run_updates(["yt-dlp", "ffmpeg"]))
        self.assertIn("pypi.org", asked[0])
        self.assertIn("terms", asked[0])
        self.assertNotIn("--", asked[0])

    def test_the_update_log_says_how_a_batch_ended(self):
        app = self.open_window()
        ytdlp, ffmpeg = app.deps["yt-dlp"], app.deps["ffmpeg"]

        # As if winget had just finished: nothing newer yet, and the batch is over.
        app.updating, app._update_queue, app._update_cancelled = "ffmpeg", [], False
        app._update_finished(ffmpeg, deps.WINGET_UPDATE_NOT_APPLICABLE, "")
        lines = app.settings_tab.logview.contents().splitlines()
        failure = deps.update_failure_text(ffmpeg, deps.WINGET_UPDATE_NOT_APPLICABLE)
        self.assertEqual(lines[-2], "Converter (FFmpeg): " + failure)
        self.assertEqual(lines[-1], help_content.UPDATES_FINISHED + ".")
        self.assertIsNone(app.updating)
        app.updating = "yt-dlp"
        app._update_finished(ytdlp, None, "cancelled")
        self.assertIn("update cancelled", app.settings_tab.logview.contents().splitlines()[-1])

        # Cancel lands just after yt-dlp finished, with FFmpeg still queued: the log says FFmpeg was
        # skipped, and the batch must not end with the line Help says means it all finished.
        app.updating, app._update_runner = "yt-dlp", mock.Mock()
        app._update_queue, app._update_cancelled = [ffmpeg], False
        self.assertIs(app.cancel_update(), True)
        self.assertEqual(app._update_queue, [])
        self.assertEqual(app.settings_tab.logview.contents().splitlines()[-1],
                         "Update cancelled. Not updated: the Converter (FFmpeg).")
        app._update_finished(ytdlp, 0, "")
        self.assertEqual(app.settings_tab.logview.contents().splitlines()[-1], "Downloader (yt-dlp): done.")
        self.assertIs(app.cancel_update(), False)  # nothing is running now
        # And a new batch starts clean, so its end is announced again.
        with mock.patch.object(app, "_next_update"):
            self.assertTrue(app.run_updates(["yt-dlp"], confirm=False))
        self.assertFalse(app._update_cancelled)

    def test_setup_shows_at_once_and_goes_quietly_when_nothing_is_missing(self):
        from tapewright import setup_screen
        app = self.new_app()
        screen = app.setup_screen
        # In place of the tabs, not over them, so nothing out of sight can take the focus.
        self.assertTrue(app.setup_shown)
        self.assertEqual((screen.winfo_manager(), app.notebook.winfo_manager()), ("pack", ""))
        self.assertEqual(self.text(screen.status), setup_screen.LOOKING)
        self.assertEqual(screen.shown_buttons, ("later",))

        app._check_finished(self.helpers(ytdlp=deps.OUTDATED), {}, True)  # out of date is Settings' business
        self.assertEqual((app.setup_shown, screen.winfo_manager(), app.notebook.winfo_manager()),
                         (False, "", "pack"))
        saved = config.Settings(app.settings.path)
        saved.load()
        self.assertIs(saved["setup_done"], True)
        self.assertEqual(app.settings_tab.logview.contents(), "")
        self.assertFalse(self.new_app(saved).setup_shown)  # and once done, it isn't shown at all

    def test_setup_stays_while_ffmpeg_is_missing(self):
        from tapewright import setup_screen
        app = self.new_app()
        screen = app.setup_screen
        app._check_finished(self.helpers(ffmpeg=deps.MISSING), {}, True)
        self.assertTrue(app.setup_shown)
        self.assertIs(app.settings["setup_done"], False)
        self.assertEqual(screen.shown_buttons, ("install", "later"))
        self.assertEqual(self.text(screen.buttons["install"]), "Install the missing ones")
        ffmpeg = screen.rows["ffmpeg"]
        self.assertEqual(self.text(ffmpeg["name"]), "Converter (FFmpeg)")
        self.assertEqual(self.text(ffmpeg["about"]), deps.ABOUT["ffmpeg"])
        self.assertEqual(self.text(ffmpeg["state"]), "Needs installing")
        self.assertEqual(self.text(ffmpeg["hint"]), "about 110 MB from github.com")
        self.assertEqual(self.text(screen.rows["yt-dlp"]["state"]), setup_screen.STATUS_WORDS[deps.OK])
        self.assertEqual(ffmpeg["bar"].winfo_manager(), "")  # a bar only on its turn
        app.settings["js_runtime"] = "none"
        screen.refresh()
        self.assertEqual((screen.rows["js"]["frame"].winfo_manager(), ffmpeg["frame"].winfo_manager()),
                         ("", "grid"))

        # Escape is [Not now]. Settings can still bring the screen back on purpose.
        screen._escape(None)
        self.assertFalse(app.setup_shown)
        app.settings_tab.setup_button.invoke()
        self.assertTrue(app.setup_shown)

        # Once setup is done, only a required helper going missing brings it back by itself.
        again = self.new_app(setup_done=True)
        self.assertFalse(again.setup_shown)
        again._check_finished(self.helpers(ffmpeg=deps.MISSING), {}, True)
        self.assertTrue(again.setup_shown)

    def test_a_setup_screen_asked_for_while_the_first_check_runs_stays(self):
        # Opened with either Run setup again, on Settings or in Help, once setup has been done, and the first
        # check then finds nothing the screen installs: only a deno that deno's own updater updates.
        found = self.helpers(js=deps.OUTDATED)
        found["js"].command = [r"C:\Users\someone\.deno\bin\deno.exe", "upgrade"]
        for route in ("settings", "help"):
            with self.subTest(route=route):
                app = self.new_app(setup_done=True)
                app.checking = True  # the check at startup is still running
                if route == "settings":
                    app.settings_tab.setup_button.invoke()
                else:
                    app.help_tab.run_setup()
                self.assertTrue(app.setup_shown)
                app._check_finished(found, {}, True)
                self.assertTrue(app.setup_shown)
                app.hide_setup()
                self.assertIs(app._setup_requested, False)

        # On a first launch it opened by itself, was put off, and was asked for again before the check came
        # back. It stays, and setup is still finished for good, since nothing is missing.
        app = self.new_app()
        app.checking = True
        app.setup_screen.buttons["later"].invoke()
        self.assertTrue(app.request_setup())
        app._check_finished(self.helpers(), {}, True)
        self.assertTrue(app.setup_shown)
        saved = config.Settings(app.settings.path)
        saved.load()
        self.assertIs(saved["setup_done"], True)

    def test_not_now_holds_for_the_rest_of_the_session(self):
        app = self.new_app()
        app.setup_screen.buttons["later"].invoke()  # while the first check is still looking
        app._check_finished(self.helpers(ffmpeg=deps.MISSING), {}, True)
        self.assertFalse(app.setup_shown)
        self.assertIs(app.settings["setup_done"], False)

    def test_install_them_now_installs_what_setup_keys_names(self):
        app = self.new_app()
        screen = app.setup_screen
        everything = self.helpers(deps.MISSING, deps.MISSING, deps.MISSING)
        app._check_finished(everything, {}, True)
        self.assertEqual(self.text(screen.buttons["install"]), "Install them now")
        with mock.patch.object(app, "run_updates", return_value=True) as run_updates:
            screen.buttons["install"].invoke()
            run_updates.assert_called_once_with(["yt-dlp", "ffmpeg", "js"], confirm=False)

            # After an offline check, the versions are looked up online before anything is installed.
            app._check_finished(everything, {}, False)
            run_updates.reset_mock()
            screen.buttons["install"].invoke()
            run_updates.assert_not_called()
            check = self.app.App.check_dependencies
            self.assertIs(check.call_args.kwargs["online"], True)
            app._check_finished(everything, {}, True)
            check.call_args.kwargs["then"]()
            run_updates.assert_called_once_with(deps.setup_keys(everything), confirm=False)

    def test_update_events_drive_a_setup_row(self):
        from tapewright import setup_screen
        app = self.new_app()
        app._check_finished(self.helpers(ffmpeg=deps.MISSING, js=deps.MISSING), {}, True)
        screen, ffmpeg, row = app.setup_screen, app.deps["ffmpeg"], app.setup_screen.rows["ffmpeg"]
        app._notify("start", ffmpeg, 1, 2)
        self.assertEqual(self.text(screen.status), "Step 1 of 2: Installing the Converter (FFmpeg)…")
        self.assertEqual(screen.shown_buttons, ("hide", "cancel"))  # Hide first: Space and Enter never cancel
        self.assertEqual((row["bar"].winfo_manager(), self.text(row["bar"], "mode")),
                         ("grid", "indeterminate"))
        self.assertEqual(screen.rows["js"]["bar"].winfo_manager(), "")

        app._notify("line", ffmpeg, "STEP Downloading…")
        self.assertEqual(self.text(row["state"]), "Downloading…")
        app._notify("line", ffmpeg, f"PROGRESS {48 * setup_screen.MB} 111253802")
        self.assertEqual(self.text(row["bar"], "mode"), "determinate")
        self.assertEqual(float(row["bar"].cget("value")), 48 * setup_screen.MB)
        self.assertEqual(self.text(row["amount"]), "48 of 106 MB")
        app._notify("line", ffmpeg, "https://github.com/GyanD/codexffmpeg/releases/download/9.0.1/x.zip")
        self.assertEqual(self.text(row["state"]), "Downloading…")  # free text is for the log alone

        app._notify("done", ffmpeg, True, "done.")
        self.assertEqual((self.text(row["state"]), row["bar"].winfo_manager()), (setup_screen.INSTALLED, ""))
        self.assertEqual(setup_screen.megabytes(3 * setup_screen.MB, 0), "3 MB")

    class Inline:
        """In place of threading.Thread: runs a worker at once, leaving what it posted queued in order."""

        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    def test_every_way_a_download_ends_reaches_the_log_and_the_setup_screen(self):
        import test_fetch  # for its real runs of fetch.py, made with fake downloads
        from tapewright import setup_screen
        with tempfile.TemporaryDirectory() as folder:
            runs = test_fetch.outcomes(folder)
        # And two endings fetch.py can't print for itself: killed from outside, and cancelled.
        runs["killed from outside"] = (1, ["STEP Downloading…", "PROGRESS 1048576 111253802"])
        runs["cancelled"] = (None, ["STEP Downloading…", "PROGRESS 1048576 111253802"])
        app = self.new_app()
        screen, log = app.setup_screen, app.settings_tab.logview
        missing = self.helpers(ffmpeg=deps.MISSING)
        playing = {}

        class Runner:  # plays a recorded run back, passing on its lines as procs.Runner would
            def run(self, command, on_line):
                for line in playing["lines"]:
                    on_line(line)
                if playing["code"] is None:
                    raise procs.Cancelled()
                return playing["code"]

        with mock.patch.object(self.app.procs, "Runner", Runner), \
                mock.patch.object(self.app, "threading", mock.Mock(Thread=self.Inline)):
            for name, (code, lines) in runs.items():
                with self.subTest(name):
                    app._check_finished(missing, {}, True)
                    ffmpeg, before = app.deps["ffmpeg"], len(log.contents().splitlines())
                    playing.update(code=code, lines=lines)
                    self.assertTrue(app.run_updates(["ffmpeg"], confirm=False))
                    self.pump(app)

                    read = [deps.parse_fetch_line(line) for line in lines]
                    errors = [result[1] for result in read if result and result[0] == "error"]
                    if code is None:
                        message = deps.cancel_text(ffmpeg)
                    elif code == 0:
                        message = "done."
                    else:  # the sentence the fetcher ended with, and its exit code only when there is none
                        message = errors[-1] if errors else (
                            f"the download stopped with code {code}; its output is above.")
                    # The log drops PROGRESS, shows a STEP as its sentence, and the rest as they came.
                    shown = [result[1] if result and result[0] == "step" else line
                             for line, result in zip(lines, read)
                             if line.strip() and not (result and result[0] == "progress")]
                    expected = ["Install: Converter (FFmpeg)", "$ " + procs.format_command(ffmpeg.command),
                                *shown, f"Converter (FFmpeg): {message}"]
                    if code is not None:
                        expected.append(help_content.UPDATES_FINISHED + ".")
                    self.assertEqual(log.contents().splitlines()[before:], expected)

                    # The check after the batch, which the setup screen waits for before it judges anything.
                    app._check_finished(self.helpers() if code == 0 else missing, {}, False)
                    state = screen.rows["ffmpeg"]["state"]
                    if code == 0:
                        self.assertEqual((self.text(screen.status), screen.shown_buttons),
                                         ("All set! Tapewright is ready.", ("start",)))
                    elif code is None:
                        self.assertEqual((self.text(state), screen.shown_buttons),
                                         (message[:1].upper() + message[1:], ("install", "later")))
                    else:
                        failure = errors[-1] if errors else setup_screen.DIDNT_INSTALL
                        row = (self.text(state), self.text(state, "foreground"), screen.shown_buttons)
                        self.assertEqual(row, (failure, theme.C["error"], ("retry", "help", "later")))

    def test_the_setup_intro_says_only_what_is_true_for_the_rows_listed(self):
        from tapewright import setup_screen
        intro, keys = setup_screen.intro_text, list(setup_screen.KEYS)
        everything = {"yt-dlp": "pip", "ffmpeg": "fetch", "js": "fetch"}
        self.assertEqual(intro(keys, everything, True), (
            "Tapewright needs three free helper programs. The Converter and the YouTube helper are "
            "downloaded from their official releases on github.com, checked, and kept in Tapewright's "
            "own folder. The Downloader is added to the Python that runs Tapewright. You don't need an "
            "administrator password."))
        # With the YouTube helper turned off, two rows are listed, and only the Converter is downloaded.
        self.assertEqual(intro(keys[:2], everything, True), (
            "Tapewright needs two free helper programs. The Converter is downloaded from its official "
            "releases on github.com, checked, and kept in Tapewright's own folder. The Downloader is added "
            "to the Python that runs Tapewright. You don't need an administrator password."))
        # Outside Windows nothing is downloaded, and nothing is said about a password.
        self.assertEqual(intro(keys, everything, False),
                         "Tapewright needs three free helper programs. The Downloader is added to the "
                         "Python that runs Tapewright.")
        # A helper this screen doesn't install gets no sentence.
        self.assertEqual(intro(keys, {}, True),
                         "Tapewright needs three free helper programs. You don't need an administrator "
                         "password.")
        # pip adds yt-dlp to a Python other programs may share, and a helper stopped while pip installs it
        # may be half-installed, as deps.cancel_text says. Neither the page nor its Help topic may promise
        # otherwise.
        topic = next(t for t in help_content.TOPICS if t["title"] == "Setting up Tapewright")
        help_text = " ".join(Help.texts(topic))
        for text in [help_text] + [intro(keys, plan, windows)
                                   for plan, windows in itertools.product((everything, {}), (True, False))]:
            self.assertNotIn("Nothing else", text)
        pip = deps.Dep("yt-dlp", "yt-dlp", True, command=deps.ytdlp_install_command("2026.9.10", True))
        self.assertIn("half-installed", deps.cancel_text(pip))
        self.assertIn("half-installed", help_text)
        self.assertIn("added to the Python that runs Tapewright", help_text)

    def test_a_helper_only_the_settings_tab_updates_is_left_to_it(self):
        from tapewright import setup_screen
        app = self.new_app()
        screen = app.setup_screen
        ffmpeg, js = screen.rows["ffmpeg"], screen.rows["js"]
        winget = deps._winget_command("winget", "Gyan.FFmpeg")
        found = self.helpers(ffmpeg=deps.OUTDATED, js=deps.MISSING)
        found["ffmpeg"].command = winget
        app._check_finished(found, {}, True)
        self.assertTrue(app.setup_shown)  # deno is missing, and setup was never finished
        self.assertEqual((self.text(ffmpeg["state"]), self.text(ffmpeg["hint"])),
                         ("Needs an update", setup_screen.SETTINGS_HINT))
        self.assertEqual(self.text(js["hint"]), "about 45 MB from github.com")
        # The intro names only the helper this screen downloads, never the one winget updates.
        only_deno = setup_screen.intro_text(list(setup_screen.KEYS), {"js": "fetch"}, deps.WINDOWS)
        self.assertEqual(self.text(screen.intro), only_deno)
        self.assertNotIn("Converter", only_deno)
        with mock.patch.object(app, "run_updates", return_value=True) as run_updates:
            screen.buttons["install"].invoke()
        run_updates.assert_called_once_with(["js"], confirm=False)

        # deno installs, and the Converter that only winget updates doesn't hold the page at "ready".
        deno = app.deps["js"]
        for event in (("start", deno, 1, 1), ("done", deno, True, "done."), ("finished", None, False)):
            app._notify(*event)
        after = self.helpers(ffmpeg=deps.OUTDATED)
        after["ffmpeg"].command = winget
        after["js"].path = str(deps.own_copy("deno"))  # where the fetcher puts it
        app._check_finished(after, {}, False)
        self.assertEqual((self.text(screen.status), screen.shown_buttons),
                         ("All set! Tapewright is ready.", ("start",)))
        self.assertEqual(self.text(screen.intro), only_deno)  # unchanged under a setup that just finished

        # A deno too old for yt-dlp that only deno's own updater updates is never "will be replaced".
        older = self.helpers(js=deps.UNSUPPORTED)
        older["js"].command = [r"C:\Users\someone\.deno\bin\deno.exe", "upgrade"]
        app._check_finished(older, {}, False)
        self.assertEqual((self.text(js["state"]), self.text(js["hint"]), screen.shown_buttons),
                         (setup_screen.TOO_OLD, setup_screen.SETTINGS_HINT, ("start",)))

    def test_start_after_almost_ready_finishes_setup(self):
        app = self.new_app()
        screen = app.setup_screen
        app._check_finished(self.helpers(ffmpeg=deps.MISSING, js=deps.MISSING), {}, True)
        ffmpeg, deno = app.deps["ffmpeg"], app.deps["js"]
        blocked = "Windows stopped deno from running. This computer may only allow approved programs."
        for event in (("start", ffmpeg, 1, 2), ("done", ffmpeg, True, "done."), ("start", deno, 2, 2),
                      ("line", deno, f"ERROR {blocked}"), ("done", deno, False, blocked),
                      ("finished", None, False)):
            app._notify(*event)
        app._check_finished(self.helpers(js=deps.MISSING), {}, False)
        self.assertEqual((self.text(screen.status), screen.shown_buttons),
                         ("Almost ready.", ("start", "retry")))
        screen.buttons["start"].invoke()
        self.assertFalse(app.setup_shown)
        saved = config.Settings(app.settings.path)
        saved.load()
        self.assertIs(saved["setup_done"], True)
        # So a deno that fails the same way every time doesn't bring the page back at each launch, while
        # FFmpeg going missing still would.
        self.assertIs(deps.setup_needed(self.helpers(js=deps.MISSING), saved), False)
        self.assertIs(deps.setup_needed(self.helpers(ffmpeg=deps.MISSING), saved), True)

    def test_a_batch_where_only_the_downloader_failed_has_failed(self):
        app = self.new_app()
        screen = app.setup_screen
        missing = self.helpers(ytdlp=deps.MISSING)
        app._check_finished(missing, {}, True)
        ytdlp = app.deps["yt-dlp"]
        for event in (("start", ytdlp, 1, 1), ("done", ytdlp, False, deps.update_failure_text(ytdlp, 1)),
                      ("finished", None, False)):
            app._notify(*event)
        app._check_finished(missing, {}, False)
        status = (self.text(screen.status), self.text(screen.status, "foreground"), screen.shown_buttons)
        self.assertEqual(status, ("Some helpers didn't install.", theme.C["error"],
                                  ("retry", "help", "later")))
        # Neither way off the page finishes a setup that failed, not even Start called with its button gone.
        screen.start_using()
        self.assertTrue(app.request_setup())
        screen.buttons["later"].invoke()
        self.assertFalse(app.setup_shown)
        saved = config.Settings(app.settings.path)
        saved.load()
        self.assertEqual((app.settings["setup_done"], saved["setup_done"]), (False, False))

    def test_not_now_finishes_setup_only_once_the_youtube_helper_really_failed(self):
        app = self.new_app()
        screen = app.setup_screen
        missing = self.helpers(js=deps.MISSING)
        app._check_finished(missing, {}, True)
        screen.buttons["later"].invoke()  # nothing tried yet: put off for this session, and no more
        self.assertIs(app.settings["setup_done"], False)
        self.assertTrue(app.request_setup())
        deno = app.deps["js"]
        damaged = "The download was damaged on the way, so it was thrown away. Try again."
        for event in (("start", deno, 1, 1), ("line", deno, f"ERROR {damaged}"),
                      ("done", deno, False, damaged), ("finished", None, False)):
            app._notify(*event)
        app._check_finished(missing, {}, False)
        self.assertEqual(screen.shown_buttons, ("start", "retry"))
        # Tried again and cancelled: its row no longer says it failed, but it did, in this session.
        for event in (("start", deno, 1, 1), ("done", deno, False, deps.cancel_text(deno)),
                      ("finished", None, True)):
            app._notify(*event)
        app._check_finished(missing, {}, False)
        self.assertEqual(screen.shown_buttons, ("install", "later"))
        screen.buttons["later"].invoke()
        saved = config.Settings(app.settings.path)
        saved.load()
        self.assertIs(saved["setup_done"], True)

    def test_an_install_the_check_still_cannot_use_says_why(self):
        from tapewright import setup_screen
        app = self.new_app()
        screen, row = app.setup_screen, app.setup_screen.rows["ffmpeg"]
        app._check_finished(self.helpers(ffmpeg=deps.MISSING), {}, True)
        ffprobe = r"ffmpeg is at C:\tools\ffmpeg.exe, but ffprobe (part of every FFmpeg build) wasn't found."
        output = "Tapewright's own copy of FFmpeg won't run: " + " ".join(["it printed a long line"] * 20)
        # The last is a new copy stopped from starting in its final folder, as check_ffmpeg reports one:
        # "can't tell" rather than missing, with no version and a command that fetches it again.
        for state, detail, shown in ((deps.MISSING, ffprobe, ffprobe),
                                     (deps.MISSING, "", setup_screen.DIDNT_INSTALL),
                                     (deps.MISSING, output, setup_screen.shorten(output)),
                                     (deps.UNKNOWN, output, setup_screen.shorten(output))):
            with self.subTest(state=state, detail=detail[:30]):
                ffmpeg = app.deps["ffmpeg"]
                for event in (("start", ffmpeg, 1, 1), ("done", ffmpeg, True, "done."),
                              ("finished", None, False)):
                    app._notify(*event)
                found = self.helpers(ffmpeg=deps.MISSING)
                # An install that exited 0, yet the check can't use it.
                found["ffmpeg"].state, found["ffmpeg"].detail = state, detail
                app._check_finished(found, {}, False)
                drawn = (self.text(row["state"]), self.text(row["state"], "foreground"), screen.shown_buttons)
                self.assertEqual(drawn, (shown, theme.C["error"], ("retry", "help", "later")))
                # What Try again would download, said as on any row the page installs.
                self.assertEqual(self.text(row["hint"]), "about 110 MB from github.com")
                self.assertIs(app.settings["setup_done"], False)
        # Too long for a row: cut there between words, and whole in the log, which Show details mirrors.
        cut = setup_screen.shorten(output)
        self.assertTrue(cut.endswith(setup_screen.CUT), cut)
        self.assertLessEqual(len(cut), setup_screen.DETAIL_LIMIT + len(setup_screen.CUT))
        self.assertTrue(output.startswith(cut[:-len(setup_screen.CUT)]))
        self.assertIn(output, app.settings_tab.logview.contents())
        self.assertIn(output, screen.logview.contents())

    def test_a_failure_is_forgotten_once_a_check_finds_that_helper_working(self):
        app = self.new_app()
        screen, row = app.setup_screen, app.setup_screen.rows["ffmpeg"]
        missing = self.helpers(ffmpeg=deps.MISSING)
        app._check_finished(missing, {}, True)
        ffmpeg = app.deps["ffmpeg"]
        blocked = "Windows stopped FFmpeg from running. This computer may only allow approved programs."
        for event in (("start", ffmpeg, 1, 1), ("line", ffmpeg, f"ERROR {blocked}"),
                      ("done", ffmpeg, False, blocked), ("finished", None, False)):
            app._notify(*event)
        app._check_finished(missing, {}, False)
        self.assertEqual(self.text(row["state"]), blocked)
        app._check_finished(self.helpers(), {}, False)  # put in place by hand, as Help describes
        self.assertEqual(screen.shown_buttons, ("start",))
        # Missing again later: that old sentence is no news about it.
        app._check_finished(missing, {}, False)
        self.assertEqual((self.text(row["state"]), screen.shown_buttons),
                         ("Needs installing", ("install", "later")))

    def test_a_helper_that_runs_is_in_place_even_when_its_updates_could_not_be_checked(self):
        from tapewright import setup_screen
        app = self.new_app()
        screen, row = app.setup_screen, app.setup_screen.rows["ffmpeg"]

        def unchecked(found, *keys):
            """Offline with nothing cached: each of keys runs, but nobody knows whether it is current."""
            for key in keys:
                found[key].state, found[key].installed, found[key].command = deps.UNKNOWN, "1.0", []
                found[key].detail = "Couldn't check for updates (online checks were skipped)."
            return found

        before = unchecked(self.helpers(ffmpeg=deps.MISSING, js=deps.MISSING), "yt-dlp")
        app._check_finished(before, {}, True)
        # One helper is in place already, so the button doesn't offer to install all three.
        self.assertEqual((screen.shown_buttons, self.text(screen.buttons["install"])),
                         (("install", "later"), "Install the missing ones"))
        ffmpeg, deno = app.deps["ffmpeg"], app.deps["js"]
        for event in (("start", ffmpeg, 1, 2), ("done", ffmpeg, True, "done."), ("start", deno, 2, 2),
                      ("done", deno, True, "done."), ("finished", None, False)):
            app._notify(*event)
        after = unchecked(self.helpers(), "yt-dlp", "ffmpeg", "js")
        # The check after the batch, which can't reach the internet either.
        app._check_finished(after, {}, False)
        self.assertEqual((self.text(screen.status), screen.shown_buttons, self.text(row["state"])),
                         ("All set! Tapewright is ready.", ("start",),
                          setup_screen.STATUS_WORDS[deps.UNKNOWN]))
        # Nor did either install fail at any time, which is what Not now reads for the YouTube helper.
        self.assertEqual(screen.failed_once, set())

        # A failure is forgotten once a check finds the helper running, checked for updates or not...
        blocked = "Windows stopped FFmpeg from running. This computer may only allow approved programs."

        def failed_install():
            app._check_finished(before, {}, True)
            for event in (("start", ffmpeg, 1, 1), ("line", ffmpeg, f"ERROR {blocked}"),
                          ("done", ffmpeg, False, blocked), ("finished", None, False)):
                app._notify(*event)
            app._check_finished(before, {}, False)
            self.assertEqual(self.text(row["state"]), blocked)

        failed_install()
        app._check_finished(after, {}, False)
        app._check_finished(before, {}, False)
        self.assertEqual(self.text(row["state"]), "Needs installing")
        # ...and once the helper is one only the Settings tab updates: an FFmpeg put in place with winget,
        # which trails gyan.dev's newest release. A red sentence about this page's download would sit under
        # "All set!".
        failed_install()
        winget = self.helpers(ffmpeg=deps.OUTDATED)
        winget["ffmpeg"].command = deps._winget_command("winget", "Gyan.FFmpeg")
        app._check_finished(winget, {}, False)
        self.assertEqual((self.text(screen.status), self.text(row["state"]), self.text(row["hint"])),
                         ("All set! Tapewright is ready.", "Needs an update", setup_screen.SETTINGS_HINT))

        # Not now finishes setup once the YouTube helper really failed, with yt-dlp and FFmpeg in place though
        # unchecked, as Start after "Almost ready." does.
        missing_js = unchecked(self.helpers(js=deps.MISSING), "yt-dlp", "ffmpeg")
        app._check_finished(missing_js, {}, True)
        deno = app.deps["js"]
        for event in (("start", deno, 1, 1), ("done", deno, False, "the download stopped with code 2"),
                      ("finished", None, False)):
            app._notify(*event)
        app._check_finished(missing_js, {}, False)
        self.assertEqual(screen.shown_buttons, ("start", "retry"))
        screen.not_now()
        self.assertIs(app.settings["setup_done"], True)

    def test_the_setup_page_judges_only_the_installs_it_runs_itself(self):
        from tapewright import setup_screen
        app = self.new_app(setup_done=True)
        screen, rows = app.setup_screen, app.setup_screen.rows
        app.confirm = lambda title, message: True  # the Settings tab's question, answered Yes
        # Out of date, and updated only from the Settings tab, which asks first: FFmpeg with winget, and deno
        # with its own updater.
        behind = self.helpers(ffmpeg=deps.OUTDATED, js=deps.UNSUPPORTED)
        behind["ffmpeg"].command = deps._winget_command("winget", "Gyan.FFmpeg")
        behind["js"].command = [r"C:\Users\someone\.deno\bin\deno.exe", "upgrade"]
        playing = {}

        class Runner:  # plays back each run, ending it with the code given for that kind of command
            def run(self, command, on_line):
                for line in playing["lines"]:
                    on_line(line)
                code = playing["codes"]["fetch" if deps.is_fetch(command) else deps._program(command)]
                if code is None:
                    raise procs.Cancelled()
                return code

        def from_settings(found, codes, lines=()):
            """Update from the Settings tab, the check after it finding found; then Run setup again."""
            app.hide_setup()
            app._check_finished(found, {}, True)
            playing.update(codes=codes, lines=lines)
            self.assertTrue(app.run_updates([key for key, dep in found.items() if dep.command]))
            self.pump(app)
            app._check_finished(found, {}, False)
            self.assertTrue(app.request_setup())
            return (self.text(screen.status), screen.shown_buttons,
                    [self.text(rows[key]["state"]) for key in ("ffmpeg", "js")], screen.failed_once)

        with mock.patch.object(self.app.procs, "Runner", Runner), \
                mock.patch.object(self.app, "threading", mock.Mock(Thread=self.Inline)):
            # winget has nothing newer yet, and deno's updater fails. Both are said in the Settings tab's log,
            # and neither is this page's failure, whose Try again would have nothing to run.
            all_set = ("All set! Tapewright is ready.", ("start",),
                       ["Needs an update", setup_screen.TOO_OLD], set())
            codes = {"winget": deps.WINGET_UPDATE_NOT_APPLICABLE, "deno": 1}
            self.assertEqual(from_settings(behind, codes), all_set)
            failure = deps.update_failure_text(behind["ffmpeg"], deps.WINGET_UPDATE_NOT_APPLICABLE)
            self.assertIn(f"Converter (FFmpeg): {failure}", app.settings_tab.logview.contents().splitlines())
            self.assertEqual(self.text(rows["ffmpeg"]["hint"]), setup_screen.SETTINGS_HINT)
            # Both exit 0, and the check after still finds them behind: not judged here either.
            self.assertEqual(from_settings(behind, {"winget": 0, "deno": 0}), all_set)

            # A fetch cancelled after it said it was putting deno in place is a note, as any cancel is.
            missing = self.helpers(js=deps.MISSING)
            swapped = deps.cancel_text(missing["js"], swapped=True)
            status, buttons, states, failed_once = from_settings(
                missing, {"fetch": None}, ("STEP Testing it…", f"STEP {deps.SWAP_STEP}"))
            self.assertEqual((buttons, states[1], failed_once),
                             (("install", "later"), swapped[:1].upper() + swapped[1:], set()))

        # However a failure came to be kept for a helper this page doesn't install, it never holds the page at
        # "failed" or "almost", which would offer a Try again with nothing to run.
        app._check_finished(behind, {}, False)
        screen.failures.update({"ffmpeg": "an old sentence", "js": "an old sentence"})
        screen.refresh()
        self.assertEqual((screen.verdict(), screen.shown_buttons, deps.setup_keys(app.deps)),
                         ("done", ("start",), []))

    def test_the_intro_says_a_helper_is_downloaded_only_while_it_is_the_copy_fetched(self):
        from tapewright import setup_screen
        app = self.new_app()
        screen, keys = app.setup_screen, list(setup_screen.KEYS)

        def intro(plan):
            self.assertEqual(screen.plan, plan)
            self.assertEqual(self.text(screen.intro), setup_screen.intro_text(keys, plan, deps.WINDOWS))

        app._check_finished(self.helpers(ytdlp=deps.MISSING, js=deps.MISSING), {}, True)
        intro({"yt-dlp": "pip", "js": "fetch"})
        # deno where the fetcher puts it, and yt-dlp where pip does: both sentences stay under a setup that
        # has just finished.
        fetched = self.helpers()
        fetched["js"].path = str(deps.own_copy("deno"))
        app._check_finished(fetched, {}, False)
        intro({"yt-dlp": "pip", "js": "fetch"})

        # The YouTube helper set to a Node.js already on this computer, which Tapewright never downloads.
        screen.start_using()
        app.settings["js_runtime"] = "node"
        node = self.helpers()
        node["js"].name, node["js"].path = "Node.js", r"C:\Program Files\nodejs\node.exe"
        app._check_finished(node, {}, False)
        self.assertTrue(app.request_setup())
        intro({"yt-dlp": "pip"})

        # FFmpeg, planned for this page's download and then put in place with winget instead.
        app.settings["js_runtime"] = "auto"
        app._check_finished(self.helpers(ffmpeg=deps.MISSING), {}, True)
        intro({"yt-dlp": "pip", "ffmpeg": "fetch"})
        winget = self.helpers()
        winget["ffmpeg"].path = (r"C:\Users\someone\AppData\Local\Microsoft\WinGet\Packages"
                                 r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
                                 r"\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe")
        app._check_finished(winget, {}, False)
        intro({"yt-dlp": "pip"})

        # A helper that is a problem again, which this page doesn't install: here, a check that itself failed.
        crashed = self.helpers()
        crashed["yt-dlp"] = deps.Dep("yt-dlp", "yt-dlp", True, detail="The check itself failed: OSError()")
        app._check_finished(crashed, {}, False)
        intro({})

    def test_hide_this_page_brings_back_the_tabs_while_the_batch_goes_on(self):
        app = self.new_app()
        screen = app.setup_screen
        app._check_finished(self.helpers(ffmpeg=deps.MISSING), {}, True)
        ffmpeg, cancel = app.deps["ffmpeg"], screen.buttons["cancel"]
        app._notify("start", ffmpeg, 1, 1)
        self.assertEqual(screen.shown_buttons, ("hide", "cancel"))
        self.assertTrue(cancel.instate(["!disabled"]))
        # While the fetcher puts the helper in place, Cancel can't be clicked.
        app.update_swapping = True  # as App sets it on reading this line
        app._notify("line", ffmpeg, f"STEP {deps.SWAP_STEP}")
        self.assertTrue(cancel.instate(["disabled"]))

        screen.buttons["hide"].invoke()
        self.assertEqual((app.setup_shown, app.notebook.winfo_manager()), (False, "pack"))
        app.update_swapping = False
        app._notify("done", ffmpeg, True, "done.")
        app._notify("finished", None, False)
        # Hiding put nothing off, and Help's Run setup again brings the page back to the batch it left.
        app.help_tab.run_setup()
        self.assertEqual((app.setup_shown, self.text(screen.status)),
                         (True, "Checking that everything works…"))
        app._check_finished(self.helpers(), {}, False)
        self.assertEqual((screen.shown_buttons, app._setup_put_off), (("start",), False))
        # Escape reaches Hide this page while a batch runs.
        app._notify("start", ffmpeg, 1, 1)
        screen._escape(None)
        self.assertEqual((app.setup_shown, app._setup_put_off), (False, False))

    def test_the_page_says_cancelling_only_when_app_really_cancels(self):
        app = self.new_app()
        screen = app.setup_screen
        cancel = screen.buttons["cancel"]
        app._check_finished(self.helpers(ffmpeg=deps.MISSING, js=deps.MISSING), {}, True)
        ffmpeg, deno = app.deps["ffmpeg"], app.deps["js"]
        runner = mock.Mock()
        app.updating, app._update_runner, app._update_queue = "ffmpeg", runner, [deno]
        app._notify("start", ffmpeg, 1, 2)
        self.assertTrue(cancel.instate(["!disabled"]))
        with mock.patch.object(self.app, "threading", mock.Mock(Thread=self.Inline)):
            # The worker read the fetcher's swap line after the page last drew Cancel, so the click still
            # lands, and App ignores it. The page follows App: nothing is cancelling, and nothing can be
            # cancelled now.
            app.update_swapping = True
            cancel.invoke()
            runner.cancel.assert_not_called()
            self.assertEqual((screen.cancelling, [d.key for d in app._update_queue]), (False, ["js"]))
            self.assertEqual((self.text(screen.status), cancel.instate(["disabled"])),
                             ("Step 1 of 2: Installing the Converter (FFmpeg)…", True))
            # So the next helper's install says what it is doing, and its Cancel works.
            app.update_swapping = False
            app._notify("done", ffmpeg, True, "done.")
            app._update_queue.clear()
            app._notify("start", deno, 2, 2)
            self.assertEqual((self.text(screen.status), cancel.instate(["!disabled"])),
                             ("Step 2 of 2: Installing the YouTube helper (deno)…", True))
            cancel.invoke()
        runner.cancel.assert_called_once_with()
        self.assertEqual((screen.cancelling, self.text(screen.status), cancel.instate(["disabled"])),
                         (True, "Cancelling…", True))

    def test_enter_presses_the_focused_button_or_else_the_default_one(self):
        app = self.new_app()
        screen, top = app.setup_screen, app.root
        # No ttk button answers Enter, so the page catches both Enter keys on the window.
        for sequence in ("<Return>", "<KP_Enter>"):
            self.assertIn("_enter", top.bind(sequence))

        def enter(widget):  # the key, as it reaches the window from the widget with the focus
            return screen._enter(mock.Mock(widget=widget))

        app._check_finished(self.helpers(ffmpeg=deps.MISSING), {}, True)
        self.assertEqual(screen.shown_buttons, ("install", "later"))
        with mock.patch.object(app, "run_updates", return_value=False) as run_updates:
            self.assertEqual(enter(top), "break")  # no button has the focus: the default one
            run_updates.assert_called_once_with(["ffmpeg"], confirm=False)
            enter(screen.buttons["start"])  # not one of the buttons shown now
            self.assertEqual((run_updates.call_count, app.setup_shown), (2, True))
            enter(screen.details_button)  # beside the buttons, and pressed like them rather than installing
            self.assertEqual((run_updates.call_count, screen.details_shown), (2, True))
            enter(screen.buttons["later"])
            self.assertEqual((run_updates.call_count, app.setup_shown, app._setup_put_off), (2, False, True))

        # While a batch runs the default is Hide, and Enter cancels only when Cancel has the focus.
        app._setup_put_off = False
        self.assertTrue(app.request_setup())
        app._notify("start", app.deps["ffmpeg"], 1, 1)
        with mock.patch.object(app, "cancel_update", return_value=True) as cancel_update:
            enter(screen.buttons["cancel"])
            cancel_update.assert_called_once_with()
            enter(screen.buttons["cancel"])  # disabled now that it is cancelling, so nothing is pressed
            self.assertEqual((cancel_update.call_count, app.setup_shown), (1, True))
        enter(top)
        self.assertEqual((app.setup_shown, app._setup_put_off), (False, False))
        # Out of sight, the page presses nothing.
        with mock.patch.object(app, "hide_setup") as hide_setup:
            self.assertIsNone(enter(top))
        hide_setup.assert_not_called()

    def test_help_says_why_setup_cannot_open_over_a_conversion(self):
        from tapewright import help_tab
        app = self.open_window()
        tab, help_page = app.mp4_tab, app.help_tab
        help_page.show_topic("Setting up Tapewright")
        refused = "Setup can't open while a conversion is running. Try again when it has finished."
        self.assertEqual(help_tab.SETUP_REFUSED, refused)
        for pending, running in ((True, ()), (False, (tab,))):  # waiting on the gate, then running
            with self.subTest(pending=pending):
                tab.pending = pending
                app.active_jobs.clear()
                app.active_jobs.update(running)
                help_page.message.set("")
                help_page.run_setup()
                self.assertEqual((app.setup_shown, help_page.message.get()), (False, refused))
        tab.pending = False
        app.active_jobs.clear()
        help_page.message.set("")
        help_page.run_setup()
        self.assertEqual((app.setup_shown, help_page.message.get()), (True, ""))

    def test_the_setup_buttons_are_never_what_a_short_window_cuts_off(self):
        from tapewright import setup_screen
        output = "Tapewright's own copy won't run: " + " ".join(["it printed a long line"] * 20)

        def failed_page(scale, size, screen_height):
            """The page placed where App's window would put it, after a batch the check found useless."""
            app = self.new_app(scale=scale)
            screen = app.setup_screen
            ratio = app.root.winfo_fpixels("1i") / 96.0
            width = int(size[0] * ratio)
            # Less the room App leaves for the taskbar.
            height = min(int(size[1] * ratio), screen_height - int(90 * ratio))
            inset = 2 * int(str(screen.pack_info()["padx"]))
            # A withdrawn window is never laid out, but a frame placed at a fixed size is, with its children.
            screen.pack_forget()
            screen.place(x=0, y=0, width=width - inset, height=height - inset)
            app._check_finished(self.helpers(deps.MISSING, deps.MISSING, deps.MISSING), {}, True)
            for index, key in enumerate(setup_screen.KEYS, 1):
                app._notify("start", app.deps[key], index, 3)
                app._notify("done", app.deps[key], True, "done.")
            app._notify("finished", None, False)
            # Each install finished, yet the check still can't use any of them, and says why at length.
            after = self.helpers(deps.MISSING, deps.MISSING, deps.MISSING)
            for dep in after.values():
                dep.detail = output
            app._check_finished(after, {}, False)
            app.root.update_idletasks()
            self.assertEqual(screen.shown_buttons, ("retry", "help", "later"))
            return app, screen

        def bottom(widget):
            return widget.winfo_y() + widget.winfo_height()

        # The smallest window on a 1366 x 768 laptop at 125%.
        app, screen = failed_page(1.25, self.app.SMALLEST, 768)
        bar = screen.buttons["retry"].master
        self.assertLessEqual(bottom(bar), screen.winfo_height(), "the buttons would be cut off")
        self.assertLess(screen.rows_area.winfo_height(), screen.table.winfo_reqheight())  # the rows scroll
        screen.toggle_details()
        app.root.update_idletasks()
        self.assertLessEqual(bottom(bar), screen.logview.winfo_y())
        self.assertLessEqual(bottom(screen.logview), screen.winfo_height())
        self.assertGreater(screen.logview.winfo_height(), setup_screen.DETAILS_FLOOR)  # never squeezed away

        # The default window at 96 DPI has room for all of it, so nothing there scrolls.
        app, screen = failed_page(None, self.app.WINDOW, 4000)
        self.assertGreaterEqual(screen.rows_area.winfo_height(), screen.table.winfo_reqheight())
        self.assertLessEqual(bottom(screen.buttons["retry"].master), screen.winfo_height())

    def test_quitting_during_a_download_says_what_that_costs(self):
        app = self.open_window()
        asked = []
        app.confirm = lambda title, message: asked.append(message) or False  # declined, so the window stays
        app.updating = "ffmpeg"
        app.on_close()
        app.deps["ffmpeg"].command = deps.fetch_command("ffmpeg", "9.1")
        app.on_close()
        app.updating = None
        self.assertIn("half-installed", asked[0])  # winget promises nothing
        self.assertIn("Quitting now stops it, and nothing is replaced.", asked[1])
        self.assertNotIn("half-installed", asked[1])

    def test_a_setup_batch_ends_almost_ready_and_then_all_set(self):
        app = self.new_app()
        screen = app.setup_screen
        app._check_finished(self.helpers(ffmpeg=deps.MISSING, js=deps.MISSING), {}, True)
        events = []
        app.update_listeners.append(lambda event, dep, *rest: events.append((event, dep and dep.key) + rest))
        damaged = "The download was damaged on the way, so it was thrown away. Try again."
        script = {"ffmpeg": (["STEP Downloading…", "PROGRESS 5 10", "PROGRESS 10 10", "DONE 9.0.1"], 0),
                  "deno": ([f"ERROR {damaged}"], 3)}

        class Runner:  # stands in for procs.Runner, so no process ever starts
            def run(self, command, on_line):
                lines, code = script[command[2]]
                for line in lines:
                    on_line(line)
                return code

        with mock.patch.object(self.app.procs, "Runner", Runner), \
                mock.patch.object(self.app, "threading", mock.Mock(Thread=self.Inline)):
            screen.buttons["install"].invoke()
            self.pump(app)
            self.assertEqual(events, [
                ("start", "ffmpeg", 1, 2), ("line", "ffmpeg", "STEP Downloading…"),
                ("line", "ffmpeg", "PROGRESS 5 10"), ("line", "ffmpeg", "PROGRESS 10 10"),
                ("line", "ffmpeg", "DONE 9.0.1"), ("done", "ffmpeg", True, "done."),
                ("start", "js", 2, 2), ("line", "js", f"ERROR {damaged}"), ("done", "js", False, damaged),
                ("finished", None, False)])
            log = app.settings_tab.logview.contents()
            self.assertIn(f"YouTube helper (deno): {damaged}", log.splitlines())  # its sentence, not its code
            self.assertEqual(screen.logview.contents(), log)
            self.assertEqual(self.text(screen.status), "Checking that everything works…")

            app._check_finished(self.helpers(js=deps.MISSING), {}, False)  # the check after the batch
            self.assertEqual((self.text(screen.status), screen.shown_buttons),
                             ("Almost ready.", ("start", "retry")))
            js = screen.rows["js"]["state"]
            self.assertEqual((self.text(js), self.text(js, "foreground")), (damaged, theme.C["error"]))

            # [Try again] after that offline check looks the version up online first.
            script["deno"] = (["STEP Testing it…", "DONE 2.9.6"], 0)
            events.clear()
            screen.buttons["retry"].invoke()
            app._check_finished(self.helpers(js=deps.MISSING), {}, True)
            self.app.App.check_dependencies.call_args.kwargs["then"]()
            self.pump(app)
            expected = [("start", "js"), ("line", "js"), ("line", "js"), ("done", "js"), ("finished", None)]
            self.assertEqual([event[:2] for event in events], expected)
            app._check_finished(self.helpers(), {}, False)

        self.assertEqual((self.text(screen.status), screen.shown_buttons),
                         ("All set! Tapewright is ready.", ("start",)))
        screen.buttons["start"].invoke()
        self.assertFalse(app.setup_shown)
        self.assertEqual(app.notebook.select(), str(app.mp3_tab))
        saved = config.Settings(app.settings.path)
        saved.load()
        self.assertIs(saved["setup_done"], True)

    def test_a_failed_setup_offers_help_and_a_cancelled_one_the_button_again(self):
        from tapewright import setup_screen
        self.assertIn(setup_screen.HELP_TOPIC, [topic["title"] for topic in help_content.TOPICS])
        app = self.new_app()
        screen = app.setup_screen
        missing = self.helpers(ytdlp=deps.MISSING, ffmpeg=deps.MISSING)
        app._check_finished(missing, {}, True)
        ytdlp, ffmpeg = app.deps["yt-dlp"], app.deps["ffmpeg"]
        blocked = "Windows stopped FFmpeg from running. This computer may only allow approved programs."
        for event in (("start", ytdlp, 1, 2), ("done", ytdlp, False, deps.update_failure_text(ytdlp, 1)),
                      ("start", ffmpeg, 2, 2), ("line", ffmpeg, f"ERROR {blocked}"),
                      ("done", ffmpeg, False, blocked), ("finished", None, False)):
            app._notify(*event)
        app._check_finished(missing, {}, False)
        self.assertEqual(screen.shown_buttons, ("retry", "help", "later"))
        self.assertEqual(self.text(screen.rows["ffmpeg"]["state"]), blocked)
        self.assertEqual(self.text(screen.rows["yt-dlp"]["state"]), setup_screen.DIDNT_INSTALL)
        self.assertIs(app.settings["setup_done"], False)
        screen.buttons["help"].invoke()
        self.assertFalse(app.setup_shown)
        self.assertEqual(app.notebook.select(), str(app.help_tab))
        self.assertEqual(app.help_tab.text.get("1.0", "1.end"), setup_screen.HELP_TOPIC)

        app.show_setup()
        with mock.patch.object(app, "cancel_update", return_value=True) as cancel_update:
            app._notify("start", ffmpeg, 1, 1)
            screen.buttons["cancel"].invoke()
        cancel_update.assert_called_once_with()
        self.assertEqual(self.text(screen.status), "Cancelling…")
        app._notify("done", ffmpeg, False, deps.cancel_text(ffmpeg))
        app._notify("finished", None, True)
        app._check_finished(missing, {}, False)
        self.assertEqual(screen.shown_buttons, ("install", "later"))
        cancelled = self.text(screen.rows["ffmpeg"]["state"])
        self.assertTrue(cancelled.startswith("Cancelled. Nothing was replaced"), cancelled)

    def test_the_settings_tab_runs_setup_and_names_a_new_release(self):
        app = self.open_window()
        tab = app.settings_tab
        self.assertEqual((tab.release.winfo_manager(), tab.log_caption.winfo_manager()), ("", "pack"))
        seen = {"tapewright": {"version": "99.0.0", "checked": "2026-09-14T12:00:00"}}
        app._release_finished(seen, None)  # GitHub's answer, from a check that found nothing cached
        # In the caption's place on the bottom row, never a row of its own that pushes the log down.
        self.assertEqual((tab.release.winfo_manager(), tab.log_caption.winfo_manager(),
                          self.text(tab.release_label)), ("pack", "", "Tapewright 99.0.0 is out."))
        self.assertIs(tab.release.master, tab.setup_button.master)
        self.assertEqual(app.settings["latest_cache"]["tapewright"], seen["tapewright"])
        with mock.patch("webbrowser.open") as open_page:
            tab.open_release_page()
        open_page.assert_called_once_with(deps.RELEASES_PAGE)
        taken_down = {"tapewright": {"version": "", "checked": "2026-09-15T12:00:00"}}  # a 404 the next day
        app._release_finished(taken_down, seen["tapewright"])
        self.assertEqual((tab.release.winfo_manager(), tab.log_caption.winfo_manager()), ("", "pack"))

        # A batch doesn't hold it back: the screen follows a batch whoever started it, and Hide this page
        # lands on this tab. Only a conversion, running or waiting to start, does, as request_setup refuses
        # then.
        app.updating = "ffmpeg"
        tab.on_busy_changed()
        self.assertTrue(tab.setup_button.instate(["!disabled"]))
        tab.setup_button.invoke()
        self.assertTrue(app.setup_shown)
        app.hide_setup()
        app.updating = None
        for pending, running in ((True, ()), (False, (app.mp3_tab,))):
            with self.subTest(pending=pending):
                app.mp3_tab.pending = pending
                app.active_jobs.update(running)
                tab.on_busy_changed()
                self.assertTrue(tab.setup_button.instate(["disabled"]))
                app.active_jobs.clear()
        app.mp3_tab.pending = False
        tab.on_busy_changed()
        # A start that waits on a check holds it back from the moment it waits, until the gate decides.
        check = self.app.App.check_dependencies
        app.checking, app.mp3_tab.pending = True, True
        app.request_start(app.mp3_tab, "url", mock.Mock())
        self.assertTrue(tab.setup_button.instate(["disabled"]))
        app._after_check.append(check.call_args.kwargs["then"])  # as the real check_dependencies keeps it
        app.ask_outdated = lambda root, problems, blocked: "cancel"  # the warning about yt-dlp, declined
        app._check_finished(dict(app.deps), {}, False)
        self.assertEqual((app.mp3_tab.pending, app.active_jobs), (False, set()))
        self.assertTrue(tab.setup_button.instate(["!disabled"]))
        tab.setup_button.invoke()
        self.assertTrue(app.setup_shown)

    def test_the_settings_tab_joins_detail_and_how_and_copies_diagnostics(self):
        app = self.open_window()
        tab = app.settings_tab
        found = dict(app.deps)
        found["ffmpeg"] = deps.Dep("ffmpeg", "FFmpeg", True, state=deps.OK, detail="Latest release.",
                                   how="Update FFmpeg the way you installed it.")
        app._check_finished(found, {}, False)
        self.assertTrue(self.text(tab.rows["ffmpeg"]["detail"]).endswith("the way you installed it."))
        self.assertNotIn("..", self.text(tab.rows["ffmpeg"]["detail"]))

        with mock.patch.object(tab, "clipboard_clear"), mock.patch.object(tab, "clipboard_append") as copied:
            tab.copy_diagnostics()
        text = copied.call_args.args[0]
        self.assertIn(f"Tk {tab.tk.call('info', 'patchlevel')}", text)
        self.assertIn(f"Started by {sys.executable}; ", text)
        self.assertIn(f"the installer's own Python: {'yes' if procs.bundled() else 'no'}", text)
        self.assertIn(f"Tapewright's own helpers: {deps.tools_dir() or '-'}", text)

    def test_the_settings_tab_fits_the_default_window_with_a_release_and_every_button(self):
        app = self.new_app()
        own_copy = "Update FFmpeg the way you installed it, or let Tapewright keep its own copy."
        longest = {  # each row with its longest button and a detail that wraps
            "yt-dlp": deps.Dep("yt-dlp", "yt-dlp", True, state=deps.OK, installed="2026.9.10.232121.dev0",
                               latest="2026.9.10", action="Switch to stable",
                               command=deps.ytdlp_install_command("2026.9.10", True),
                               detail="A nightly build is installed; the latest stable release is 2026.9.10.",
                               how="pip, into the Python that runs Tapewright"),
            "ffmpeg": deps.Dep("ffmpeg", "FFmpeg", True, state=deps.OUTDATED, installed="9.0.1", latest="9.1",
                               action=deps.OWN_COPY_ACTION, command=deps.fetch_command("ffmpeg", "9.1"),
                               detail="FFmpeg 9.1 is out (as of the check on 2026-09-14).", how=own_copy),
            "js": deps.Dep("js", "deno or Node.js", False, state=deps.UNSUPPORTED, installed="2.2.0",
                           latest="2.9.6", action=deps.OWN_COPY_ACTION,
                           command=deps.fetch_command("deno", "2.9.6"),
                           detail="yt-dlp needs deno 2.3.0 or newer.",
                           how=own_copy.replace("FFmpeg", "deno")),
        }
        app._check_finished(longest, {}, True)
        release = {"tapewright": {"version": "10.20.300", "checked": "2026-09-14T12:00:00"}}
        app._release_finished(release, None)
        app.root.update_idletasks()

        def across(value):
            """Both sides of a pad option, however Tk spells it: "8", "4 0" or "(12,)"."""
            numbers = [int(n) for n in re.findall(r"\d+", str(value))]
            return 2 * numbers[0] if len(numbers) == 1 else sum(numbers)

        tab, notebook = app.settings_tab, app.notebook
        panes = (app.mp3_tab, app.mp4_tab, app.settings_tab, app.help_tab)
        scale = app.root.winfo_fpixels("1i") / 96.0
        width, height = (int(size * scale) for size in self.app.WINDOW)
        # A withdrawn window is never laid out, so its words are wrapped here to the width the default
        # window gives.
        inside = (width - across(notebook.pack_info()["padx"]) - across(tab.cget("padding"))
                  - (notebook.winfo_reqwidth() - max(pane.winfo_reqwidth() for pane in panes)))
        with mock.patch.object(tab.tools, "winfo_width", return_value=inside):
            tab._rewrap()
        app.root.update_idletasks()
        room = (height - across(notebook.pack_info()["pady"])
                - (notebook.winfo_reqheight() - max(pane.winfo_reqheight() for pane in panes)))
        log = tab.logview.grid_info()
        floor = int(tab.grid_rowconfigure(int(log["row"]))["minsize"])
        needed = tab.winfo_reqheight() - tab.logview.winfo_reqheight() + floor
        self.assertLessEqual(needed, room, "the update log would lose its last lines in the default window")
        self.assertLessEqual(tab.setup_button.master.winfo_reqwidth(), inside)

    def test_the_tools_answer_is_posted_before_github_is_asked_about_tapewright(self):
        app = self.new_app()
        queued = []

        def check_release(cache, online):
            queued.append([fn.__name__ for fn, _args in list(app._events.queue)])
            return ({"version": "99.0.0", "url": deps.RELEASES_PAGE},
                    {"tapewright": {"version": "99.0.0", "checked": "2026-09-14T12:00:00"}})

        with mock.patch.object(deps, "check_all", return_value=(self.helpers(), {})), \
                mock.patch.object(deps, "check_release", side_effect=check_release), \
                mock.patch.object(self.app, "threading", mock.Mock(Thread=self.Inline)):
            self.real_check_dependencies(app, online=True)
        # However long GitHub takes, the check is already over, so it can't hold back a conversion.
        self.assertEqual(queued, [["_check_finished"]])
        self.pump(app)
        self.assertEqual((app.checking, app.release["version"]), (False, "99.0.0"))

    def test_a_release_answer_that_lands_late_never_hides_a_newer_one(self):
        two_days_ago = datetime.datetime.now().replace(microsecond=0) - datetime.timedelta(days=2)
        seen = {"version": deps.__version__, "checked": two_days_ago.isoformat()}
        newer = '{"tag_name": "v99.0.0"}'

        def window():
            app = self.new_app()
            app.settings["latest_cache"]["tapewright"] = dict(seen)  # two days old, so the next check asks
            return app

        def check(app, online, answer):
            """One check's worker, run at once, GitHub giving answer. Returns its posts, still to run."""
            failed = isinstance(answer, Exception)
            github = mock.Mock(side_effect=answer) if failed else mock.Mock(return_value=answer)
            with mock.patch.object(deps, "check_all", return_value=(self.helpers(), {})), \
                    mock.patch.object(deps, "fetch_text", github), \
                    mock.patch.object(self.app, "threading", mock.Mock(Thread=self.Inline)):
                self.real_check_dependencies(app, online=online)
            posted = []
            while not app._events.empty():
                posted.append(app._events.get_nowait())
            return posted

        def apply(*posted):
            for fn, args in posted:
                fn(*args)

        # An offline check started while an online one asks GitHub read the cache from before that answer, and
        # its own answer lands last.
        app = window()
        first = check(app, True, newer)
        apply(first[0])  # the tools' answer, which lets another check start
        second = check(app, False, None)
        apply(first[1], *second)
        self.assertEqual((app.release, self.text(app.settings_tab.release_label)),
                         ({"version": "99.0.0", "url": deps.RELEASES_PAGE}, "Tapewright 99.0.0 is out."))

        # A slow failure that lands after another check's answer is never put over it...
        app = window()
        slow = check(app, True, OSError("GitHub took too long"))
        apply(slow[0])
        fast = check(app, True, newer)
        apply(*fast, slow[1])
        self.assertEqual((app.release["version"], app.settings["latest_cache"]["tapewright"]["version"]),
                         ("99.0.0", "99.0.0"))
        self.assertNotIn("failed", app.settings["latest_cache"]["tapewright"])
        # ...but one with nothing newer beside it is remembered, so GitHub isn't asked again for an hour.
        app = window()
        apply(*check(app, True, OSError("no network")))
        self.assertEqual((app.release, app.settings["latest_cache"]["tapewright"]["failed"]), (None, True))

    def test_a_check_overtaken_by_a_newer_one_is_never_shown(self):
        app = self.new_app()
        check = self.app.App.check_dependencies
        check.reset_mock()
        waited = []
        app.checking = True
        app._after_check.append(lambda: waited.append(app.deps["ffmpeg"].state))
        # A batch that installed FFmpeg ends while a check that read it as missing is still running.
        app.updating, app._update_queue, app._update_runner = "ffmpeg", [], mock.Mock()
        app._next_update()
        check.assert_not_called()
        self.assertIs(app._recheck_online, False)  # queued behind the running one
        news = {"ffmpeg": {"version": "9.0.1", "checked": "2026-09-14T12:00:00"}}
        with mock.patch.object(app.setup_screen, "on_deps_changed") as screen_heard:
            app._check_finished(self.helpers(ffmpeg=deps.MISSING), news, True)
        # Its answer goes nowhere: not to the gate, the setup screen, the first-check rule or anything
        # waiting on it.
        check.assert_called_once_with(online=False)
        screen_heard.assert_not_called()
        self.assertEqual((app.deps, waited, app._checked_once, app.last_check_online), ({}, [], False, False))
        self.assertEqual(app.settings["latest_cache"]["ffmpeg"], news["ffmpeg"])  # what it looked up is kept
        app.checking = True  # as that fresh check_dependencies would have set it
        app._check_finished(self.helpers(), {}, False)
        self.assertEqual((waited, app._checked_once, app.setup_shown), ([deps.OK], True, False))

    def test_a_setting_changed_during_a_batch_is_checked_after_it(self):
        app = self.open_window()
        check = self.app.App.check_dependencies
        check.reset_mock()
        app.updating, app._update_queue, app._update_runner = "ffmpeg", [], mock.Mock()
        app.settings_tab._run_recheck(True)  # the yt-dlp channel changed, which needs the network
        app.settings_tab._run_recheck(False)
        check.assert_not_called()  # a check now would read the tool the batch is replacing
        app._next_update()
        check.assert_called_once_with(online=True)
        self.assertIs(app._batch_recheck_online, False)

    def test_a_start_waiting_on_the_first_check_never_runs_behind_the_setup_screen(self):
        app = self.new_app(setup_done=True)
        check = self.app.App.check_dependencies
        tab, started = app.mp3_tab, mock.Mock()
        app.checking, tab.pending = True, True  # Convert on a local file while the first check runs
        app.request_start(tab, "file", started)
        # Kept, as the real check_dependencies keeps it.
        app._after_check.append(check.call_args.kwargs["then"])
        app._check_finished(self.helpers(ytdlp=deps.MISSING), {}, True)
        # yt-dlp is missing, but the screen doesn't take the tabs' place over a start, and a local file
        # never needs yt-dlp.
        self.assertFalse(app.setup_shown)
        started.assert_called_once_with()
        app.job_finished(tab)

        # A setup screen that is showing when the start's turn comes declines it.
        app.checking, tab.pending = True, True
        app.request_start(tab, "file", started)
        app._after_check.append(check.call_args.kwargs["then"])
        app.show_setup()
        app._check_finished(self.helpers(ytdlp=deps.MISSING), {}, True)
        self.assertEqual((started.call_count, tab.pending, tab.status.get()), (1, False, "Not started."))
        self.assertEqual(app.active_jobs, set())

    def test_nothing_cancels_or_quits_while_a_helper_is_put_in_place(self):
        app = self.new_app()
        app._check_finished(self.helpers(ffmpeg=deps.MISSING, js=deps.MISSING), {}, True)
        cancels, waits = [], []
        script = {"ffmpeg": ["STEP Testing it…", f"STEP {deps.SWAP_STEP}", "STEP Done.", "DONE 9.0.1"],
                  "deno": ["STEP Downloading…"]}

        class Runner:  # stands in for procs.Runner, so no process ever starts
            finishes = True

            def run(self, command, on_line):
                for line in script[command[2]]:
                    on_line(line)
                return 0

            def cancel(self):
                cancels.append(True)

            def wait(self, timeout):
                waits.append(timeout)
                return Runner.finishes

        def step():  # one posted event, as the window's timer would run it
            fn, args = app._events.get_nowait()
            fn(*args)

        tab = app.settings_tab
        with mock.patch.object(self.app.procs, "Runner", Runner), \
                mock.patch.object(self.app, "threading", mock.Mock(Thread=self.Inline)):
            self.assertTrue(app.run_updates(["ffmpeg", "js"], confirm=False))
            # Set as the fetcher's line was read, before the window has drawn anything.
            self.assertTrue(app.update_swapping)
            step()
            step()
            self.assertTrue(tab.cancel_update_button.instate(["disabled"]))
            self.assertIs(app.cancel_update(), False)
            self.assertEqual((cancels, [d.key for d in app._update_queue]), ([], ["js"]))  # nothing skipped

            asked = []
            app.confirm = lambda title, message: asked.append(message) or False
            with mock.patch.object(app, "shutdown") as shutdown:
                app.on_close()
            self.assertEqual(asked, [])  # no question whose answer could only do harm
            shutdown.assert_called_once_with()
            with mock.patch.object(app.root, "destroy"):
                app.shutdown()  # waits for the run to end rather than killing it
                self.assertEqual((waits, cancels), ([self.app.SWAP_WAIT_S], []))
                # Still running after that: something is stuck, and nothing may be left running.
                Runner.finishes = False
                app.shutdown()
                self.assertEqual(cancels, [True])

            step()
            step()
            step()  # the run ends, and the next helper starts with its Cancel usable again
            self.assertFalse(app.update_swapping)
            self.assertTrue(tab.cancel_update_button.instate(["!disabled"]))
            self.pump(app)
        self.assertFalse(app.update_swapping)

    def test_a_cancelled_download_is_put_right_at_once(self):
        from tapewright import fetch
        app = self.new_app()
        tools = deps.tools_dir()
        # new_app's own folder, never Tapewright's real one, which this test would otherwise tidy up.
        self.assertIn(Path(tempfile.gettempdir()).resolve(), tools.resolve().parents)
        app._check_finished(self.helpers(ytdlp=deps.MISSING, ffmpeg=deps.MISSING), {}, True)
        ffmpeg, log = app.deps["ffmpeg"], app.settings_tab.logview
        check, playing, found = self.app.App.check_dependencies, {}, []
        # What the check after the batch would find in the folder, noted as it is asked for.
        check.side_effect = lambda **kw: found.append(sorted(p.name for p in tools.iterdir())
                                                      if tools.exists() else None)

        class Runner:  # plays back a run that Cancel killed, ending it as procs.Runner would
            def run(self, command, on_line):
                for line in playing["lines"]:
                    on_line(line)
                raise procs.Cancelled()

        def cancelled(key, *lines):
            playing["lines"] = lines
            self.assertTrue(app.run_updates([key], confirm=False))
            self.pump(app)
            self.assertIsNone(app.updating)  # the batch ended, whatever happened while tidying up
            return log.contents().splitlines()

        def killed_between_the_renames():
            """As a kill between swap()'s two renames leaves the folder: no ffmpeg, the old copy aside."""
            for name in (".ffmpeg-old", ".ffmpeg-new"):
                (tools / name).mkdir(parents=True, exist_ok=True)
                (tools / name / "ffmpeg.exe").write_text(name, encoding="utf-8")

        with mock.patch.object(self.app.procs, "Runner", Runner), \
                mock.patch.object(self.app, "threading", mock.Mock(Thread=self.Inline)):
            # No folder yet: nothing to put right, and none is made.
            lines = cancelled("ffmpeg", "STEP Downloading…")
            self.assertEqual((tools.exists(), found), (False, [None]))
            self.assertEqual(lines[-2:],
                             ["Downloading…", f"Converter (FFmpeg): {deps.cancel_text(ffmpeg)}"])

            # Cancel was clicked before the window read the swap line, and the kill landed between the
            # renames. The old copy is back before the check after the batch looks, and nothing promises
            # what is there.
            killed_between_the_renames()
            lines = cancelled("ffmpeg", "STEP Testing it…", f"STEP {deps.SWAP_STEP}")
            self.assertEqual((tools / "ffmpeg" / "ffmpeg.exe").read_text(encoding="utf-8"), ".ffmpeg-old")
            self.assertEqual(found[-1], [".ffmpeg.lock", "ffmpeg"])
            self.assertEqual(lines[-4:], [
                deps.SWAP_STEP, "Put back the copy of ffmpeg that an interrupted update had moved aside.",
                "Removed .ffmpeg-new, left over from an interrupted update.",
                f"Converter (FFmpeg): {deps.cancel_text(ffmpeg, swapped=True)}"])
            self.assertFalse(app.update_swapping)

            # Another window's run holds the tool, so its own recover() has run, and its files are left alone.
            killed_between_the_renames()
            held = fetch.lock(tools, "ffmpeg")
            try:
                lines = cancelled("ffmpeg", "STEP Downloading…")
            finally:
                fetch.unlock(held)
            self.assertTrue((tools / ".ffmpeg-old").is_dir() and (tools / ".ffmpeg-new").is_dir())
            self.assertEqual(lines[-2:],
                             ["Downloading…", f"Converter (FFmpeg): {deps.cancel_text(ffmpeg)}"])

            # A folder that can't be tidied says why in the log, and lets go of the tool.
            denied = PermissionError(13, "Access is denied")
            with mock.patch.object(fetch, "recover", side_effect=denied):
                lines = cancelled("ffmpeg", "STEP Downloading…")
            self.assertEqual(lines[-2], f"{tools}: {denied}")
            held = fetch.lock(tools, "ffmpeg")
            self.assertIsNotNone(held)
            fetch.unlock(held)
            with mock.patch.object(fetch, "recover", side_effect=RuntimeError("a bug")):
                lines = cancelled("ffmpeg", "STEP Downloading…")
            self.assertIn("RuntimeError: a bug", "\n".join(lines))

            # pip keeps nothing staged, so nothing is tidied up after it.
            with mock.patch.object(fetch, "lock") as lock:
                lines = cancelled("yt-dlp")
            lock.assert_not_called()
            self.assertEqual(lines[-1], f"Downloader (yt-dlp): {deps.cancel_text(app.deps['yt-dlp'])}")

    def test_the_helpers_folder_and_the_download_pages(self):
        from tapewright import help_tab
        app = self.open_window()
        with tempfile.TemporaryDirectory() as folder:
            tools = Path(folder) / "tools"
            with mock.patch.dict(os.environ, {deps.TOOLS_DIR_ENV: str(tools)}), \
                    mock.patch.object(help_tab.os, "startfile", create=True) as startfile, \
                    mock.patch.object(help_tab.subprocess, "Popen") as popen:
                app.help_tab.open_tools_folder()
            self.assertTrue((tools / "ffmpeg").is_dir() and (tools / "deno").is_dir())
            opened = startfile.call_args.args[0] if os.name == "nt" else popen.call_args.args[0][-1]
            self.assertEqual(Path(opened), tools)
        with mock.patch("webbrowser.open") as open_page:
            app.help_tab.open_ffmpeg_page()
            app.help_tab.open_deno_page()
            app.help_tab.open_release_page()
        self.assertEqual([call.args[0] for call in open_page.call_args_list],
                         ["https://www.gyan.dev/ffmpeg/builds/",
                          "https://github.com/denoland/deno/releases/latest",
                          "https://github.com/angrysandhill/tapewright/releases/latest"])

    def test_the_installer_can_tell_that_tapewright_is_open(self):
        fake_ctypes, fake_os = mock.Mock(), mock.Mock()
        fake_os.name = "nt"
        fake_ctypes.windll.kernel32.CreateMutexW.return_value = 1234
        # ctypes is faked, so no real mutex is ever made here.
        with mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}), \
                mock.patch.object(self.app, "os", fake_os), \
                mock.patch.object(self.app, "_app_mutex", []) as held:
            self.app._hold_app_mutex()
        fake_ctypes.windll.kernel32.CreateMutexW.assert_called_once_with(None, False, "Tapewright.Running")
        self.assertEqual(held, [1234])  # kept for the life of the process
        with mock.patch.object(self.app.tk, "Tk") as tk_root, mock.patch.object(self.app, "App"), \
                mock.patch.object(self.app, "_enable_dpi_awareness"), \
                mock.patch.object(self.app, "_set_app_user_model_id"), \
                mock.patch.object(self.app.config, "Settings"), \
                mock.patch.object(self.app, "_hold_app_mutex") as hold:
            self.assertEqual(self.app.main(), 0)
        hold.assert_called_once_with()
        tk_root.return_value.mainloop.assert_called_once_with()

    def test_the_taskbar_groups_the_window_with_the_installers_shortcuts(self):
        # ctypes is faked, so this process's own ID is never changed here.
        for os_name, refused, calls in (("nt", None, 1), ("nt", OSError("refused"), 1), ("posix", None, 0)):
            fake_ctypes, fake_os = mock.Mock(), mock.Mock()
            fake_os.name = os_name
            set_id = fake_ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
            set_id.side_effect = refused
            with mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}), \
                    mock.patch.object(self.app, "os", fake_os):
                self.app._set_app_user_model_id()  # a refusal is passed over: only the grouping is lost
            self.assertEqual(set_id.call_args_list, [mock.call("AngrySandhill.Tapewright")] * calls)
        # Windows wants the ID before the program shows anything, so it is set before the window exists.
        order = mock.Mock()
        with mock.patch.object(self.app.tk, "Tk", order.Tk), mock.patch.object(self.app, "App"), \
                mock.patch.object(self.app, "_enable_dpi_awareness"), \
                mock.patch.object(self.app.config, "Settings"), \
                mock.patch.object(self.app, "_hold_app_mutex"), \
                mock.patch.object(self.app, "_set_app_user_model_id", order.set_id):
            self.assertEqual(self.app.main(), 0)
        self.assertEqual([call[0] for call in order.mock_calls[:2]], ["set_id", "Tk"])

    def test_the_title_bar_cassette_is_drawn_from_its_pixels(self):
        image = self.new_app(setup_done=True).root._cassette_icon
        self.assertEqual((image.width(), image.height()), (48, 48))
        for y, row in enumerate(theme.cassette_pixels(48)):
            for x, color in enumerate(row):
                # A pixel nothing was put into stays clear, so the title bar shows through the corners.
                self.assertEqual(image.transparency_get(x, y), color is None, (x, y))
                if color is not None:
                    self.assertEqual("#{:02x}{:02x}{:02x}".format(*image.get(x, y)), color, (x, y))


class Help(unittest.TestCase):
    @staticmethod
    def texts(topic):
        for _kind, value in topic["blocks"]:
            yield from (value if isinstance(value, list) else [value])

    def test_topics_are_well_formed(self):
        titles = [topic["title"] for topic in help_content.TOPICS]
        self.assertTrue(titles)
        self.assertEqual(len(titles), len(set(titles)))
        for topic in help_content.TOPICS:
            self.assertLessEqual(len(topic["title"]), 30, "the question list cannot wrap")
            for kind, value in topic["blocks"]:
                self.assertIn(kind, ("p", "steps", "list", "tip"))
                self.assertIsInstance(value, list if kind in ("steps", "list") else str)
            for key in topic["actions"]:
                self.assertIn(key, help_content.ACTION_LABELS)
            for text in self.texts(topic):
                unmarked = re.sub(r"\[[^\[\]]+\]|<[^<>]+>", "", text)
                self.assertFalse(set(unmarked) & set("[]<>"), f"unbalanced markup: {text}")

    def test_every_label_it_names_is_on_screen(self):
        strings = set()
        for path in (ROOT / "tapewright").glob("*.py"):
            if path.name != "help_content.py":
                tree = ast.parse(path.read_text(encoding="utf-8"))
                strings.update(node.value for node in ast.walk(tree)
                               if isinstance(node, ast.Constant) and isinstance(node.value, str))
        labels = {label for topic in help_content.TOPICS for text in self.texts(topic)
                  for label in re.findall(r"\[([^\[\]]+)\]", text)}
        self.assertGreater(len(labels), 10)
        # A word from the deck's display must be one of its modes exactly: "ERROR" is also part of
        # every "ERROR:" line jobs.py reads, so a substring match could never fail for it.
        display = {words for words, _speed in deck.MODES.values()} if deck else None

        def on_screen(label):
            if label.isupper() and display is not None:
                return label in display
            return any(label in s for s in strings)

        missing = sorted(label for label in labels if not on_screen(label))
        self.assertEqual(missing, [], "the Help tab names things that are no longer on screen")

    def test_a_button_the_text_points_to_is_under_that_answer(self):
        # A button under an answer can't be marked [Label]: its words live only in ACTION_LABELS, in the
        # one file the test above doesn't read. So text that names one says "the button below that says",
        # and that button has to be under that answer.
        pointed = 0
        for topic in help_content.TOPICS:
            below = [help_content.ACTION_LABELS[key] for key in topic["actions"]]
            for text in self.texts(topic):
                for label in re.findall(r"button below that says (.+?)[.,]", text):
                    self.assertIn(label, below, topic["title"])
                    pointed += 1
        self.assertGreater(pointed, 0)

    def test_help_waits_for_the_line_the_update_log_really_ends_with(self):
        topic = next(t for t in help_content.TOPICS if t["title"] == "What is the red warning?")
        self.assertIn(f"says {help_content.UPDATES_FINISHED},", " ".join(self.texts(topic)))
        app_source = (ROOT / "tapewright" / "app.py").read_text(encoding="utf-8")
        self.assertIn("help_content.UPDATES_FINISHED", app_source)

    def test_the_installer_is_named_as_the_release_page_lists_it(self):
        # Windows' warning shows the file's name, and the answer asks people to check it there.
        for title in ("Windows warned me about it", "Updating Tapewright"):
            topic = next(t for t in help_content.TOPICS if t["title"] == title)
            self.assertIn("TapewrightSetup.exe", " ".join(self.texts(topic)), title)
            self.assertIn("release_page", topic["actions"], title)

    def test_the_browser_steps_are_the_ones_microsoft_documents_for_edge(self):
        # Microsoft Learn gives Edge's way to keep a download it doesn't know as the three dots beside it,
        # then Keep, Show more and Keep anyway. Help, README and the release notes each tell people how to
        # download the installer, so each names all three, in that order.
        topic = next(t for t in help_content.TOPICS if t["title"] == "Updating Tapewright")
        places = {
            "Help": " ".join(self.texts(topic)),
            "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
            "release-notes.md": (ROOT / "packaging" / "release-notes.md").read_text(encoding="utf-8"),
        }
        for name, text in places.items():
            self.assertIn("Microsoft Edge", text, name)
            after = text[text.index("Microsoft Edge"):]
            self.assertEqual(re.findall(r"Keep anyway|Show more|Keep", after)[:3],
                             ["Keep", "Show more", "Keep anyway"], name)

    def test_a_blocked_installer_points_to_the_source_and_promises_no_signed_version(self):
        # Nobody can say when, or whether, the installer will be signed. Where Smart App Control leaves
        # only OK, the way on is running Tapewright from source, never waiting for a signature.
        topic = next(t for t in help_content.TOPICS if t["title"] == "Windows warned me about it")
        self.assertIn("source code", " ".join(self.texts(topic)))
        places = {
            "Help": " ".join(text for topic in help_content.TOPICS for text in self.texts(topic)),
            "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
            "release-notes.md": (ROOT / "packaging" / "release-notes.md").read_text(encoding="utf-8"),
        }
        for name, text in places.items():
            self.assertNotIn("wait for a signed", text.lower(), name)

    @unittest.skipIf(tkinter is None, "a Python built without Tk")
    def test_every_action_has_a_handler(self):
        from tapewright import help_tab  # here, not at the top, so only this test fails if it can't import
        self.assertEqual(set(help_tab.HelpTab.HANDLERS), set(help_content.ACTION_LABELS))
        for method in help_tab.HelpTab.HANDLERS.values():
            self.assertTrue(callable(getattr(help_tab.HelpTab, method, None)), method)


class License(unittest.TestCase):
    def test_every_source_file_names_the_license(self):
        self.assertTrue((ROOT / "LICENSE").is_file())
        sources = [*ROOT.glob("tapewright/*.py"), *ROOT.glob("tests/*.py"), *ROOT.glob("packaging/*.py"),
                   *ROOT.glob("packaging/*.iss"), *ROOT.glob(".github/workflows/*.yml"),
                   *ROOT.glob(".signpath/artifact-configurations/*.xml"), ROOT / "Tapewright.pyw"]
        missing = [p.relative_to(ROOT).as_posix() for p in sorted(sources)
                   if "SPDX-License-Identifier: GPL-3.0-or-later" not in p.read_text(encoding="utf-8")[:400]]
        self.assertEqual(missing, [], "start these files with the two SPDX lines (see AGENTS.md)")


if __name__ == "__main__":
    unittest.main()
