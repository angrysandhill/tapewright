# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""Start the window, or say in plain words why this Python can't run it.

Tapewright.pyw, `python -m tapewright` and the installed `tapewright` command all start here.
Double-clicked, Tapewright.pyw runs under pythonw.exe, which has no console, so a Python too old
for the app, or one without Tkinter, would otherwise fail with nothing on screen at all.
app.main() uses explain() and tk_failure_fix() too, for a Tk that imports but can't open a window.

This file, tapewright/__init__.py, tapewright/__main__.py and Tapewright.pyw run before the
version check can, so they must parse and import on any Python 3: no f-strings, annotations,
walrus, `match`, positional-only parameters, numeric underscores or parenthesized `with`.
tests/test_core.py checks for the newer syntax most likely to slip in.
"""

import os
import sys

# The same floor as requires-python in pyproject.toml and the oldest Python CI runs; change them together.
MINIMUM = (3, 10)
DOWNLOADS = "https://www.python.org/downloads/"


def problem():
    """(why this Python can't run Tapewright, what to do about it), or None when it can.

    The reason finishes the sentence "Tapewright can't start, because ...".
    """
    linux = sys.platform.startswith("linux")
    if tuple(sys.version_info[:2]) < MINIMUM:
        reason = "it needs Python %d.%d or newer, and this copy of Python is %d.%d." % (
            MINIMUM[0], MINIMUM[1], sys.version_info[0], sys.version_info[1])
        if linux:
            return reason, "Install Python %d.%d or newer, then open Tapewright again." % MINIMUM
        return reason, "Install the latest Python from %s, then open Tapewright again." % DOWNLOADS
    try:
        import tkinter  # noqa: F401  (only whether it imports matters here)
    except ImportError as e:
        reason = "this copy of Python has no Tkinter, which Tapewright draws its window with (%s)." % e
        if linux:
            return reason, ("Install your distribution's Tkinter package, such as python3-tk on "
                            "Debian and Ubuntu, then open Tapewright again.")
        return reason, ("Install the latest Python from %s, which includes Tkinter, then open "
                        "Tapewright again." % DOWNLOADS)
    return None


def tk_failure_fix(error):
    """What to do when tkinter imports but Tk() can't open a window, chosen from the cause.

    Reinstalling Python fixes a broken Tcl/Tk, but not a missing display, and not a TCL_LIBRARY
    or TK_LIBRARY left set by other software, which survives any reinstall.
    """
    if "display" in error.lower():
        return ("Tapewright needs a desktop to show its window. Start it from a desktop session, "
                "not from a remote terminal.")
    stray = [name for name in ("TCL_LIBRARY", "TK_LIBRARY") if os.environ.get(name)]
    if stray:
        one = len(stray) == 1
        return ("The environment %s %s, probably left by another program, may be pointing Tkinter "
                "at the wrong files. Removing %s usually fixes this. Then open Tapewright again." % (
                    "variable" if one else "variables", " and ".join(stray), "it" if one else "them"))
    return "Reinstalling Python usually fixes this. Then open Tapewright again."


def explain(reason, fix):
    """Say why Tapewright can't start: on stderr when there is one, and in a message box on Windows."""
    text = "Tapewright can't start, because %s\n\n%s\n\nPython in use: %s (version %s)" % (
        reason, fix, sys.executable or "unknown", sys.version.split()[0])
    if sys.stderr is not None:  # None under pythonw.exe
        try:
            sys.stderr.write(text + "\n")
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes
            MB_ICONERROR, MB_SETFOREGROUND = 0x10, 0x10000
            ctypes.windll.user32.MessageBoxW(None, text, "Tapewright", MB_ICONERROR | MB_SETFOREGROUND)
        except Exception:
            pass


def main():
    found = problem()
    if found:
        explain(*found)
        return 1
    from tapewright.app import main as open_window
    return open_window()
