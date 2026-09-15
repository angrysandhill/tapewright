# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""The steps that build Tapewright's Windows installer, run in this order by .github/workflows/release.yml:

    python packaging/build.py check-tag v0.2.0   the tag names this version of Tapewright (tag pushes only)
    python packaging/build.py versions           app=... and python=..., for Inno Setup's /D options
    python packaging/build.py runtime            python.org's runtime zip, checked twice, then unpacked
    python packaging/build.py check              that runtime's Python and Tk match the pin, and pip runs
    python packaging/build.py mark               the marker procs.bundled() looks for, after the tests
    python packaging/build.py stage              the files the installer puts in its app folder
    python packaging/build.py sums FILE...       dist/SHA256SUMS.txt

It runs on whatever Python the build machine has, so it uses the standard library only, and it never imports
tapewright: the version is read out of tapewright/__init__.py with ast. Importing this file does nothing;
tests/test_packaging.py tests its helpers without a network.
"""

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "packaging" / "runtime.json"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
INDEX_URL = "https://www.python.org/ftp/python/index-windows.json"
# pymanager splits that index into pages, each naming the next under "next" (its scripts/repartition-index.py
# writes them). These caps only stop a broken index from looping or filling memory.
INDEX_PAGES = 10
INDEX_LIMIT = 64 * 1024 * 1024
MARKER = "tapewright-runtime.txt"  # procs.RUNTIME_MARKER; a test keeps the two the same
SUMS = "SHA256SUMS.txt"
CHUNK = 1024 * 1024
TIMEOUT = 60
USER_AGENT = "Tapewright-build"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# What the runtime's own Python reports about itself, as one line of JSON. tkinter.Tcl() opens no window.
PROBE = ("import json, sys, tkinter; print(json.dumps({'python': list(sys.version_info[:3]), "
         "'tk': tkinter.TkVersion, 'patchlevel': tkinter.Tcl().eval('info patchlevel')}))")


class BuildError(Exception):
    """A step failed in a way its message explains, so main() prints it without a traceback."""


def read_version(path):
    """The string assigned to __version__ in a Python file, read without running the file."""
    for node in ast.parse(Path(path).read_text(encoding="utf-8")).body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__"
                        for target in node.targets)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            return node.value.value
    raise BuildError(f"{path} assigns no __version__ string")


def load_pin(path=PIN):
    """runtime.json, refused unless it holds exactly the fields the other steps use, each in its shape."""
    pin = json.loads(Path(path).read_text(encoding="utf-8"))
    shapes = {"version": r"\d+\.\d+\.\d+", "url": r"https://www\.python\.org/ftp/python/\S+\.zip",
              "sha256": r"[0-9a-f]{64}", "tk": r"\d+\.\d+"}
    if not isinstance(pin, dict) or set(pin) != set(shapes):
        raise BuildError(f"{path} must hold exactly these fields: {', '.join(sorted(shapes))}")
    for key, shape in shapes.items():
        if not isinstance(pin[key], str) or not re.fullmatch(shape, pin[key]):
            raise BuildError(f"{path}: {key} is {pin[key]!r}, which isn't in the shape it should be")
    return pin


def index_has(index_json, url, sha256):
    """True when one parsed page of python.org's index-windows.json lists url with that sha256.

    Its entries look like {"id": ..., "url": ..., "hash": {"sha256": ...}}, as pymanager reads them.
    """
    versions = index_json.get("versions") if isinstance(index_json, dict) else None
    for entry in versions if isinstance(versions, list) else ():
        digest = entry.get("hash") if isinstance(entry, dict) else None
        if (isinstance(digest, dict) and entry.get("url") == url
                and isinstance(digest.get("sha256"), str) and digest["sha256"].lower() == sha256.lower()):
            return True
    return False


def next_page(index_json, page_url):
    """The address of the index page after this one, or None.

    A page elsewhere doesn't count, because the point of reading the index is that python.org still says so.
    """
    following = index_json.get("next") if isinstance(index_json, dict) else None
    if not isinstance(following, str) or not following:
        return None
    joined = urllib.parse.urljoin(page_url, following)
    here, there = urllib.parse.urlsplit(page_url), urllib.parse.urlsplit(joined)
    return joined if (there.scheme, there.netloc) == (here.scheme, here.netloc) else None


def safe_members(names):
    """The zip's member names, unchanged, or BuildError naming the first that could land outside its folder.

    Windows takes either slash as a separator, a colon as a drive or a stream, and drops trailing dots and
    spaces from each part of a path, so ".. " is "..".
    """
    names = list(names)
    for name in names:
        parts = re.split(r"[\\/]", name)
        if (not name or "\0" in name or ":" in name or name[0] in "\\/"
                or any(part not in ("", ".") and not part.rstrip(". ") for part in parts)):
            raise BuildError(f"the zip holds {name!r}, which could land outside its folder")
    return names


def stage_list(root):
    """(source, destination) for each file of the installed app folder, destinations relative, with "/".

    The launcher, the package's modules, the license, the README and the icon the shortcuts show. Only
    tapewright/*.py is taken, so __pycache__ and tests/ stay out. A module in a folder below it would be left
    behind with no word, so one is refused until this list takes that folder too.
    """
    root = Path(root)
    package = root / "tapewright"
    nested = sorted(path.relative_to(root).as_posix()
                    for path in package.rglob("*.py") if path.parent != package)
    if nested:
        raise BuildError(f"stage copies only tapewright/*.py, and {nested[0]} is in a folder below it")
    pairs = [(root / "Tapewright.pyw", "Tapewright.pyw")]
    pairs += [(path, "tapewright/" + path.name) for path in sorted(package.glob("*.py"))]
    pairs += [(root / name, name) for name in ("LICENSE", "README.md")]
    pairs.append((root / "packaging" / "tapewright.ico", "tapewright.ico"))
    return pairs


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sums_text(paths):
    """SHA256SUMS.txt's text: a "<hex>  <name>" line for each file, the layout `sha256sum -c` reads."""
    lines, names = [], set()
    for path in map(Path, paths):
        if path.name in names:
            raise BuildError(f"two files are called {path.name}, and {SUMS} lists names without folders")
        names.add(path.name)
        lines.append(f"{sha256_of(path)}  {path.name}\n")
    return "".join(lines)


def findings(pin, answer, pip, names):
    """What check found, as (passed, sentence) pairs.

    answer is what the runtime's Python said about itself, {"python": [3, 14, 6], "tk": 8.6, "patchlevel":
    "8.6.15"}, or None when it didn't run; pip is the first line `-m pip --version` printed, or None when it
    failed; names are the names of the files in the runtime's top folder.
    """
    want = tuple(int(part) for part in pin["version"].split("."))
    answer = answer if isinstance(answer, dict) else {}
    python = tuple(answer.get("python") or ())
    tk = answer.get("tk")
    patchlevel = str(answer.get("patchlevel") or "")
    pth = sorted(name for name in names if name.lower().endswith("._pth"))
    return [
        (python == want, f"Python {'.'.join(map(str, python)) or 'did not run'}, pinned {pin['version']}"),
        (tk == float(pin["tk"]), f"tkinter.TkVersion {tk}, pinned {pin['tk']}"),
        (patchlevel.startswith(pin["tk"] + "."), f"Tcl/Tk patch level {patchlevel or 'unknown'}"),
        (pip is not None, f"pip: {pip or '-m pip --version failed'}"),
        ("pythonw.exe" in names, "pythonw.exe, which the shortcuts start"),
        ("LICENSE.txt" in names, "LICENSE.txt, Python's license"),
        # With a ._pth file, Python's search path is only what it lists, so pip's yt-dlp could go unread.
        (not pth, f"no ._pth file (found {', '.join(pth)})" if pth else "no ._pth file"),
    ]


def _open(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def download(url, dest, sha256, opener=None):
    """Streams url into dest, hashing as it goes, and returns its size.

    A SHA-256 other than sha256 is a BuildError and leaves no file behind.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest, size = hashlib.sha256(), 0
    try:
        with (opener or _open)(url) as response, open(part, "wb") as file:
            for chunk in iter(lambda: response.read(CHUNK), b""):
                digest.update(chunk)
                file.write(chunk)
                size += len(chunk)
        if digest.hexdigest() != sha256.lower():
            raise BuildError(f"{url} came with SHA-256 {digest.hexdigest()}, "
                             f"not the {sha256} runtime.json pins")
        os.replace(part, dest)
    finally:
        if part.exists():
            part.unlink()
    return size


def python_org_lists(pin, opener=None):
    """True when python.org's live index still lists the pinned zip with the pinned hash, on any page."""
    page, seen = INDEX_URL, set()
    while page and page not in seen and len(seen) < INDEX_PAGES:
        seen.add(page)
        with (opener or _open)(page) as response:
            body = response.read(INDEX_LIMIT + 1)
        if len(body) > INDEX_LIMIT:
            raise BuildError(f"{page} is over {INDEX_LIMIT // 2 ** 20} MB, more than any index should be")
        try:
            index = json.loads(body)
        except ValueError as error:
            raise BuildError(f"{page} isn't JSON: {error}") from None
        if index_has(index, pin["url"], pin["sha256"]):
            return True
        page = next_page(index, page)
    return False


def unpack(archive, out):
    """Extracts the zip into out once safe_members has passed every name. Returns how many files it held."""
    with zipfile.ZipFile(archive) as zipped:
        names = safe_members(zipped.namelist())
        zipped.extractall(out)
    return sum(1 for name in names if not name.endswith("/"))


def _fresh(folder):
    """Makes folder, refusing one that already holds something, so nothing left from before gets shipped."""
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        raise BuildError(f"{folder} already exists and isn't empty; delete it first")
    folder.mkdir(parents=True, exist_ok=True)


def _ask(exe, *args):
    """What the runtime's Python printed, or None when it failed.

    -I runs it the way the shortcuts do, with no PYTHON* variable and no user site-packages, so a pip found
    here is the runtime's own. -B keeps it from writing bytecode into the folder that ships.
    """
    command = [str(exe), "-I", "-B", *args]
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=120, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"     {' '.join(args)}: {error}")
        return None
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip()[-800:]
        print(f"     {' '.join(args)} exited with {done.returncode}: {detail}")
        return None
    return done.stdout


def cmd_check_tag(args):
    version = read_version(ROOT / "tapewright" / "__init__.py")
    if args.tag != "v" + version:
        raise BuildError(f"the tag {args.tag} isn't v{version}, the version tapewright/__init__.py says")
    print(f"ok   {args.tag} is Tapewright {version}")
    return 0


def cmd_versions(args):
    print("app=" + read_version(ROOT / "tapewright" / "__init__.py"))
    print("python=" + load_pin()["version"])
    return 0


def cmd_runtime(args):
    pin = load_pin()
    _fresh(args.out)
    archive = BUILD / "python.zip"
    print(f"Downloading {pin['url']}")
    size = download(pin["url"], archive, pin["sha256"])
    print(f"ok   {size:,} bytes with SHA-256 {pin['sha256']}, as runtime.json pins")
    if not python_org_lists(pin):
        raise BuildError(f"python.org's index-windows.json doesn't list {pin['url']} with that SHA-256: "
                         "the pin is no longer listed or its hash changed. Read the index before changing "
                         "runtime.json.")
    print("ok   python.org's index-windows.json lists that address with that SHA-256")
    print(f"ok   unpacked {unpack(archive, args.out):,} files into {args.out}")
    return 0


def cmd_check(args):
    pin = load_pin()
    exe = args.runtime / "python.exe"
    if not exe.is_file():
        raise BuildError(f"there is no python.exe in {args.runtime}; the runtime step makes it")
    output = _ask(exe, "-c", PROBE)
    try:
        answer = json.loads(output) if output else None
    except ValueError:
        print(f"     it printed {output.strip()[-800:]!r}, not a line of JSON")
        answer = None
    pip = (_ask(exe, "-m", "pip", "--version") or "").strip()
    results = findings(pin, answer, pip.splitlines()[0] if pip else None, set(os.listdir(args.runtime)))
    for passed, sentence in results:
        print(("ok   " if passed else "FAIL ") + sentence)
    return 0 if all(passed for passed, _ in results) else 1


def cmd_mark(args):
    pin = load_pin()
    if not (args.runtime / "python.exe").is_file():
        raise BuildError(f"there is no python.exe in {args.runtime} to mark")
    (args.runtime / MARKER).write_bytes(pin["version"].encode("ascii") + b"\n")
    print(f"ok   {args.runtime / MARKER} says {pin['version']}")
    return 0


def cmd_stage(args):
    pairs = stage_list(ROOT)
    _fresh(args.out)
    for source, destination in pairs:
        target = args.out / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    print(f"ok   {len(pairs)} files staged in {args.out}")
    return 0


def cmd_sums(args):
    text = sums_text(args.files)
    DIST.mkdir(parents=True, exist_ok=True)
    (DIST / SUMS).write_bytes(text.encode("utf-8"))  # "\n" line ends, which sha256sum -c expects
    print(text, end="")
    return 0


COMMANDS = {
    "check-tag": cmd_check_tag,
    "versions": cmd_versions,
    "runtime": cmd_runtime,
    "check": cmd_check,
    "mark": cmd_mark,
    "stage": cmd_stage,
    "sums": cmd_sums,
}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="build.py",
                                     description="Build steps for Tapewright's Windows installer.")
    steps = parser.add_subparsers(dest="command", required=True)
    steps.add_parser("check-tag", help="fail unless TAG is v and Tapewright's version").add_argument("tag")
    steps.add_parser("versions", help="print app=VERSION and python=VERSION")
    steps.add_parser("runtime", help="download, verify and unpack python.org's runtime").add_argument(
        "--out", type=Path, default=BUILD / "runtime")
    steps.add_parser("check", help="check the unpacked runtime").add_argument(
        "--runtime", type=Path, default=BUILD / "runtime")
    steps.add_parser("mark", help="mark the runtime as the installer's").add_argument(
        "--runtime", type=Path, default=BUILD / "runtime")
    steps.add_parser("stage", help="copy the app's files into a fresh folder").add_argument(
        "--out", type=Path, default=BUILD / "app")
    steps.add_parser("sums", help="write dist/SHA256SUMS.txt").add_argument("files", type=Path, nargs="+")
    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except BuildError as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
