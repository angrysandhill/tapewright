# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""Everything that needs no network and no installed tools. The window is only ever built withdrawn.

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
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
        args = deps._winget_command("winget", "upgrade", "Gyan.FFmpeg")
        self.assertEqual(args[:5], ["winget", "upgrade", "--id", "Gyan.FFmpeg", "--exact"])
        for flag in ("--disable-interactivity", "--accept-source-agreements", "--accept-package-agreements"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--source") + 1], "winget")
        self.assertNotIn("--no-upgrade", args)
        self.assertEqual(deps._winget_command("winget", "install", "DenoLand.Deno")[-1], "--no-upgrade")
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
        missing_deno = deps.Dep("js", "deno", False, action="Install deno",
                                command=deps._winget_command("winget", "install", "DenoLand.Deno"))
        self.assertTrue(deps.describe_update(missing_deno).startswith(
            "Install the YouTube helper (deno) with winget"))
        winget = deps.Dep("ffmpeg", "FFmpeg", True, action="Update",
                          command=deps._winget_command("winget", "upgrade", "Gyan.FFmpeg"))
        text = deps.describe_update(winget)
        self.assertIn("Converter (FFmpeg)", text)
        self.assertIn("terms", text)  # winget accepts them for the user, so the box has to say so
        deno = deps.Dep("js", "deno", False, action="Update",
                        command=[r"C:\Users\x\.deno\bin\deno.exe", "upgrade"])
        self.assertIn("deno's own updater", deps.describe_update(deno))
        for dep in (pip, install, stable, missing_deno, winget, deno):
            self.assertNotIn("--", deps.describe_update(dep))

    def test_labels_say_what_each_tool_is_for(self):
        self.assertEqual(deps.Dep("yt-dlp", "yt-dlp", True).label, "Downloader (yt-dlp)")
        self.assertEqual(deps.Dep("js", "Node.js", False).label, "YouTube helper (Node.js)")
        self.assertEqual(deps.Dep("other", "Other", False).label, "Other")

    def test_a_missing_youtube_helper_is_named_after_what_would_fill_the_gap(self):
        with mock.patch.object(deps, "find_tool", return_value=""):
            with mock.patch.object(deps, "_winget", return_value="winget"):
                dep, _ = deps.check_js("auto", {}, False)
                self.assertEqual((dep.label, dep.action), ("YouTube helper (deno)", "Install deno"))
                dep, _ = deps.check_js("node", {}, False)
                self.assertEqual((dep.label, dep.command), ("YouTube helper (Node.js)", []))
            with mock.patch.object(deps, "_winget", return_value=""):
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
        upgrade = deps._winget_command("winget", "upgrade", "Gyan.FFmpeg")
        behind = deps.Dep("ffmpeg", "FFmpeg", True, state=deps.OUTDATED, command=upgrade)
        self.assertIn("try again later", deps.update_failure_text(behind, deps.WINGET_UPDATE_NOT_APPLICABLE))
        # A current FFmpeg with its ffprobe gone gets the same code, and waiting would never fix it.
        broken = deps.Dep("ffmpeg", "FFmpeg", True, state=deps.MISSING, command=upgrade)
        text = deps.update_failure_text(broken, deps.WINGET_UPDATE_NOT_APPLICABLE - 2 ** 32)
        self.assertNotIn("try again later", text)
        self.assertIn("winget uninstall --id Gyan.FFmpeg", text)
        self.assertTrue(text.endswith("(winget exit code 0x8A15002B)"), text)
        too_old = deps.Dep("js", "deno", False, state=deps.UNSUPPORTED,
                           command=deps._winget_command("winget", "upgrade", "DenoLand.Deno"))
        text = deps.update_failure_text(too_old, deps.WINGET_UPDATE_NOT_APPLICABLE)
        self.assertNotIn("try again later", text)
        self.assertIn("winget uninstall --id DenoLand.Deno", text)
        # winget still has a record of a tool whose files are gone: reopening can't fix that alone.
        gone = deps.Dep("ffmpeg", "FFmpeg", True, state=deps.MISSING,
                        command=deps._winget_command("winget", "install", "Gyan.FFmpeg"))
        text = deps.update_failure_text(gone, deps.WINGET_ALREADY_INSTALLED)
        self.assertIn("open it again", text)
        self.assertIn("winget uninstall --id Gyan.FFmpeg", text)
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


class FindTool(unittest.TestCase):
    """Where tools are found on a pretend Windows profile, with nothing on PATH."""

    WINGET_SOURCE = "_Microsoft.Winget.Source_8wekyb3d8bbwe"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.packages = self.root / "Microsoft" / "WinGet" / "Packages"
        for patch in (mock.patch.object(deps, "WINDOWS", True),
                      mock.patch.object(deps.shutil, "which", return_value=None),
                      mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}),
                      mock.patch.object(deps.Path, "home", return_value=self.root / "home")):
            patch.start()
            self.addCleanup(patch.stop)

    def touch(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    def test_versioned_folders_sort_as_versions_whatever_order_they_arrive_in(self):
        def exe(folder):
            return Path("Packages") / "Gyan.FFmpeg" / folder / "bin" / "ffmpeg.exe"

        newest_first = [exe("ffmpeg-10.1-full_build"), exe("ffmpeg-10.0-full_build"),
                        exe("ffmpeg-9.0.1-full_build"), exe("unversioned")]
        self.assertGreater("ffmpeg-9.0.1-full_build", "ffmpeg-10.1-full_build")  # by name, 9.0.1 would win
        for order in itertools.permutations(newest_first):
            self.assertEqual(deps._newest_first(order), newest_first)

    def test_the_newest_ffmpeg_folder_is_the_one_found(self):
        package = self.packages / f"Gyan.FFmpeg{self.WINGET_SOURCE}"
        for version in ("9.0.1", "10.1", "10.0"):
            self.touch(package / f"ffmpeg-{version}-full_build" / "bin" / "ffmpeg.exe")
        newest = package / "ffmpeg-10.1-full_build" / "bin" / "ffmpeg.exe"
        self.assertEqual(deps.find_tool("ffmpeg"), str(newest))

    def test_a_winget_deno_with_no_link_is_found_before_the_home_copy(self):
        self.touch(self.root / "home" / ".deno" / "bin" / "deno.exe")
        packaged = self.touch(self.packages / f"DenoLand.Deno{self.WINGET_SOURCE}" / "deno.exe")
        self.assertEqual(deps.find_tool("deno"), str(packaged))
        packaged.unlink()
        self.assertEqual(deps.find_tool("deno"), str(self.root / "home" / ".deno" / "bin" / "deno.exe"))

    def test_ffprobe_beside_ffmpeg_comes_before_a_separate_search(self):
        bin_dir = self.packages / f"Gyan.FFmpeg{self.WINGET_SOURCE}" / "ffmpeg-9.0.1-full_build" / "bin"
        ffmpeg = self.touch(bin_dir / "ffmpeg.exe")
        ffprobe = self.touch(bin_dir / "ffprobe.exe")
        elsewhere = self.touch(self.root / "other" / "ffprobe.exe")
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

    def test_every_module_imports(self):
        for path in sorted((ROOT / "tapewright").glob("*.py")):
            if path.stem not in ("__init__", "__main__"):
                importlib.import_module(f"tapewright.{path.stem}")

    def test_a_tk_that_cannot_open_a_window_is_explained(self):
        error = self.tk.TclError("no display name and no $DISPLAY environment variable")
        # Of tkinter, only Tk is patched: the except clause reads tk.TclError, which must stay real.
        with mock.patch.object(self.app.tk, "Tk", side_effect=error), \
                mock.patch.object(self.app, "_enable_dpi_awareness"), \
                mock.patch.object(self.app.config, "Settings"), \
                mock.patch.object(self.app.launch, "explain") as explain:
            self.assertEqual(self.app.main(), 1)
        reason, fix = explain.call_args.args
        self.assertIn("no display name", reason)
        self.assertIn("desktop", fix)

    def open_window(self):
        """The real App on a withdrawn root, with yt-dlp and FFmpeg out of date and no checks run."""
        try:
            root = self.tk.Tk()
        except self.tk.TclError as e:
            if os.name == "nt" or os.environ.get("DISPLAY"):
                raise  # a broken Tk where a window can open is a failure, not a reason to skip
            self.skipTest(f"no display to open a window on: {e}")
        root.withdraw()
        self.addCleanup(root.destroy)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        no_checks = mock.patch.object(self.app.App, "check_dependencies")  # no tools, no network
        no_checks.start()
        self.addCleanup(no_checks.stop)
        app = self.app.App(root, config.Settings(Path(folder.name) / "settings.json"))
        ytdlp = deps.Dep("yt-dlp", "yt-dlp", True, state=deps.OUTDATED, installed="2026.8.19",
                         latest="2026.9.10", action="Update",
                         command=deps.ytdlp_install_command("2026.9.10", True))
        ffmpeg = deps.Dep("ffmpeg", "FFmpeg", True, state=deps.OUTDATED, installed="9.0.1", latest="9.1",
                          action="Update", command=deps._winget_command("winget", "upgrade", "Gyan.FFmpeg"))
        js = deps.Dep("js", "deno", False, state=deps.OK, installed="2.9.6")
        app._check_finished({"yt-dlp": ytdlp, "ffmpeg": ffmpeg, "js": js}, {}, False)
        root.update_idletasks()
        return app

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
        app.cancel_update()
        self.assertEqual(app._update_queue, [])
        self.assertEqual(app.settings_tab.logview.contents().splitlines()[-1],
                         "Update cancelled. Not updated: the Converter (FFmpeg).")
        app._update_finished(ytdlp, 0, "")
        self.assertEqual(app.settings_tab.logview.contents().splitlines()[-1], "Downloader (yt-dlp): done.")
        # And a new batch starts clean, so its end is announced again.
        with mock.patch.object(app, "_next_update"):
            self.assertTrue(app.run_updates(["yt-dlp"], confirm=False))
        self.assertFalse(app._update_cancelled)


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

    def test_help_waits_for_the_line_the_update_log_really_ends_with(self):
        topic = next(t for t in help_content.TOPICS if t["title"] == "What is the red warning?")
        self.assertIn(f"says {help_content.UPDATES_FINISHED},", " ".join(self.texts(topic)))
        app_source = (ROOT / "tapewright" / "app.py").read_text(encoding="utf-8")
        self.assertIn("help_content.UPDATES_FINISHED", app_source)

    @unittest.skipIf(tkinter is None, "a Python built without Tk")
    def test_every_action_has_a_handler(self):
        from tapewright import help_tab  # here, not at the top, so only this test fails if it can't import
        self.assertEqual(set(help_tab.HelpTab.HANDLERS), set(help_content.ACTION_LABELS))
        for method in help_tab.HelpTab.HANDLERS.values():
            self.assertTrue(callable(getattr(help_tab.HelpTab, method, None)), method)


class License(unittest.TestCase):
    def test_every_source_file_names_the_license(self):
        self.assertTrue((ROOT / "LICENSE").is_file())
        sources = [*ROOT.glob("tapewright/*.py"), *ROOT.glob("tests/*.py"),
                   *ROOT.glob(".github/workflows/*.yml"), ROOT / "Tapewright.pyw"]
        missing = [p.relative_to(ROOT).as_posix() for p in sorted(sources)
                   if "SPDX-License-Identifier: GPL-3.0-or-later" not in p.read_text(encoding="utf-8")[:400]]
        self.assertEqual(missing, [], "start these files with the two SPDX lines (see AGENTS.md)")


if __name__ == "__main__":
    unittest.main()
