# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""What each conversion runs.

Every yt-dlp flag and FFmpeg option the app passes is spelled out in this file and nowhere
else. When a tool release changes one, this is the file that changes, and the command-line
checks in tests/test_core.py make sure the change is a deliberate one.

Nothing here imports tkinter, so all of it runs headless. tests/test_core.py drives run_job()
with canned yt-dlp output; driving it against the real tools needs no window either.
"""

import collections
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from tapewright import procs

# Keys must match config.CHOICES; tests/test_core.py checks that they do.
MP3_QUALITY = {  # key: (label, yt-dlp --audio-quality, ffmpeg options)
    "V0": ("Best: VBR V0, about 245 kbps", "0", ["-q:a", "0"]),
    "V2": ("High: VBR V2, about 190 kbps", "2", ["-q:a", "2"]),
    "320k": ("320 kbps constant", "320K", ["-b:a", "320k"]),
    "192k": ("192 kbps constant", "192K", ["-b:a", "192k"]),
    "128k": ("128 kbps constant (smallest)", "128K", ["-b:a", "128k"]),
}
MP4_MODE = {
    "compatible": "Compatible: H.264 + AAC, plays almost everywhere",
    "best": "Best quality: keeps the source codecs (VP9/AV1/Opus may not play everywhere)",
}
MP4_HEIGHT = {
    "best": "No limit", "2160": "2160p (4K)", "1440": "1440p", "1080": "1080p",
    "720": "720p", "480": "480p", "360": "360p",
}

# yt-dlp output the app parses. Each progress update is one line in a fixed format, and the
# finished file's path arrives on a line of its own.
DL_MARK = "MGDL;"
PP_MARK = "MGPP;"
FILE_MARK = "MGFILE;"
NAME_MARK = "MGNAME;"
_DL_FIELDS = (
    "info.playlist_index", "info.n_entries", "info.vcodec", "info.acodec", "progress.status",
    "progress.downloaded_bytes", "progress.total_bytes", "progress.total_bytes_estimate",
    "progress.speed", "progress.eta",
)
DL_TEMPLATE = "download:" + DL_MARK + ";".join(f"%({name})s" for name in _DL_FIELDS)
PP_TEMPLATE = "postprocess:" + PP_MARK + "%(progress.postprocessor)s;%(progress.status)s"
# after_move comes after every post-processor, so this is the finished MP3 or MP4.
FILE_TEMPLATE = "after_move:" + FILE_MARK + "%(filepath)s"
# before_dl comes once for each video, before it downloads, so a playlist names every video in turn.
NAME_TEMPLATE = "before_dl:" + NAME_MARK + "%(title)s"

POSTPROCESSOR_NAMES = {
    "ExtractAudio": "Converting to MP3",
    "Merger": "Merging video and audio",
    "VideoRemuxer": "Remuxing into MP4",
    "VideoConvertor": "Converting to MP4",
    "FixupM3u8": "Repairing the stream",
    "Metadata": "Writing tags",
    "EmbedThumbnail": "Adding cover art",
    "ThumbnailsConvertor": "Converting the thumbnail",
    "MoveFiles": "Finishing",
}

_FFMPEG_COMMON = ["-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
                  "-progress", "pipe:1", "-nostats"]
ENCODE_VIDEO = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
ENCODE_AUDIO = ["-c:a", "aac", "-b:a", "192k"]

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_BARE_HOST = re.compile(r"^(?:[\w-]+\.)+[A-Za-z]{2,}/")
_KEY_VALUE = re.compile(r"^([a-z0-9_]+)=(.*)$")


class JobError(Exception):
    """A failure whose message is fit to show as it is."""


@dataclass
class Job:
    target: str        # "mp3" or "mp4"
    kind: str          # "url" or "file"
    source: str        # the URL, or the file's path
    out_dir: Path
    ffmpeg: str
    ffprobe: str
    mp3_quality: str = "V0"
    mp4_mode: str = "compatible"
    mp4_max_height: str = "1080"
    playlist: bool = False
    thumbnail: bool = True
    js_args: list = field(default_factory=list)


@dataclass
class Result:
    ok: bool
    message: str
    files: list = field(default_factory=list)
    cancelled: bool = False


def classify(text):
    """What the input box holds: ("url", url), ("file", Path) or ("error", message)."""
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':  # Explorer's "Copy as path" adds quotes
        s = s[1:-1].strip()
    if not s:
        return "error", "Paste a link, or choose a file with Browse."
    if _SCHEME.match(s):
        scheme = urlparse(s).scheme.lower()
        if scheme in ("http", "https"):
            return "url", s
        return "error", f"Only http:// and https:// links are supported, not {scheme}://."
    path = Path(s).expanduser()
    if path.is_file():
        return "file", path
    if path.is_dir():
        return "error", "That is a folder. Choose a file inside it, or paste a link."
    if _BARE_HOST.match(s):  # youtu.be/abc, www.youtube.com/watch?v=abc
        return "url", "https://" + s
    return "error", f"That isn't a link, and there is no file at:\n{s}"


def _num(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def human_size(n):
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.0f} {units[i]}" if i == 0 else f"{n:.1f} {units[i]}"


def human_time(seconds):
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def parse_download(line):
    """A DL_TEMPLATE line -> (fraction or None, status text). None for any other line."""
    if not line.startswith(DL_MARK):
        return None
    parts = line[len(DL_MARK):].split(";")
    if len(parts) != len(_DL_FIELDS):
        return None
    index, count, vcodec, acodec, status, done, total, estimate, speed, eta = parts
    has_video = vcodec not in ("none", "NA", "")
    has_audio = acodec not in ("none", "NA", "")
    if has_video and not has_audio:
        what = "video"
    elif has_audio and not has_video:
        what = "audio"
    else:
        what = "media"
    done_bytes = _num(done)
    size = _num(total) or _num(estimate)
    if status == "finished":
        fraction = 1.0
    elif done_bytes is not None and size:
        fraction = min(done_bytes / size, 1.0)
    else:
        fraction = None
    bits = []
    if fraction is not None:
        bits.append(f"{fraction * 100:.0f}%" + (f" of {human_size(size)}" if size else ""))
    elif done_bytes is not None:
        bits.append(human_size(done_bytes))
    if status != "finished":
        rate, left = _num(speed), _num(eta)
        if rate:
            bits.append(f"{human_size(rate)}/s")
        if left is not None:
            bits.append(f"{human_time(left)} left")
    text = f"Downloading {what}"
    if index.isdigit() and count.isdigit():
        text += f" ({index} of {count})"
    if bits:
        text += ": " + ", ".join(bits)
    return fraction, text


def parse_postprocess(line):
    """A PP_TEMPLATE line -> (readable step name, status). None for any other line."""
    if not line.startswith(PP_MARK):
        return None
    name, _, status = line[len(PP_MARK):].partition(";")
    return POSTPROCESSOR_NAMES.get(name, name), status


def ytdlp_command(job):
    args = [
        procs.python_exe(), "-m", "yt_dlp",
        "--newline", "--progress-delta", "0.5", "--color", "never",
        "--progress-template", DL_TEMPLATE,
        "--progress-template", PP_TEMPLATE,
        # --print implies --quiet unless told otherwise, and quiet hides the progress too.
        "--print", FILE_TEMPLATE, "--print", NAME_TEMPLATE, "--no-quiet",
        # The FFmpeg the Settings tab checked, not whichever one is first on PATH.
        "--ffmpeg-location", job.ffmpeg,
        "-P", str(Path(job.out_dir).resolve()), "-o", "%(title)s.%(ext)s",
        # Never convert over an existing file of the same name: a cancel halfway through
        # would leave a truncated copy where a good one used to be.
        "--no-post-overwrites",
        "--yes-playlist" if job.playlist else "--no-playlist",
        "--embed-metadata",
    ]
    if job.thumbnail:
        args.append("--embed-thumbnail")
    args += job.js_args
    if job.target == "mp3":
        quality = MP3_QUALITY[job.mp3_quality][1]
        # -t mp3's format choice: take audio that is already MP3 when a site offers it.
        args += ["-f", "ba[acodec^=mp3]/ba/b", "-x", "--audio-format", "mp3",
                 "--audio-quality", quality]
    else:
        res = "res" if job.mp4_max_height == "best" else f"res:{job.mp4_max_height}"
        args += ["--merge-output-format", "mp4", "--remux-video", "mp4"]
        if job.mp4_mode == "compatible":
            # -t mp4's sort order (yt-dlp 2026.08.19) with res moved ahead of quality.
            # YouTube's quality ranking follows resolution, so a cap placed after it never
            # applies: measured, res:360 in the preset's position downloaded 1080p.
            args += ["-S", f"vcodec:h264,lang,{res},quality,fps,hdr:12,acodec:aac"]
        elif res != "res":
            args += ["-S", res]
    args += ["--", job.source]
    return args


def ffprobe_command(job, src):
    return [job.ffprobe, "-v", "error", "-of", "json", "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,pix_fmt"
            ":stream_disposition=attached_pic", str(src)]


def mp3_command(job, src, audio_index, dst):
    return [job.ffmpeg, *_FFMPEG_COMMON, "-i", str(src), "-map", f"0:{audio_index}",
            "-c:a", "libmp3lame", *MP3_QUALITY[job.mp3_quality][2],
            "-map_metadata", "0", "-id3v2_version", "3", str(dst)]


def mp4_command(job, src, video_index, audio_index, codec_options, dst):
    maps = ["-map", f"0:{video_index}"]
    if audio_index is not None:
        maps += ["-map", f"0:{audio_index}"]
    return [job.ffmpeg, *_FFMPEG_COMMON, "-i", str(src), *maps, *codec_options,
            "-map_metadata", "0", "-movflags", "+faststart", str(dst)]


def mp4_plans(mode, video, audio):
    """The attempts for a local file -> MP4, in order, as (label, codec options)."""
    video_ok = video.get("codec_name") == "h264" and video.get("pix_fmt") in ("yuv420p", "yuvj420p")
    audio_ok = audio is None or audio.get("codec_name") == "aac"
    audio_encode = ENCODE_AUDIO if audio is not None else []
    reencode = ("Re-encoding to H.264 + AAC", ENCODE_VIDEO + audio_encode)
    copy = ("Copying the streams into MP4", ["-c", "copy"])
    if mode == "best":
        # Keep the codecs if an MP4 can hold them; if FFmpeg refuses, re-encode instead.
        return [copy, reencode]
    if video_ok and audio_ok:
        return [copy]
    if video_ok:
        return [("Re-encoding the audio to AAC", ["-c:v", "copy"] + audio_encode)]
    if audio_ok:
        audio_copy = ["-c:a", "copy"] if audio is not None else []
        return [("Re-encoding the video to H.264", ENCODE_VIDEO + audio_copy)]
    return [reencode]


def reserve_output(folder, stem, ext):
    """Create an empty output file that did not exist before, and return its path.

    Creating it exclusively is the reservation. FFmpeg then overwrites a file this job owns,
    and cleanup after a failure or a cancel can delete it with no chance that it was someone
    else's -- including the source, when a .mp4 is converted to .mp4 in its own folder.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(10000):
        path = folder / (f"{stem}.{ext}" if n == 0 else f"{stem} ({n}).{ext}")
        try:
            with open(path, "xb"):
                return path
        except FileExistsError:
            continue
    raise JobError(f"Couldn't find a free file name for {stem}.{ext} in {folder}.")


def _discard(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


# Lines where yt-dlp names a file it is about to write. This is human-readable output, not a
# contract, so a wording change can only make cleanup miss a file -- never delete a wrong one.
_ANNOUNCED = (
    re.compile(r"^\[(?:download|ExtractAudio|VideoRemuxer|VideoConvertor)\] Destination: (.+)$"),
    re.compile(r'^\[Merger\] Merging formats into "(.+)"$'),
    re.compile(r"^\[info\] Writing video thumbnail .* to: (.+)$"),
)
_THUMBNAIL_CONVERT = re.compile(r'^\[ThumbnailsConvertor\] Converting thumbnail "(.+)" to (\w+)$')


def announced_path(line):
    """The file a yt-dlp output line says is being written, or None."""
    for pattern in _ANNOUNCED:
        m = pattern.match(line)
        if m:
            return Path(m.group(1).strip())
    m = _THUMBNAIL_CONVERT.match(line)
    return Path(m.group(1)).with_suffix("." + m.group(2)) if m else None


def remove_unfinished(announced, keep):
    """Delete what yt-dlp started writing during this job and did not finish.

    announced maps each path to whether it already existed at the moment yt-dlp named it,
    and a file that existed before is never touched. Along with each path go the
    companions yt-dlp writes next to it: .part and .part-FragN while downloading, .ytdl for
    resume state, and NAME.temp.EXT while a post-processor rewrites a file.
    Returns the names that were removed.
    """
    removed = []
    for path, existed in announced.items():
        if existed or path in keep:
            continue
        victims = [path, path.with_name(f"{path.stem}.temp{path.suffix}")]
        try:
            victims += [path.parent / name for name in os.listdir(path.parent)
                        if name.startswith(path.name + ".part") or name == path.name + ".ytdl"]
        except OSError:
            pass
        for victim in victims:
            for _ in range(5):  # a process killed a moment ago can still hold the handle
                try:
                    if victim.is_file():
                        victim.unlink()
                        removed.append(victim.name)
                    break
                except OSError:
                    time.sleep(0.2)
    return sorted(set(removed))


def _saved_message(files):
    return f"Saved {files[0].name}" if len(files) == 1 else f"Saved {len(files)} files"


def run_job(job, runner, emit):
    """Run one job to the end and return a Result.

    emit(kind, value) reports along the way: "log" (a line), "status" (a sentence),
    "progress" (0..1, or None when there is no way to know), "file" (a finished Path),
    "phase" ("load", "play" or "record": what the tape deck shows) and "name" (the title of
    the video about to download, for the cassette label).
    Cancelling, a tool failing, or a folder that can't be written all come back as a
    Result; only a bug in this module raises.
    """
    try:
        if job.kind == "url":
            return _run_download(job, runner, emit)
        return _run_local(job, runner, emit)
    except procs.Cancelled:
        return Result(False, "Cancelled.", cancelled=True)
    except (JobError, RuntimeError) as e:
        return Result(False, str(e))
    except OSError as e:
        return Result(False, f"{e.strerror or e}: {e.filename or ''}".rstrip(": "))


def _run_download(job, runner, emit):
    args = ytdlp_command(job)
    files, errors, announced, present = [], [], {}, []
    emit("status", "Contacting the site…")
    emit("progress", None)
    emit("phase", "load")
    emit("log", "$ " + procs.format_command(args))

    def on_line(line):
        download = parse_download(line)
        if download is not None:
            fraction, text = download
            emit("phase", "play")
            emit("progress", fraction)
            emit("status", text)
            return
        step = parse_postprocess(line)
        if step is not None:
            name, status = step
            if status != "finished":
                emit("phase", "record")
                emit("progress", None)
                emit("status", name + "…")
            return
        if line.startswith(NAME_MARK):
            emit("name", line[len(NAME_MARK):].strip())
            return
        if line.startswith(FILE_MARK):
            path = Path(line[len(FILE_MARK):].strip())
            files.append(path)
            emit("file", path)
            emit("log", f"Saved {path}")
            return
        path = announced_path(line)
        if path is not None and path not in announced:
            announced[path] = path.exists()  # checked the moment yt-dlp names it
        if line.startswith("ERROR:"):
            errors.append(line[len("ERROR:"):].strip())
        elif line.startswith("[download]") and line.endswith("has already been downloaded"):
            present.append(line)
        if line.strip():
            emit("log", line)

    def clean_up():
        # A cancelled or failed download leaves a truncated MP3 that looks finished.
        removed = remove_unfinished(announced, set(files))
        if removed:
            emit("log", "Removed unfinished files: " + ", ".join(removed))

    try:
        code = runner.run(args, on_line)
    except procs.Cancelled:
        clean_up()
        raise
    if code == 0 and files:
        # after_move reports a file that was already there exactly like a new one, so
        # "Saved" would claim work that never happened.
        already = min(len(present), len(files))
        if already == len(files):
            message = (f"Already in the folder: {files[0].name}" if len(files) == 1
                       else f"All {len(files)} files were already in the folder")
        elif already:
            message = f"Saved {len(files) - already} new file(s); {already} were already in the folder"
        else:
            message = _saved_message(files)
        return Result(True, message, files)
    if code == 0:
        return Result(True, "yt-dlp finished without reporting a new file. See the log.")
    clean_up()
    return Result(False, errors[-1] if errors else f"yt-dlp exited with code {code}.", files)


def _probe(job, runner, src):
    lines = []
    code = runner.run(ffprobe_command(job, src), lines.append)
    text = "\n".join(lines)
    start, end = text.find("{"), text.rfind("}")
    if code != 0 or start < 0:
        last = next((line.strip() for line in reversed(lines) if line.strip()), "")
        raise JobError("FFmpeg couldn't read this file" + (f": {last}" if last else "."))
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        raise JobError("FFmpeg couldn't read this file (ffprobe's output made no sense).")
    return data.get("streams") or [], _num((data.get("format") or {}).get("duration"))


def _run_ffmpeg(args, runner, emit, duration, label):
    """Run one FFmpeg command with progress. Returns (exit code, last problem it reported)."""
    emit("status", label + "…")
    emit("progress", 0.0 if duration else None)
    emit("phase", "record")
    emit("log", "$ " + procs.format_command(args))
    state = {"t": None, "speed": None}
    problems = collections.deque(maxlen=8)

    def on_line(line):
        m = _KEY_VALUE.match(line)
        if m is None:
            if line.strip():
                problems.append(line.strip())
                emit("log", line)
            return
        key, value = m.groups()
        if key == "out_time_us":
            us = _num(value)
            state["t"] = us / 1e6 if us is not None else None
        elif key == "speed":
            state["speed"] = _num(value.strip().rstrip("x"))
        elif key == "progress":
            if value == "end":
                emit("progress", 1.0)
            elif duration and state["t"] is not None:
                fraction = min(state["t"] / duration, 1.0)
                text = f"{label}: {fraction * 100:.0f}% of {human_time(duration)}"
                if state["speed"]:
                    text += f", {human_time(max(duration - state['t'], 0) / state['speed'])} left"
                emit("progress", fraction)
                emit("status", text)

    code = runner.run(args, on_line)
    return code, (problems[-1] if problems else f"ffmpeg exited with code {code}")


def _run_local(job, runner, emit):
    src = Path(job.source)
    emit("status", "Reading the file…")
    emit("progress", None)
    emit("phase", "load")
    streams, duration = _probe(job, runner, src)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = [s for s in streams if s.get("codec_type") == "video"
             and not (s.get("disposition") or {}).get("attached_pic")]

    if job.target == "mp3":
        if not audio:
            raise JobError("This file has no audio track, so there is nothing to put in an MP3.")
        if len(audio) > 1:
            emit("log", f"This file has {len(audio)} audio tracks; converting the first.")
        attempts = [("Converting to MP3", mp3_command, (audio[0]["index"],))]
    else:
        if not video:
            raise JobError("This file has no video track. To keep only the audio, use the To MP3 tab.")
        first_audio = audio[0] if audio else None
        left_out = len(streams) - 1 - (1 if first_audio else 0)
        if left_out:
            emit("log", f"Keeping the first video and audio track; {left_out} other stream(s) "
                        "(subtitles, extra audio, cover art) are left out of the MP4.")
        audio_index = first_audio["index"] if first_audio else None
        attempts = [(label, mp4_command, (video[0]["index"], audio_index, options))
                    for label, options in mp4_plans(job.mp4_mode, video[0], first_audio)]

    dst = reserve_output(job.out_dir, src.stem, job.target)
    emit("log", f"Writing {dst}")
    finished = False
    try:
        for i, (label, build, extra) in enumerate(attempts):
            code, problem = _run_ffmpeg(build(job, src, *extra, dst), runner, emit, duration, label)
            if code == 0:
                finished = True
                break
            if i + 1 < len(attempts):
                emit("log", f"{label} failed ({problem}). Trying again with re-encoding.")
        if not finished:
            return Result(False, f"FFmpeg failed: {problem}")
    finally:
        if not finished:
            _discard(dst)
    emit("file", dst)
    return Result(True, _saved_message([dst]), [dst])
