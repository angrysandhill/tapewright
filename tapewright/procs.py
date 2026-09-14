# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""External tools as child processes.

Three things here are load-bearing on Windows, and each fails silently when missing (see
AGENTS.md): the UTF-8 environment, the hidden-window flags, and killing the whole process
tree rather than only the process Popen knows about.
"""

import os
import re
import shlex
import signal
import subprocess
import sys
import threading
from pathlib import Path

WINDOWS = os.name == "nt"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
_EOL = re.compile(rb"\r\n|\r|\n")


class Cancelled(Exception):
    """The job was cancelled, and whatever it was running has been killed."""


def python_exe():
    """The console interpreter of the Python running this app.

    Launched from Tapewright.pyw, sys.executable is pythonw.exe. python.exe beside it is the
    same installation, so `-m yt_dlp` and `-m pip` see the same packages, and
    CREATE_NO_WINDOW keeps its console out of sight.
    """
    exe = Path(sys.executable)
    if WINDOWS and exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.is_file():
            return str(console)
    return str(exe)


def child_env(extra_path=()):
    env = dict(os.environ)
    # yt-dlp is a Python child writing to a pipe. Left on Windows' ANSI code page it drops
    # the characters it cannot encode from the paths it prints -- no replacement character,
    # just gone -- so a title containing a full-width bar came back as a path that does not
    # exist. Measured, not assumed.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # A Python child writing to a pipe block-buffers: progress would arrive all at once, at
    # the end, which looks exactly like a hang.
    env["PYTHONUNBUFFERED"] = "1"
    dirs = [str(d) for d in extra_path if d]
    if dirs:
        env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
    return env


def _spawn_kwargs(extra_path=()):
    kw = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "env": child_env(extra_path),
    }
    if WINDOWS:
        kw["creationflags"] = _NO_WINDOW | _NEW_GROUP
    else:
        kw["start_new_session"] = True
    return kw


def format_command(args):
    """The command as someone would paste it into a terminal to reproduce a failure."""
    args = [str(a) for a in args]
    return subprocess.list2cmdline(args) if WINDOWS else shlex.join(args)


def run_capture(args, timeout=30, extra_path=()):
    """Run to completion and return (returncode, output). returncode is None if it never ran."""
    args = [str(a) for a in args]
    name = Path(args[0]).name
    try:
        done = subprocess.run(args, timeout=timeout, **_spawn_kwargs(extra_path))
    except FileNotFoundError:
        return None, f"{name} was not found"
    except subprocess.TimeoutExpired:
        return None, f"{name} gave no answer within {timeout}s"
    except OSError as e:
        return None, f"could not run {name}: {e}"
    return done.returncode, done.stdout.decode("utf-8", "replace")


def kill_tree(popen):
    """Kill a process and everything it started.

    shell=False keeps cmd.exe out of it, but yt-dlp still runs ffmpeg and deno as children
    of its own. Terminating only the Popen leaves an ffmpeg writing the file after the UI
    has said "Cancelled".
    """
    if popen.poll() is not None:
        return
    if WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(popen.pid)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    else:
        try:
            os.killpg(popen.pid, signal.SIGKILL)  # start_new_session made it a group leader
        except (ProcessLookupError, PermissionError):
            pass
    try:
        popen.wait(timeout=10)
    except subprocess.TimeoutExpired:
        popen.kill()


def iter_lines(stream):
    """Decoded lines from a binary stream, ending at LF, CRLF or a bare CR.

    pip, deno upgrade and winget redraw their progress with a bare CR. Splitting on LF alone
    would hold every redraw back until the command finished.
    """
    buf = b""
    while True:
        chunk = stream.read1(65536)
        if not chunk:
            break
        buf += chunk
        hold = b""
        if buf.endswith(b"\r"):  # possibly the first half of a CRLF split across reads
            buf, hold = buf[:-1], b"\r"
        *lines, buf = _EOL.split(buf)
        buf += hold
        for line in lines:
            yield line.decode("utf-8", "replace")
    buf = buf.rstrip(b"\r")
    if buf:
        yield buf.decode("utf-8", "replace")


class Runner:
    """Runs one job's processes, one after another, and lets another thread cancel it.

    Starting a process and cancelling take the same lock, so a cancel cannot land between
    "checked the flag" and "Popen returned" and leave a process that nothing will kill.
    """

    def __init__(self, extra_path=()):
        self.extra_path = list(extra_path)
        self._lock = threading.Lock()
        self._cancelled = False
        self._popen = None

    def cancel(self):
        with self._lock:
            self._cancelled = True
            popen = self._popen
        if popen is not None:
            kill_tree(popen)

    def run(self, args, on_line):
        """Run args to completion, passing each output line to on_line. Returns the exit code.

        Raises Cancelled if the job was cancelled before or during the run, and RuntimeError
        if the program could not be started at all.
        """
        args = [str(a) for a in args]
        with self._lock:
            if self._cancelled:
                raise Cancelled()
            try:
                popen = subprocess.Popen(args, **_spawn_kwargs(self.extra_path))
            except OSError as e:
                raise RuntimeError(f"Could not start {Path(args[0]).name}: {e}") from e
            self._popen = popen
        try:
            for line in iter_lines(popen.stdout):
                on_line(line)
            code = popen.wait()
        except BaseException:
            kill_tree(popen)
            raise
        finally:
            popen.stdout.close()
            with self._lock:
                self._popen = None
        if self._cancelled:
            raise Cancelled()
        return code
