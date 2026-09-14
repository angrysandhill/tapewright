# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""The parts with no window and no network.

    python -m unittest discover -s tests -v
"""

import ast
import datetime
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tapewright import config, deps, help_content, jobs, procs, versions  # noqa: E402

try:  # both draw with Tk; a Python built without it can still run every other test
    from tapewright import deck, theme  # noqa: E402
except ImportError:
    deck = theme = None


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
    ROOT = Path(__file__).resolve().parents[1]

    @unittest.skipIf(theme is None, "a Python built without Tk")
    def test_every_listed_pair_is_readable(self):
        self.assertAlmostEqual(theme.contrast("#000000", "#ffffff"), 21.0)
        for foreground, background, minimum in theme.CONTRAST:
            ratio = theme.contrast(theme.C[foreground], theme.C[background])
            self.assertGreaterEqual(ratio, minimum, f"{foreground} on {background} is only {ratio:.2f}:1")

    def test_colors_are_only_spelled_out_in_theme(self):
        found = []
        for path in (self.ROOT / "tapewright").glob("*.py"):
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


class Help(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

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
        for path in (self.ROOT / "tapewright").glob("*.py"):
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

    def test_every_action_has_a_handler(self):
        try:
            from tapewright import help_tab
        except ImportError as e:  # a Python built without Tk
            self.skipTest(str(e))
        self.assertEqual(set(help_tab.HelpTab.HANDLERS), set(help_content.ACTION_LABELS))
        for method in help_tab.HelpTab.HANDLERS.values():
            self.assertTrue(callable(getattr(help_tab.HelpTab, method, None)), method)


class License(unittest.TestCase):
    def test_every_source_file_names_the_license(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "LICENSE").is_file())
        sources = [*root.glob("tapewright/*.py"), *root.glob("tests/*.py"), root / "Tapewright.pyw"]
        missing = [p.relative_to(root).as_posix() for p in sorted(sources)
                   if "SPDX-License-Identifier: GPL-3.0-or-later" not in p.read_text(encoding="utf-8")[:400]]
        self.assertEqual(missing, [], "start these files with the two SPDX lines (see AGENTS.md)")


if __name__ == "__main__":
    unittest.main()
