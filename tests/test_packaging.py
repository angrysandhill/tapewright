# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Windows installer's build, with no network, no Inno Setup and no runtime: packaging/build.py's steps,
make_icon.py's icon, and the .iss and release.yml agreeing with the code they package.

    python -m unittest tests.test_packaging -v
"""

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import re
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
sys.path.insert(0, str(ROOT))

from tapewright import __version__, procs  # noqa: E402

try:  # only the committed icon's test draws with theme, which imports tkinter
    import tkinter  # noqa: F401
except ImportError:
    theme = None
else:  # outside the try, so an ImportError from Tapewright's own modules fails instead of skipping
    from tapewright import theme  # noqa: E402

URL = "https://www.python.org/ftp/python/3.14.6/python-3.14.6-amd64.zip"
SHA = "75afa83f93b284d19040e24bc440ab741c09582c0d5310504d607a4e08c3dbaf"


def _load(name):
    """A packaging script, loaded by its path: packaging/ isn't a package, and "build" is a common name."""
    spec = importlib.util.spec_from_file_location("tapewright_packaging_" + name, PACKAGING / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load("build")
make_icon = _load("make_icon")


def _params(line):
    """An entry's parameters, Name: "value"; ..., with "" inside quotes read as one quote."""
    return {name: value[1:-1].replace('""', '"') if value.startswith('"') else value.strip()
            for name, value in re.findall(r'(\w+):\s*("(?:[^"]|"")*"|[^;]*)', line)}


class Script:
    """The .iss as the tests read it: its defines, [Setup]'s directives and the other sections' entries."""

    def __init__(self):
        self.text = (PACKAGING / "tapewright.iss").read_text(encoding="utf-8")
        self.defines, self.setup, self.entries = {}, {}, {}
        section, pending = None, ""
        for raw in self.text.splitlines():
            if raw.endswith(" \\"):  # the preprocessor's line spanning
                pending += raw[:-1].strip() + " "
                continue
            line, pending = pending + raw.strip(), ""
            define = re.fullmatch(r'#define (\w+) "(.*)"', line)
            if define:
                self.defines[define[1]] = define[2]
            elif not line or line.startswith((";", "#")) or section == "Code" and not line.startswith("["):
                continue
            elif re.fullmatch(r"\[\w+\]", line):
                section = line[1:-1]
                self.entries.setdefault(section, [])
            elif section == "Setup":
                key, _, value = line.partition("=")
                self.setup[key] = value
            else:
                self.entries[section].append(_params(line))


def _app_constant(name):
    """A constant assigned in app.py, read with ast, so no tkinter is needed."""
    for node in ast.parse((ROOT / "tapewright" / "app.py").read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == [name]:
            return ast.literal_eval(node.value)
    raise AssertionError("app.py assigns no " + name)


class Installer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.iss = Script()

    def test_setup_waits_for_a_running_tapewright_and_never_closes_it(self):
        self.assertEqual(self.iss.setup["AppMutex"], _app_constant("APP_MUTEX"))
        self.assertEqual(self.iss.setup["CloseApplications"], "no")
        # PrepareToInstall looks again, for the same mutex, once the wizard's pages are done.
        self.assertIn("function PrepareToInstall(var NeedsRestart: Boolean): String;", self.iss.text)
        self.assertEqual(re.findall(r"CheckForMutexes\('([^']*)'\)", self.iss.text),
                         [_app_constant("APP_MUTEX")])

    def test_every_shortcut_carries_the_taskbar_id_the_window_sets(self):
        icons = self.iss.entries["Icons"]
        self.assertEqual([icon["Name"] for icon in icons],
                         ["{autoprograms}\\Tapewright", "{autodesktop}\\Tapewright"])
        self.assertEqual({icon["AppUserModelID"] for icon in icons}, {_app_constant("APP_USER_MODEL_ID")})
        self.assertEqual(icons[1]["Tasks"], "desktopicon")
        self.assertEqual([task["Name"] for task in self.iss.entries["Tasks"]], ["desktopicon"])

    def test_it_installs_for_one_user_in_a_folder_they_can_write_to(self):
        self.assertEqual(self.iss.setup["PrivilegesRequired"], "lowest")
        self.assertNotIn("PrivilegesRequiredOverridesAllowed", self.iss.setup)
        self.assertTrue(self.iss.setup["DefaultDirName"].startswith("{autopf}\\"))
        self.assertEqual(self.iss.setup["DisableDirPage"], "yes")

    def test_the_names_that_must_never_change(self):
        self.assertEqual(self.iss.setup["AppId"], "{{C7E317BA-41A6-4ADE-ABE5-173A5DAC0058}")
        self.assertEqual(self.iss.setup["OutputBaseFilename"], "TapewrightSetup")
        self.assertEqual((PACKAGING / self.iss.setup["OutputDir"].replace("\\", "/")).resolve(), build.DIST)

    def test_it_needs_the_windows_that_python_3_14_supports(self):
        self.assertEqual(self.iss.setup["MinVersion"], "10.0")

    def test_it_expects_the_python_runtime_json_pins(self):
        self.assertEqual(self.iss.defines["PyVersion"], build.load_pin()["version"])
        self.assertEqual(build.MARKER, procs.RUNTIME_MARKER)
        self.assertIn("'" + build.MARKER + "'", self.iss.text)
        self.assertIn("'{#PyVersion}'", self.iss.text)

    def test_every_file_it_names_exists_or_is_made_by_a_build_step(self):
        for directive in ("InfoBeforeFile", "SetupIconFile"):
            with self.subTest(directive):
                self.assertTrue((PACKAGING / self.iss.setup[directive]).is_file())
        folders = [(PACKAGING / entry["Source"].replace("\\", "/")).parent.resolve()
                   for entry in self.iss.entries["Files"]]
        self.assertEqual(folders, [build.BUILD / "app", build.BUILD / "runtime", build.BUILD / "runtime"])
        # What the shortcuts, the Finish page and Installed apps open in the app folder is what stage copies.
        named = set(re.findall(r"\{app\}\\app\\([\w.]+)", self.iss.text))
        self.assertEqual(named, {"Tapewright.pyw", "tapewright.ico"})
        self.assertLessEqual(named, {destination for _, destination in build.stage_list(ROOT)})
        # In the runtime folder they start pythonw.exe, which check requires; RuntimeChanged reads python.exe,
        # which check requires too, and the marker, which mark writes.
        self.assertEqual(set(re.findall(r"\{app\}\\runtime\\([\w.-]+)", self.iss.text)), {"pythonw.exe"})
        self.assertIn("'python.exe'", self.iss.text)
        failed = [sentence for passed, sentence in build.findings(build.load_pin(), None, None, set())
                  if not passed]
        self.assertTrue([sentence for sentence in failed if "pythonw.exe" in sentence])

    def test_the_shortcuts_and_the_finish_page_start_tapewright_alike(self):
        for entry in self.iss.entries["Icons"] + self.iss.entries["Run"]:
            with self.subTest(entry.get("Name") or entry.get("Description")):
                self.assertEqual(entry["Filename"], "{app}\\runtime\\pythonw.exe")
                self.assertEqual(entry["Parameters"], '-I "{app}\\app\\Tapewright.pyw"')
                self.assertEqual(entry["WorkingDir"], "{app}\\app")
        flags = self.iss.entries["Run"][0]["Flags"].split()
        self.assertEqual(flags, ["postinstall", "nowait", "skipifsilent"])

    def test_the_runtime_is_replaced_only_when_the_pin_changes(self):
        checks = {entry["Name"]: entry.get("Check") for entry in self.iss.entries["InstallDelete"]}
        self.assertEqual(checks, {"{app}\\app": None, "{app}\\runtime": "RuntimeChanged"})
        files = self.iss.entries["Files"]
        copied = [(entry["DestDir"], entry.get("Check")) for entry in files]
        self.assertEqual(copied, [("{app}\\app", None), ("{app}\\runtime", "RuntimeChanged"),
                                  ("{app}\\runtime", "RuntimeChanged")])
        self.assertIn("function RuntimeChanged(): Boolean;", self.iss.text)
        # The marker is copied last, on its own, so a Setup stopped partway through the runtime leaves none.
        self.assertEqual(files[1]["Source"], "..\\build\\runtime\\*")
        self.assertEqual(files[1]["Excludes"], "\\" + build.MARKER)
        self.assertEqual(files[2]["Source"], "..\\build\\runtime\\" + build.MARKER)
        self.assertNotIn("recursesubdirs", files[2]["Flags"])

    def test_uninstall_takes_the_helpers_and_keeps_the_settings(self):
        removed = {entry["Name"]: entry["Type"] for entry in self.iss.entries["UninstallDelete"]}
        self.assertEqual(removed["{localappdata}\\Tapewright\\tools"], "filesandordirs")
        self.assertEqual(removed["{app}\\runtime"], "filesandordirs")
        self.assertFalse([name for name in removed if "appdata}" in name and "{localappdata}" not in name])


class Pin(unittest.TestCase):
    def test_runtime_json_names_one_python_zip_by_version_and_hash(self):
        raw = json.loads((PACKAGING / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(build.load_pin(), raw)
        version = raw["version"]
        self.assertEqual(raw["url"],
                         f"https://www.python.org/ftp/python/{version}/python-{version}-amd64.zip")
        self.assertEqual((raw["url"], raw["sha256"], raw["tk"]), (URL, SHA, "8.6"))

    def test_a_pin_in_the_wrong_shape_is_refused(self):
        good = json.loads((PACKAGING / "runtime.json").read_text(encoding="utf-8"))
        wrong = [dict(good, sha256=good["sha256"].upper()),
                 dict(good, url=good["url"].replace("https", "http")),
                 dict(good, version="3.14"), dict(good, tk=8.6), dict(good, extra="x"),
                 {key: value for key, value in good.items() if key != "tk"}, [good]]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "runtime.json"
            for pin in wrong:
                with self.subTest(pin=pin):
                    path.write_text(json.dumps(pin), encoding="utf-8")
                    with self.assertRaises(build.BuildError):
                        build.load_pin(path)


class Build(unittest.TestCase):
    def test_the_version_is_read_without_running_the_file(self):
        self.assertEqual(build.read_version(ROOT / "tapewright" / "__init__.py"), __version__)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "__init__.py"
            path.write_text('"""A package."""\nimport os\n__version__ = "1.2.3"\n', encoding="utf-8")
            self.assertEqual(build.read_version(path), "1.2.3")
            for source in ('VERSION = "1.2.3"\n', "__version__ = get()\n",
                           'if True:\n    __version__ = "9"\n'):
                with self.subTest(source=source):
                    path.write_text(source, encoding="utf-8")
                    with self.assertRaises(build.BuildError):
                        build.read_version(path)

    def test_check_tag_and_versions_on_the_command_line(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(build.main(["check-tag", "v" + __version__]), 0)
            self.assertEqual(build.main(["check-tag", __version__]), 1)
            self.assertEqual(build.main(["versions"]), 0)
        self.assertIn(f"isn't v{__version__}", err.getvalue())
        self.assertIn(f"app={__version__}\npython={build.load_pin()['version']}\n", out.getvalue())

    def test_the_index_must_list_that_exact_address_with_that_hash(self):
        index = {"versions": [
            {"id": "pythoncore-3.14-64", "sort-version": "3.14.7", "url": URL.replace("3.14.6", "3.14.7"),
             "hash": {"sha256": "a" * 64}},
            {"id": "pythoncore-3.14-64", "sort-version": "3.14.6", "url": URL, "hash": {"sha256": SHA}},
        ]}
        self.assertTrue(build.index_has(index, URL, SHA))
        self.assertTrue(build.index_has(index, URL, SHA.upper()))
        self.assertFalse(build.index_has(index, URL, "b" * 64))
        self.assertFalse(build.index_has(index, URL.replace("3.14.6", "3.14.7"), SHA))
        self.assertFalse(build.index_has(index, URL + "?", SHA))
        broken = ({"versions": [{"url": URL}]},
                  {"versions": [None, 3, {"url": URL, "hash": "sha256:" + SHA}]},
                  {"versions": "nope"}, {}, [], None)
        for index_json in broken:
            with self.subTest(index=index_json):
                self.assertFalse(build.index_has(index_json, URL, SHA))

    def test_later_index_pages_are_followed_only_on_python_org(self):
        page = build.INDEX_URL
        self.assertEqual(build.next_page({"next": "index-windows-legacy.json"}, page),
                         "https://www.python.org/ftp/python/index-windows-legacy.json")
        for following in ("https://example.com/index.json", "//example.com/index.json",
                          "http://www.python.org/ftp/python/index.json", "", 3, None):
            with self.subTest(following=following):
                self.assertIsNone(build.next_page({"next": following}, page))
        self.assertIsNone(build.next_page({"versions": []}, page))

        pin = {"url": URL, "sha256": SHA}
        legacy = "https://www.python.org/ftp/python/index-windows-legacy.json"
        pages = {page: {"versions": [], "next": "index-windows-legacy.json"},
                 legacy: {"versions": [{"url": URL, "hash": {"sha256": SHA}}]}}
        asked = []

        def opener(url):
            asked.append(url)
            body = pages[url]
            return io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())

        self.assertTrue(build.python_org_lists(pin, opener))
        self.assertEqual(asked, [page, legacy])
        pages[legacy] = {"versions": [], "next": "index-windows.json"}  # back to the first page: stop there
        asked.clear()
        self.assertFalse(build.python_org_lists(pin, opener))
        self.assertEqual(asked, [page, legacy])
        pages[page] = b"<html>Service Unavailable</html>"
        with self.assertRaises(build.BuildError):
            build.python_org_lists(pin, opener)

    def test_the_download_is_kept_only_with_the_pinned_hash(self):
        body = b"a python runtime" * 5000
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "build" / "python.zip"
            size = build.download(URL, dest, hashlib.sha256(body).hexdigest().upper(),
                                  opener=lambda url: io.BytesIO(body))
            self.assertEqual((size, dest.read_bytes()), (len(body), body))
            dest.unlink()
            with self.assertRaises(build.BuildError):
                build.download(URL, dest, SHA, opener=lambda url: io.BytesIO(body))
            self.assertEqual(list(dest.parent.iterdir()), [])

    def test_zip_names_that_could_land_outside_the_folder_are_refused(self):
        fine = ["python.exe", "Lib/", "Lib/os.py", "tcl/tk8.6/tk.tcl", "./LICENSE.txt", "a//b.txt",
                "..x", "a..b/c"]
        self.assertEqual(build.safe_members(fine), fine)
        refused = ("../evil.exe", "Lib/../../evil.exe", "Lib\\..\\..\\evil.exe", ".. /evil.exe",
                   "Lib/.../evil.exe", "/evil.exe", "\\evil.exe", "\\\\server\\share\\evil.exe",
                   "C:/evil.exe", "C:evil.exe", "python.exe:stream", "", "evil\0.exe")
        for name in refused:
            with self.subTest(name=name), self.assertRaises(build.BuildError):
                build.safe_members(["python.exe", name])
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            with zipfile.ZipFile(folder / "runtime.zip", "w") as zipped:
                zipped.writestr("python.exe", b"MZ")
                zipped.writestr("Lib/os.py", b"")
            runtime = folder / "runtime"
            self.assertEqual(build.unpack(folder / "runtime.zip", runtime), 2)
            unpacked = sorted(p.relative_to(runtime).as_posix() for p in runtime.rglob("*") if p.is_file())
            self.assertEqual(unpacked, ["Lib/os.py", "python.exe"])
            with zipfile.ZipFile(folder / "evil.zip", "w") as zipped:
                zipped.writestr("python.exe", b"MZ")
                zipped.writestr(zipfile.ZipInfo("../evil.exe"), b"MZ")
            with self.assertRaises(build.BuildError):
                build.unpack(folder / "evil.zip", folder / "unpacked" / "runtime")
            self.assertFalse((folder / "unpacked").exists())

    def test_the_stage_takes_the_app_and_leaves_out_tests_and_caches(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("Tapewright.pyw", "LICENSE", "README.md", "pyproject.toml",
                         "tapewright/__init__.py", "tapewright/app.py", "tapewright/notes.txt",
                         "tapewright/__pycache__/app.cpython-314.pyc", "tests/test_core.py",
                         "tests/__pycache__/test_core.cpython-314.pyc", "packaging/tapewright.ico",
                         "packaging/build.py"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(b"")
            pairs = build.stage_list(root)
            self.assertEqual([destination for _, destination in pairs],
                             ["Tapewright.pyw", "tapewright/__init__.py", "tapewright/app.py", "LICENSE",
                              "README.md", "tapewright.ico"])
            self.assertTrue(all(source.is_file() for source, _ in pairs))
            (root / "tapewright" / "more").mkdir()
            (root / "tapewright" / "more" / "module.py").write_bytes(b"")
            with self.assertRaises(build.BuildError):
                build.stage_list(root)
        destinations = [destination for _, destination in build.stage_list(ROOT)]
        self.assertIn("tapewright/fetch.py", destinations)  # run by its path as a child, so it must ship
        self.assertFalse([destination for destination in destinations if "test" in destination])

    def test_stage_mark_check_and_sums_on_the_command_line(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            app, runtime = folder / "app", folder / "runtime"
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(build.main(["stage", "--out", str(app)]), 0)
                self.assertEqual(build.main(["stage", "--out", str(app)]), 1)
                runtime.mkdir()
                self.assertEqual(build.main(["mark", "--runtime", str(runtime)]), 1)
                self.assertEqual(build.main(["check", "--runtime", str(runtime)]), 1)
                (runtime / "python.exe").write_bytes(b"")
                self.assertEqual(build.main(["mark", "--runtime", str(runtime)]), 0)
                (folder / "TapewrightSetup.exe").write_bytes(b"MZ, an installer")
                with mock.patch.object(build, "DIST", folder / "dist"):
                    self.assertEqual(build.main(["sums", str(folder / "TapewrightSetup.exe")]), 0)
            staged = sorted(p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file())
            self.assertEqual(staged, sorted(destination for _, destination in build.stage_list(ROOT)))
            self.assertEqual((app / "tapewright.ico").read_bytes(),
                             (PACKAGING / "tapewright.ico").read_bytes())
            self.assertIn("isn't empty", err.getvalue())
            self.assertEqual((runtime / procs.RUNTIME_MARKER).read_bytes(),
                             build.load_pin()["version"].encode() + b"\n")
            installer_sum = hashlib.sha256(b"MZ, an installer").hexdigest()
            self.assertEqual((folder / "dist" / "SHA256SUMS.txt").read_bytes(),
                             f"{installer_sum}  TapewrightSetup.exe\n".encode())

    def test_sums_list_each_file_by_name_once(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "one").mkdir()
            (folder / "a.txt").write_bytes(b"a")
            (folder / "one" / "a.txt").write_bytes(b"b")
            (folder / "b.txt").write_bytes(b"")
            empty, a = hashlib.sha256(b"").hexdigest(), hashlib.sha256(b"a").hexdigest()
            self.assertEqual(build.sums_text([folder / "b.txt", folder / "a.txt"]),
                             f"{empty}  b.txt\n{a}  a.txt\n")
            with self.assertRaises(build.BuildError):
                build.sums_text([folder / "a.txt", folder / "one" / "a.txt"])

    def test_check_fails_on_each_way_the_runtime_can_be_wrong(self):
        pin = build.load_pin()
        good = {"python": [int(part) for part in pin["version"].split(".")], "tk": float(pin["tk"]),
                "patchlevel": pin["tk"] + ".15"}
        names = {"python.exe", "pythonw.exe", "LICENSE.txt", "python314.dll"}
        pip = "pip 26.1.2 from C:\\runtime\\Lib\\site-packages\\pip (python 3.14)"

        def failures(answer, pip_line, files):
            return [sentence for passed, sentence in build.findings(pin, answer, pip_line, files)
                    if not passed]

        self.assertEqual(failures(good, pip, names), [])
        wrong = {
            "another Python": (dict(good, python=[3, 14, 7]), pip, names, 1),
            "Tk 9": (dict(good, tk=9.0, patchlevel="9.0.4"), pip, names, 2),
            "Tk 8.60": (dict(good, patchlevel="8.60.1"), pip, names, 1),
            "a probe that didn't run": (None, None, names, 4),
            "no pip": (good, None, names, 1),
            "no pythonw.exe": (good, pip, names - {"pythonw.exe"}, 1),
            "no license": (good, pip, names - {"LICENSE.txt"}, 1),
            "a ._pth file": (good, pip, names | {"python314._pth"}, 1),
        }
        for what, (answer, pip_line, files, count) in wrong.items():
            with self.subTest(what):
                failed = failures(answer, pip_line, files)
                self.assertEqual(len(failed), count, failed)


class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        cls.steps = cls.text.split("\n    steps:\n", 1)[1].split("\n      - ")

    def step(self, pattern):
        found = [index for index, step in enumerate(self.steps) if re.search(pattern, step, re.M)]
        self.assertTrue(found, "no step matches " + pattern)
        return found[0]

    def test_it_calls_every_build_step_and_no_other(self):
        names = set(re.findall(r"run: python packaging/build\.py ([\w-]+)", self.text))
        self.assertEqual(names, set(build.COMMANDS))
        for name in names:
            with self.subTest(name), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as done:
                    build.main([name, "--help"])
                self.assertEqual(done.exception.code, 0)

    def test_the_steps_run_in_the_order_the_installer_needs(self):
        order = [r"build\.py check-tag", r"build\.py versions", r"build\.py runtime$", r"build\.py check$",
                 r"build\\runtime\\python\.exe -m unittest", r"build\.py mark$", r"build\.py stage$", r"ISCC",
                 r"build\.py sums", r"uses: actions/upload-artifact@", r"uses: actions/attest@",
                 r"gh release create"]
        indexes = [self.step(pattern) for pattern in order]
        self.assertEqual(indexes, sorted(set(indexes)))

    def test_the_unit_tests_keep_their_settings_and_tools_in_the_runners_temp_folder(self):
        tests = self.steps[self.step(r"-m unittest")]
        self.assertIn("TAPEWRIGHT_CONFIG_DIR: ${{ runner.temp }}", tests)
        self.assertIn("TAPEWRIGHT_TOOLS_DIR: ${{ runner.temp }}", tests)

    def test_only_a_pushed_tag_is_checked_attested_and_released(self):
        self.assertIn('  push:\n    tags: ["v*"]', self.text)
        self.assertNotIn("branches:", self.text)
        for pattern in (r"build\.py check-tag", r"uses: actions/attest@", r"gh release create"):
            with self.subTest(pattern):
                self.assertIn("if: github.event_name == 'push'", self.steps[self.step(pattern)])
        for pattern in (r"build\.py runtime$", r"ISCC", r"uses: actions/upload-artifact@"):
            with self.subTest(pattern):
                self.assertNotIn("if:", self.steps[self.step(pattern)])
        release = self.steps[self.step(r"gh release create")]
        for part in ("dist/TapewrightSetup.exe dist/SHA256SUMS.txt", "--draft",
                     "--notes-file packaging/release-notes.md"):
            self.assertIn(part, release)

    def test_the_compiler_gets_the_defines_the_script_expects(self):
        compile_step = self.steps[self.step(r"ISCC")]
        self.assertEqual(re.findall(r"/D(\w+)=", compile_step), ["AppVersion", "PyVersion"])
        self.assertEqual(set(Script().defines), {"AppVersion", "PyVersion"})
        self.assertIn("packaging\\tapewright.iss", compile_step)


def _drawing(size):
    """Stripes of three colors inside a clear border, runs of color like the cassette's."""
    colors = ("#18181b", "#ecebe6", "#1f5c52")
    return [[None if min(x, y, size - 1 - x, size - 1 - y) < size // 8 else colors[(x // 3 + y // 5) % 3]
             for x in range(size)] for y in range(size)]


def _icon_images(data):
    """(size, image bytes) for each entry of an .ico, checking the directory is laid out without gaps."""
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert (reserved, kind) == (0, 1), (reserved, kind)
    images, expected = [], 6 + 16 * count
    for index in range(count):
        entry = struct.unpack_from("<BBBBHHII", data, 6 + 16 * index)
        width, height, palette, zero, planes, bits, length, offset = entry
        assert (width, palette, zero, planes, bits, offset) == (height, 0, 0, 1, 32, expected), index
        images.append((width or 256, data[offset:offset + length]))
        expected = offset + length
    assert expected == len(data)
    return images


def _bitmap_pixels(image, size):
    fields = struct.unpack_from("<IiiHHI", image)
    assert fields == (40, size, 2 * size, 1, 32, 0), fields
    stride = (size + 31) // 32 * 4
    colors, mask = image[40:40 + 4 * size * size], image[40 + 4 * size * size:]
    assert len(mask) == stride * size
    rows = []
    for y in range(size):
        up = size - 1 - y  # stored bottom-up
        row = []
        for x in range(size):
            blue, green, red, alpha = colors[4 * (up * size + x):4 * (up * size + x) + 4]
            clear = mask[up * stride + x // 8] >> (7 - x % 8) & 1
            assert (alpha, clear) in ((0, 1), (255, 0)) and (alpha or not blue + green + red), (x, y)
            row.append(f"#{red:02x}{green:02x}{blue:02x}" if alpha else None)
        rows.append(row)
    return rows


def _png_pixels(image):
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    at, chunks = 8, []
    while at < len(image):
        (length,) = struct.unpack_from(">I", image, at)
        kind, body = image[at + 4:at + 8], image[at + 8:at + 8 + length]
        assert struct.unpack_from(">I", image, at + 8 + length)[0] == zlib.crc32(kind + body), kind
        chunks.append((kind, body))
        at += 12 + length
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (width, depth, color, compression, filtering, interlace) == (height, 8, 6, 0, 0, 0)
    raw, rows = zlib.decompress(chunks[1][1]), []
    for y in range(height):
        line = raw[y * (1 + 4 * width):(y + 1) * (1 + 4 * width)]
        assert line[0] == 0  # no filter
        pixels = [line[1 + 4 * x:5 + 4 * x] for x in range(width)]
        assert all(pixel[3] in (0, 255) and (pixel[3] or pixel == bytes(4)) for pixel in pixels)
        rows.append(["#" + pixel[:3].hex() if pixel[3] else None for pixel in pixels])
    return rows


class Icon(unittest.TestCase):
    def test_the_deflate_stream_inflates_to_the_same_bytes(self):
        samples = [b"", b"a", b"ab" * 400, bytes(300), bytes(range(256)) * 3,
                   b"xyz" * 1000 + bytes(1025) + b"q" * 259]
        for data in samples:
            for distances in ((1,), (4, 1025), (3,), (1, 2, 3, 258, 32768)):
                with self.subTest(size=len(data), distances=distances):
                    self.assertEqual(zlib.decompress(make_icon.zlib_stream(data, distances)), data)

    def test_an_icon_holds_each_size_as_it_was_drawn(self):
        data = make_icon.icon_bytes(_drawing, sizes=(16, 24, 256))
        self.assertEqual(data, make_icon.icon_bytes(_drawing, sizes=(16, 24, 256)))
        images = _icon_images(data)
        self.assertEqual([size for size, _ in images], [16, 24, 256])
        for size, image in images:
            with self.subTest(size=size):
                pixels = _png_pixels(image) if size == 256 else _bitmap_pixels(image, size)
                self.assertEqual(pixels, _drawing(size))

    @unittest.skipIf(theme is None, "a Python built without Tk")
    def test_the_committed_icon_is_the_cassette_drawn_again(self):
        committed = (PACKAGING / "tapewright.ico").read_bytes()
        self.assertEqual(make_icon.icon_bytes(theme.cassette_pixels), committed,
                         "run python packaging/make_icon.py and commit packaging/tapewright.ico")
        images = _icon_images(committed)
        self.assertEqual(tuple(size for size, _ in images), (16, 24, 32, 48, 64, 256))
        self.assertEqual(_png_pixels(images[-1][1]), theme.cassette_pixels(256))
        self.assertEqual(_bitmap_pixels(images[0][1], 16), theme.cassette_pixels(16))


if __name__ == "__main__":
    unittest.main()
