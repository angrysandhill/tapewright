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
import itertools
import json
import os
import re
import struct
import sys
import tempfile
import unittest
import urllib.parse
import zipfile
import zlib
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
sys.path.insert(0, str(ROOT))

from tapewright import APP_NAME, __version__, config, deps, help_content, procs  # noqa: E402

try:  # only the committed icon's test draws with theme, which imports tkinter
    import tkinter  # noqa: F401
except ImportError:
    theme = None
else:  # outside the try, so an ImportError from Tapewright's own modules fails instead of skipping
    from tapewright import theme  # noqa: E402

URL = "https://www.python.org/ftp/python/3.14.6/python-3.14.6-amd64.zip"
SHA = "75afa83f93b284d19040e24bc440ab741c09582c0d5310504d607a4e08c3dbaf"
VER_CHECK = object()  # Script's mark for an open #if Ver block, which no #ifdef name can equal
SIGNPATH_NS = "{http://signpath.io/artifact-configuration/v1}"  # the namespace SignPath's reference gives


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
    """The .iss as the tests read it: its defines, [Setup]'s directives, the other sections' entries, and in
    conditional[NAME] the [Setup] directives between #ifdef NAME and #endif, which ISCC reads only when given
    /DNAME.
    """

    def __init__(self):
        self.text = (PACKAGING / "tapewright.iss").read_text(encoding="utf-8")
        self.defines, self.setup, self.entries, self.conditional = {}, {}, {}, {}
        self.needs_inno, self.too_old = None, None  # the #if Ver check's oldest Inno Setup, and its #error
        # blocks: each open #ifdef's name, None for #ifndef, or VER_CHECK for the compiler version check
        section, pending, blocks = None, "", []
        for raw in self.text.splitlines():
            if raw.endswith(" \\"):  # the preprocessor's line spanning
                pending += raw[:-1].strip() + " "
                continue
            line, pending = pending + raw.strip(), ""
            define = re.fullmatch(r'#define (\w+) "(.*)"', line)
            opened = re.fullmatch(r"#(ifn?def) (\w+)", line)
            needs = re.fullmatch(r"#if Ver < EncodeVer\((\d+), (\d+), (\d+)\)", line)
            if opened:
                blocks.append(opened[2] if opened[1] == "ifdef" else None)
            elif needs:
                self.needs_inno = tuple(int(part) for part in needs.groups())
                blocks.append(VER_CHECK)
            elif line == "#endif":
                blocks.pop()
            elif not line or line.startswith(";") or section == "Code" and not line.startswith("["):
                continue
            elif blocks and not (define if blocks[-1] is None
                                 else line.startswith("#error ") if blocks[-1] is VER_CHECK
                                 else section == "Setup" and re.fullmatch(r"\w+=.*", line)):
                # Only the shapes the script uses are read: a default #define inside #ifndef, a [Setup]
                # directive inside #ifdef and an #error inside the version check. Anything else fails here
                # instead of being misread.
                raise AssertionError("tests/test_packaging.py can't read this inside an #if: " + line)
            elif line.startswith("#error "):
                assert blocks, "an #error outside any #if stops every compile"
                self.too_old = line[len("#error "):]
            elif define:
                self.defines[define[1]] = define[2]
            elif line.startswith("#"):
                continue
            elif re.fullmatch(r"\[\w+\]", line):
                section = line[1:-1]
                self.entries.setdefault(section, [])
            elif section == "Setup":
                key, _, value = line.partition("=")
                (self.conditional.setdefault(blocks[-1], {}) if blocks else self.setup)[key] = value
            else:
                self.entries[section].append(_params(line))
        assert not blocks, "an #ifdef or #ifndef with no #endif"


def _app_constant(name):
    """A constant assigned in app.py, read with ast, so no tkinter is needed."""
    for node in ast.parse((ROOT / "tapewright" / "app.py").read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == [name]:
            return ast.literal_eval(node.value)
    raise AssertionError("app.py assigns no " + name)


def _settings_label(key):
    """The words beside the Settings tab's tick box for a setting, read with ast, so no tkinter is needed."""
    source = (ROOT / "tapewright" / "settings_tab.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "_check" and len(node.args) > 3
                and isinstance(node.args[3], ast.Constant) and node.args[3].value == key):
            return ast.literal_eval(node.args[2])
    raise AssertionError("the Settings tab has no tick box for " + key)


def _lookup_hosts():
    """The hosts of the addresses deps.py's checks look up, from each fetch_text(ADDRESS, ...) call there."""
    source = (ROOT / "tapewright" / "deps.py").read_text(encoding="utf-8")
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "fetch_text"]
    assert calls and all(isinstance(call.args[0], ast.Name) for call in calls), "a lookup it can't read"
    return {urllib.parse.urlsplit(getattr(deps, call.args[0].id)).hostname for call in calls}


def _steps(text):
    """release.yml's steps in order, as the tests read them: each a dict of its keys, where env: and with: are
    dicts too and a block scalar is its lines joined ("|" by line breaks, ">-" by spaces), plus "text", the
    step's own lines without YAML comments. Only the shapes release.yml uses are read, so any other line fails
    here instead of being misread.
    """
    # levels holds (the indent of a mapping's keys, the mapping) for each open mapping, innermost last.
    steps, levels, block = [], [], None

    def close():
        block["into"][block["key"]] = block["join"].join(block["lines"]).strip()

    for raw in text.split("\n    steps:\n", 1)[1].splitlines():
        indent = len(raw) - len(raw.lstrip(" "))
        if block:
            if not raw.strip() or indent > block["indent"]:
                block["lines"].append(raw[block["indent"] + 2:])
                steps[-1]["text"] += raw + "\n"
                continue
            close()
            block = None
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith("      - "):
            steps.append({"text": ""})
            levels = [(8, steps[-1])]
            raw, indent = " " * 8 + raw[8:], 8
        found = re.fullmatch(r" *([\w-]+):(?: +(.*?))?(?: +#.*)?", raw)
        while levels and levels[-1][0] > indent:
            levels.pop()
        assert found and levels and levels[-1][0] == indent, "tests/test_packaging.py can't read this: " + raw
        key, value = found.groups()
        mapping = levels[-1][1]
        steps[-1]["text"] += raw + "\n"
        if value is None:
            mapping[key] = {}
            levels.append((indent + 2, mapping[key]))
        elif value in ("|", ">-"):
            block = {"into": mapping, "key": key, "indent": indent, "lines": [],
                     "join": "\n" if value == "|" else " "}
        else:
            mapping[key] = value
    if block:
        close()
    return steps


def _prose(markdown):
    """Markdown as it reads: an inline link as its words, and any run of spaces and line breaks as one."""
    return " ".join(re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", markdown).split())


def _section(markdown, heading):
    """What a Markdown file says under one "## " heading, up to the next."""
    found = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", markdown, re.M | re.S)
    assert found, "no ## " + heading
    return found[1]


def _help_words(topics):
    """Every string in these Help topics, joined by spaces, so a sentence split across strings is found."""
    if isinstance(topics, str):
        return topics
    values = topics.values() if isinstance(topics, dict) else topics
    return " ".join(" ".join(_help_words(value) for value in values).split())


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

    def test_it_installs_for_one_user_in_a_folder_they_can_write_to(self):
        self.assertEqual(self.iss.setup["PrivilegesRequired"], "lowest")
        self.assertNotIn("PrivilegesRequiredOverridesAllowed", self.iss.setup)
        self.assertTrue(self.iss.setup["DefaultDirName"].startswith("{autopf}\\"))
        self.assertEqual(self.iss.setup["DisableDirPage"], "yes")

    def test_the_script_itself_refuses_an_inno_setup_older_than_6_6(self):
        # WizardStyle's dark mode arrived in 6.6.0. The check is the preprocessor's, because Inno Setup's own
        # programs carry no version Windows can read, so release.yml can't make it (see Workflow).
        self.assertEqual(self.iss.needs_inno, (6, 6, 0))
        self.assertIn("6.6", self.iss.too_old)
        self.assertEqual(self.iss.setup["WizardStyle"], "modern dark")
        self.assertLess(self.iss.text.index("#if Ver"), self.iss.text.index("[Setup]"))

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

    def test_the_version_info_names_tapewright_and_the_version_it_installs(self):
        # Inno Setup's own defaults, written out, since a signing policy can hold both (see AGENTS.md).
        self.assertEqual(self.iss.setup["AppName"], APP_NAME)
        self.assertEqual(self.iss.setup["VersionInfoProductName"], self.iss.setup["AppName"])
        for directive in ("AppVersion", "VersionInfoVersion", "VersionInfoProductVersion"):
            with self.subTest(directive):
                self.assertEqual(self.iss.setup[directive], "{#AppVersion}")

    def test_the_uninstaller_is_signed_only_when_the_compiler_is_told_to(self):
        self.assertEqual(list(self.iss.conditional), ["SignUninstaller"])
        signed = self.iss.conditional["SignUninstaller"]
        self.assertEqual(set(signed), {"SignedUninstaller", "SignedUninstallerDir"})
        self.assertEqual(signed["SignedUninstaller"], "yes")
        # Without /DSignUninstaller nothing defines the name, and no Sign* directive, SignTool included, is
        # set anywhere else.
        self.assertNotIn("SignUninstaller", self.iss.defines)
        self.assertFalse([key for key in self.iss.setup if key.lower().startswith("sign")])
        self.assertEqual(len(re.findall(r"^\s*Sign\w*\s*=", self.iss.text, re.M | re.I)), len(signed))
        # The unsigned copy ISCC leaves to be signed stays out of dist/, which release.yml uploads.
        folder = (PACKAGING / signed["SignedUninstallerDir"].replace("\\", "/")).resolve()
        self.assertIn(build.BUILD, folder.parents)
        self.assertNotIn(build.DIST, [folder, *folder.parents])

    def test_setup_offers_the_start_up_check_in_the_settings_tabs_words_while_there_are_no_settings(self):
        tasks = {task["Name"]: task for task in self.iss.entries["Tasks"]}
        self.assertEqual(list(tasks), ["desktopicon", "startupcheck"])
        offered = tasks["startupcheck"]
        self.assertEqual(offered["Description"], _settings_label("check_on_startup"))
        # Offered only while there is no settings file, and ticked, which leaves the check on as config's
        # default has it.
        self.assertEqual(offered["Check"], "NoSettingsYet")
        self.assertIn("function NoSettingsYet(): Boolean;", self.iss.text)
        self.assertNotIn("Flags", offered)
        self.assertIs(config.DEFAULTS["check_on_startup"], True)

    def test_unticked_setup_writes_a_settings_file_tapewright_reads_whole(self):
        code = self.iss.text.split("\n[Code]\n", 1)[1]
        literals = re.findall(r"'(\{\"[^']*\})'", code)  # a JSON object, not an inline {#define}
        self.assertEqual(len(literals), 1)
        self.assertEqual(json.loads(literals[0]), {"check_on_startup": False})
        self.assertTrue(config._valid("check_on_startup", False))
        # Only after installing, and only with the box unticked. A file already there is only read, a missing
        # one is written, and the message follows when either doesn't work out.
        self.assertIn("if (CurStep = ssPostInstall) and NoSettingsYet() and not "
                      "WizardIsTaskSelected('startupcheck') then", code)
        body = " ".join(code.split("procedure CurStepChanged(CurStep: TSetupStep);", 1)[1].split())
        found = re.search(
            r"if FileExists\(Settings\) then Done := LoadStringFromFile\(Settings, Content\) "
            r"and \(Pos\('([^']*)', Content\) > 0\) "
            r"else Done := ForceDirectories\(ExtractFileDir\(Settings\)\) "
            r"and SaveStringToFile\(Settings, '([^']*)' \+ #10, False\); "
            r"if not Done then SuppressibleMsgBox\(", body)
        self.assertIsNotNone(found, "CurStepChanged isn't shaped as this test reads it")
        wanted, written = found.groups()
        self.assertEqual(written, literals[0])
        self.assertEqual(code.count("SaveStringToFile("), 1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(literals[0] + "\n", encoding="utf-8")
            settings = config.Settings(path)
            settings.load()
            self.assertIsNone(settings.problem)
            self.assertEqual(settings.data, dict(config.DEFAULTS, check_on_startup=False))
            self.assertIs(settings["setup_done"], False)  # so the setup screen still opens
            # Tapewright's own save holds the words Setup looks for, in the bytes LoadStringFromFile reads,
            # only when its check is off, so a file it wrote on closing with the check on gets the message.
            for check in (False, True):
                with self.subTest(check_on_startup=check):
                    settings["check_on_startup"] = check
                    self.assertIsNone(settings.save())
                    self.assertIs(wanted.encode("ascii") in path.read_bytes(), not check)

    def test_setup_looks_for_the_settings_file_where_tapewright_keeps_it(self):
        self.assertIn("GetEnv('TAPEWRIGHT_CONFIG_DIR')", self.iss.text)
        self.assertIn("AddBackslash(Result) + 'settings.json'", self.iss.text)
        self.assertIn("ExpandConstant('{userappdata}\\" + APP_NAME + "\\settings.json')", self.iss.text)
        elsewhere = str(ROOT / "no-such-folder")
        with mock.patch.dict(os.environ, {"TAPEWRIGHT_CONFIG_DIR": elsewhere}):
            self.assertEqual(config.Settings().path, Path(elsewhere) / "settings.json")
        if os.name == "nt":  # {userappdata} is %APPDATA%; an empty variable counts as unset, as with GetEnv
            with mock.patch.dict(os.environ, {"TAPEWRIGHT_CONFIG_DIR": "", "APPDATA": elsewhere}):
                self.assertEqual(config.Settings().path, Path(elsewhere) / APP_NAME / "settings.json")

    def test_setups_information_page_says_what_the_check_at_start_asks(self):
        text = (PACKAGING / "before-install.txt").read_text(encoding="utf-8")
        self.assertIn(_settings_label("check_on_startup"), text)
        for host in sorted(_lookup_hosts()):
            with self.subTest(host):
                self.assertIn(host, text)
        self.assertIsNone(re.search(r"[^\n]\n[^\n]", text), "one line per paragraph")


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

    def test_signing_json_is_one_boolean(self):
        # Read as text, so a checkout that turns its line break into \r\n still passes. A byte order mark
        # survives reading and fails the \A\{.
        data = (PACKAGING / "signing.json").read_text(encoding="utf-8")
        self.assertRegex(data, r'\A\{"installer_signed": (true|false)\}\n\Z')
        self.assertIs(build.load_signing(), json.loads(data)["installer_signed"])
        self.assertEqual(build.SIGNING, PACKAGING / "signing.json")
        wrong = ('{"installer_signed": "false"}', '{"installer_signed": 0}', '{"installer_signed": null}',
                 '{"installer_signed": false, "signed": true}', '{"Installer_signed": false}', "{}",
                 "[false]", "false", '{"installer_signed": false', "", '\ufeff{"installer_signed": false}')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "signing.json"
            for text, answer in (('{"installer_signed": true}\n', True),
                                 ('{"installer_signed": false}', False)):
                path.write_text(text, encoding="utf-8")
                self.assertIs(build.load_signing(path), answer)
            for text in wrong:
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(build.BuildError):
                        build.load_signing(path)

    def test_the_signing_plan_for_each_kind_of_run(self):
        organization, project, test, release, uninstaller = build.SIGNPATH_VARIABLES
        configured = {organization: "0f5c7e1a-organization", project: "tapewright", test: "test-signing",
                      release: "release-signing"}

        def plan(mode="none", policy="", signs_uninstaller="false"):
            return {"mode": mode, "policy": policy, "uninstaller": signs_uninstaller}

        releasing, testing = plan("release", "release-signing"), plan("test", "test-signing")
        rows = [
            # A tag push stays unsigned while signing.json says so, whatever is set.
            ("push", False, {}, plan()),
            ("push", False, dict(configured, **{uninstaller: "true"}), plan()),
            ("push", False, {uninstaller: "yes"}, plan()),
            # Once it says signed, the release policy, and never the test one.
            ("push", True, configured, releasing),
            ("push", True, {organization: "o", project: "p", release: "release-signing"}, releasing),
            ("push", True, dict(configured, **{uninstaller: "true"}),
             plan("release", "release-signing", "true")),
            ("push", True, dict(configured, **{uninstaller: "false"}), releasing),
            ("push", True, dict(configured, **{uninstaller: ""}), releasing),
            # A manual run test-signs when SignPath is configured, whatever signing.json says, and never with
            # the release policy. With none of it configured, it builds unsigned.
            ("workflow_dispatch", False, configured, testing),
            ("workflow_dispatch", True, configured, testing),
            ("workflow_dispatch", False, {organization: "o", project: "p", test: "test-signing"}, testing),
            ("workflow_dispatch", True, dict(configured, **{uninstaller: "true"}),
             plan("test", "test-signing", "true")),
            ("workflow_dispatch", True, {}, plan()),
            ("workflow_dispatch", False, {organization: "", project: "", test: ""}, plan()),
            ("workflow_dispatch", True, {release: "release-signing"}, plan()),
            ("workflow_dispatch", False, {uninstaller: "true"}, plan()),
        ]
        for event, signed, env, expected in rows:
            with self.subTest(event=event, signed=signed, env=env):
                self.assertEqual(build.signing_plan(event, signed, env), expected)
        refused = [
            ("push", True, {}, [organization, project, release]),
            ("push", True, {organization: "o", project: "p", test: "test-signing"}, [release]),
            ("push", True, dict(configured, **{uninstaller: "True"}), [uninstaller]),
            ("push", True, dict(configured, **{uninstaller: "yes"}), [uninstaller]),
            ("workflow_dispatch", False, {organization: "o", project: "p"}, [test]),
            ("workflow_dispatch", True, {test: "test-signing", release: "release-signing"},
             [organization, project]),
            ("workflow_dispatch", False, dict(configured, **{uninstaller: "1"}), [uninstaller]),
            ("workflow_dispatch", False, dict(configured, **{test: "test-signing\nmode=none"}),
             ["line break"]),
            ("pull_request", True, configured, ["pull_request"]),
            ("schedule", False, {}, ["schedule"]),
            ("Push", True, configured, ["Push"]),
        ]
        for event, signed, env, named in refused:
            with self.subTest(event=event, signed=signed, env=env):
                with self.assertRaises(build.BuildError) as raised:
                    build.signing_plan(event, signed, env)
                for name in named:
                    self.assertIn(name, str(raised.exception))

    def test_a_policy_is_only_ever_the_one_for_its_kind_of_run(self):
        names = build.SIGNPATH_VARIABLES
        seen = set()
        for event, signed, chosen, uninstaller in itertools.product(
                ("push", "workflow_dispatch"), (False, True), itertools.product((False, True), repeat=4),
                ("", "false", "true", "yes")):
            env = {name: name.lower() for name, on in zip(names, chosen) if on}
            env[names[4]] = uninstaller
            try:
                plan = build.signing_plan(event, signed, env)
            except build.BuildError:
                seen.add((event, signed, "refused"))
                continue
            seen.add((event, signed, plan["mode"]))
            with self.subTest(event=event, signed=signed, env=env):
                self.assertEqual(plan["mode"] == "release", event == "push" and signed)
                kinds = {"push": ("none", "release"), "workflow_dispatch": ("none", "test")}
                self.assertIn(plan["mode"], kinds[event])
                policy = {"none": "", "test": env.get(names[2]), "release": env.get(names[3])}[plan["mode"]]
                self.assertEqual(plan["policy"], policy)
                self.assertEqual(plan["uninstaller"],
                                 "true" if plan["mode"] != "none" and uninstaller == "true" else "false")
        # An unsigned tag push always builds unsigned, a signed one signs or is refused, and a manual run can
        # do any of the three.
        self.assertEqual(seen, {("push", False, "none"), ("push", True, "release"), ("push", True, "refused"),
                                ("workflow_dispatch", False, "none"), ("workflow_dispatch", False, "test"),
                                ("workflow_dispatch", False, "refused"), ("workflow_dispatch", True, "none"),
                                ("workflow_dispatch", True, "test"), ("workflow_dispatch", True, "refused")})

    def test_signing_on_the_command_line(self):
        organization, project, test, release, uninstaller = build.SIGNPATH_VARIABLES
        with tempfile.TemporaryDirectory() as folder:
            flag = Path(folder) / "signing.json"

            def run(event, signed, variables):
                flag.write_text(json.dumps({"installer_signed": signed}), encoding="utf-8")
                environment = dict(dict.fromkeys(build.SIGNPATH_VARIABLES, ""), **variables)
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(build, "SIGNING", flag), mock.patch.dict(os.environ, environment):
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = build.main(["signing", "--event", event])
                return code, out.getvalue(), err.getvalue()

            code, out, err = run("push", False, {organization: "o", project: "p", release: "release-signing"})
            self.assertEqual((code, out), (0, "mode=none\npolicy=\nuninstaller=false\n"))
            self.assertIn("isn't signed", err)
            code, out, err = run("workflow_dispatch", False,
                                 {organization: "o", project: "p", test: "test-signing", uninstaller: "true"})
            self.assertEqual((code, out), (0, "mode=test\npolicy=test-signing\nuninstaller=true\n"))
            self.assertIn("test-signing", err)
            code, out, err = run("push", True, {organization: "o", project: "p", release: "release-signing"})
            self.assertEqual((code, out), (0, "mode=release\npolicy=release-signing\nuninstaller=false\n"))
            # A refusal prints no output line, so the step fails before any signing step can read one.
            code, out, err = run("push", True, {})
            self.assertEqual((code, out), (1, ""))
            self.assertIn(release, err)
            flag.write_text('{"installer_signed": "yes"}', encoding="utf-8")
            with mock.patch.object(build, "SIGNING", flag), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(build.main(["signing", "--event", "workflow_dispatch"]), 1)

    def test_the_uninstaller_goes_out_to_be_signed_and_back_under_its_own_name(self):
        unsigned = b"MZ" + bytes(range(256)) * 40
        signed = unsigned + b"a certificate table"
        name = "uninst-6.7.1-0123456789.e32"
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            made, out = folder / "uninstaller", folder / "sign" / "uninstaller"
            back = folder / build.UNINSTALLER_NAME
            stdout, stderr = io.StringIO(), io.StringIO()

            def run(*argv):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    return build.main(list(argv))

            copy_out = ("uninstaller-out", "--dir", str(made), "--out", str(out))
            put_back = ("uninstaller-in", "--signed", str(back), "--dir", str(made))
            # No folder, then an empty one: refused, and no --out folder made.
            self.assertEqual(run(*copy_out), 1)
            made.mkdir()
            self.assertEqual(run(*copy_out), 1)
            self.assertFalse(out.exists())
            (made / name).write_bytes(unsigned)
            # What ISCC writes first and then renames, which isn't a second copy.
            (made / (name + ".tmp")).write_bytes(unsigned)
            self.assertEqual(run(*copy_out), 0)
            self.assertEqual([path.name for path in out.iterdir()], [build.UNINSTALLER_NAME])
            self.assertEqual((out / build.UNINSTALLER_NAME).read_bytes(), unsigned)
            self.assertIn(name, stdout.getvalue())
            self.assertEqual(run(*copy_out), 1)  # only into a fresh folder
            # Refused, leaving the unsigned file as it was: no signed file, one that isn't a program, and one
            # no larger than the unsigned file.
            for what, content in (("no file", None), ("not a program", b"PK" + signed[2:]),
                                  ("the same size", unsigned), ("smaller", unsigned[:-1])):
                with self.subTest(what):
                    if content is not None:
                        back.write_bytes(content)
                    self.assertEqual(run(*put_back), 1)
                    self.assertEqual((made / name).read_bytes(), unsigned)
            back.write_bytes(signed)
            second = made / "uninst-6.7.1-9876543210.e32"
            second.write_bytes(unsigned)
            self.assertEqual(run(*put_back), 1)  # two, and no telling which one the next compile looks for
            self.assertEqual(run("uninstaller-out", "--dir", str(made), "--out", str(folder / "other")), 1)
            second.unlink()
            self.assertEqual(run(*put_back), 0)
            self.assertEqual((made / name).read_bytes(), signed)
            self.assertEqual(sorted(path.name for path in made.iterdir()), [name, name + ".tmp"])
        for message in ("exactly one", "isn't empty", "there is no signed uninstaller",
                        "doesn't start with MZ", "carries no signature"):
            self.assertIn(message, stderr.getvalue())


class Workflow(unittest.TestCase):
    ACTION = "signpath/github-action-submit-signing-request@f6d04783b4569d051e0c80105fe66e82819d0092"
    PUSH = "github.event_name == 'push'"
    SIGNS = "steps.signing.outputs.mode != 'none'"
    SIGNS_UNINSTALLER = "steps.signing.outputs.uninstaller == 'true'"
    # Every step after checkout and setup-python, in the order they must run: a name for it here, a pattern
    # only that step matches, and the if: it runs under.
    ORDER = [
        ("check-tag", r"run: python packaging/build\.py check-tag ", PUSH),
        ("versions", r"run: python packaging/build\.py versions ", None),
        ("signing", r"run: python packaging/build\.py signing ", None),
        ("runtime", r"run: python packaging/build\.py runtime$", None),
        ("check", r"run: python packaging/build\.py check$", None),
        ("report", r"Resolve-Path build\\runtime", None),
        ("unit tests", r"build\\runtime\\python\.exe -m unittest", None),
        ("mark", r"run: python packaging/build\.py mark$", None),
        ("stage", r"run: python packaging/build\.py stage$", None),
        ("inno", r"^        id: inno$", None),
        ("first compile", r"please attach your digital signature", SIGNS_UNINSTALLER),
        ("uninstaller-out", r"run: python packaging/build\.py uninstaller-out$", SIGNS_UNINSTALLER),
        ("uninstaller upload", r"^        id: unsigned-uninstaller$", SIGNS_UNINSTALLER),
        ("uninstaller signing", r"^          artifact-configuration-slug: uninstaller$", SIGNS_UNINSTALLER),
        ("uninstaller check", r"^          SIGNED: build\\signed-uninstaller\\", SIGNS_UNINSTALLER),
        ("uninstaller-in", r"run: python packaging/build\.py uninstaller-in ", SIGNS_UNINSTALLER),
        ("compile", r"^\s*& \$env:ISCC @defines packaging\\tapewright\.iss$", None),
        ("unsigned upload", r"^        id: unsigned$", SIGNS),
        ("installer signing", r"^          artifact-configuration-slug: installer$", SIGNS),
        ("installer check", r"^          SIGNED: build\\signed\\TapewrightSetup\.exe$", SIGNS),
        ("sums", r"run: python packaging/build\.py sums ", None),
        ("upload", r"^          name: TapewrightSetup$", None),
        ("attest", r"uses: actions/attest@", PUSH),
        ("release", r"gh release create", PUSH),
    ]

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        cls.steps = _steps(cls.text)

    def find(self, label):
        pattern = {name: pattern for name, pattern, _ in self.ORDER}[label]
        found = [index for index, step in enumerate(self.steps) if re.search(pattern, step["text"], re.M)]
        self.assertEqual(len(found), 1, f"{label}: {len(found)} steps match {pattern}")
        return found[0]

    def step(self, label):
        return self.steps[self.find(label)]

    def test_it_calls_every_build_step_and_no_other(self):
        names = set(re.findall(r"run: python packaging/build\.py ([\w-]+)", self.text))
        self.assertEqual(names, set(build.COMMANDS))
        for name in names:
            with self.subTest(name), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as done:
                    build.main([name, "--help"])
                self.assertEqual(done.exception.code, 0)

    def test_the_steps_run_in_the_order_the_installer_needs(self):
        self.assertEqual([step.get("uses", "").split("@")[0] for step in self.steps[:2]],
                         ["actions/checkout", "actions/setup-python"])
        self.assertEqual([self.find(label) for label, _, _ in self.ORDER], list(range(2, len(self.steps))))

    def test_each_step_runs_only_under_its_condition(self):
        # The tag check, attestation and release only on a push; the uninstaller's pass only when it is
        # signed too, and the rest of signing whenever this run signs; everything else always, the runtime,
        # the compile and the final upload included. signing_plan never signs the uninstaller on a run that
        # signs nothing.
        for label, _, condition in self.ORDER:
            with self.subTest(label):
                self.assertEqual(self.step(label).get("if"), condition)
        for step in self.steps[:2]:
            self.assertNotIn("if", step)

    def test_signing_is_worked_out_once_from_the_flag_and_the_repository_variables(self):
        signing = self.step("signing")
        self.assertEqual(signing["id"], "signing")
        self.assertEqual(signing["run"], 'python packaging/build.py signing --event "$env:GITHUB_EVENT_NAME" '
                                         ">> $env:GITHUB_OUTPUT")
        self.assertEqual(signing["env"], {name: "${{ vars.%s }}" % name for name in build.SIGNPATH_VARIABLES})
        # Only signing_plan picks a policy and reads the uninstaller's switch, so nothing else names them.
        for name in build.SIGNPATH_VARIABLES[2:]:
            with self.subTest(name):
                self.assertEqual(self.text.count(name), 2)
                self.assertIn(f"{name}: ${{{{ vars.{name} }}}}", signing["text"])

    def test_the_job_has_the_permissions_and_the_time_signing_needs(self):
        permissions = self.text.split("\npermissions:\n", 1)[1].split("\n\n", 1)[0]
        granted = dict(re.findall(r"^  ([\w-]+): (\w+)", permissions, re.M))
        self.assertEqual(granted, {"contents": "write", "id-token": "write", "attestations": "write",
                                   "actions": "read"})
        timeouts = re.findall(r"^    timeout-minutes: (.*)$", self.text, re.M)
        self.assertEqual(timeouts, ["${{ vars.SIGNPATH_PROJECT_SLUG != '' && 240 || 45 }}"])
        signing, unsigned = map(int, re.findall(r"\d+", timeouts[0]))
        waits = [int(step["with"]["wait-for-completion-timeout-in-seconds"]) for step in self.steps
                 if step.get("uses") == self.ACTION]
        self.assertEqual(waits, [3600, 3600])
        # Both requests waiting their longest still leave the unsigned build's time, inside the 6 hours GitHub
        # allows a job on its own runners.
        self.assertLessEqual(unsigned + sum(waits) // 60, signing)
        self.assertLessEqual(signing, 360)

    def test_it_runs_on_githubs_own_runner_with_nothing_kept_from_earlier_builds(self):
        self.assertEqual(re.findall(r"^    runs-on: (.*)$", self.text, re.M), ["windows-latest"])
        jobs = self.text.split("\njobs:\n", 1)[1]
        self.assertEqual(re.findall(r"^  ([\w-]+):$", jobs, re.M), ["installer"])
        for word in ("self-hosted", "actions/cache", "cache:"):
            self.assertNotIn(word, self.text)

    def test_signpaths_action_is_pinned_by_commit_and_given_what_it_needs(self):
        commit = self.ACTION.split("@")[1]
        self.assertRegex(commit, r"\A[0-9a-f]{40}\Z")
        self.assertEqual(re.findall(r"uses: (signpath/\S+)", self.text), [self.ACTION] * 2)
        iss = Script()
        names = []
        for slug, upload, directory, name in (
                ("uninstaller", "uninstaller upload", "build/signed-uninstaller", build.UNINSTALLER_NAME),
                ("installer", "unsigned upload", "build/signed", iss.setup["OutputBaseFilename"] + ".exe")):
            with self.subTest(slug):
                uploaded, submitted = self.step(upload), self.step(slug + " signing")
                self.assertEqual(submitted["uses"], self.ACTION)
                self.assertEqual(submitted["with"], {
                    "api-token": "${{ secrets.SIGNPATH_API_TOKEN }}",
                    "organization-id": "${{ vars.SIGNPATH_ORGANIZATION_ID }}",
                    "project-slug": "${{ vars.SIGNPATH_PROJECT_SLUG }}",
                    "signing-policy-slug": "${{ steps.signing.outputs.policy }}",
                    "artifact-configuration-slug": slug,
                    "github-artifact-id": "${{ steps.%s.outputs.artifact-id }}" % uploaded["id"],
                    "wait-for-completion": "true",
                    "wait-for-completion-timeout-in-seconds": "3600",
                    "output-artifact-directory": directory,
                    "parameters": "version: ${{ toJSON(steps.versions.outputs.app) }}",
                })
                # Uploaded zipped, as the configuration's zip-file root expects, holding the one file its
                # pe-file names, under that name.
                self.assertTrue(uploaded["uses"].startswith("actions/upload-artifact@"))
                self.assertNotIn("archive", uploaded["with"])
                self.assertEqual(Path(uploaded["with"]["path"]).name, name)
                # Kept for a day, upload-artifact's shortest, since nobody should download an unsigned copy
                # from a signing run later.
                self.assertEqual(uploaded["with"]["retention-days"], "1")
                xml = ROOT / ".signpath" / "artifact-configurations" / (slug + ".xml")
                where = f"{SIGNPATH_NS}zip-file/{SIGNPATH_NS}pe-file"
                self.assertEqual(ElementTree.parse(xml).getroot().find(where).get("path"), name)
                names.append(uploaded["with"]["name"])
        names.append(self.step("upload")["with"]["name"])
        self.assertEqual(len(set(names)), 3, names)
        # The installer people download keeps the repository's own retention.
        self.assertNotIn("retention-days", self.step("upload")["with"])

    def test_the_api_token_goes_only_to_signpaths_action(self):
        self.assertNotIn("secrets.", self.text.split("\n    steps:\n", 1)[0])
        places = []
        for step in self.steps:
            for key, value in step.items():
                pairs = value.items() if isinstance(value, dict) else [(None, value)]
                places += [(key, name) for name, inner in pairs if key != "text" and "secrets." in inner]
        self.assertEqual(places, [("with", "api-token")] * 2)

    def test_the_uninstaller_pass_accepts_only_the_stop_that_asks_for_a_signature(self):
        first = self.step("first compile")["run"]
        self.assertEqual(re.findall(r"/D(\w+)", first), ["AppVersion", "PyVersion", "SignUninstaller"])
        asked = "please attach your digital signature to the following executable file"
        self.assertIn(f'$asked = "{asked}"', first)
        self.assertIn('if ($code -ne 2 -or -not ($output -join "`n").Contains($asked)) {', first)
        self.assertTrue(first.endswith("\nexit 0"))
        # The script leaves the file where uninstaller-out and uninstaller-in look, and each file goes where
        # the next step expects it.
        folder = Script().conditional["SignUninstaller"]["SignedUninstallerDir"]
        self.assertEqual((PACKAGING / folder.replace("\\", "/")).resolve(), build.UNINSTALLER)
        self.assertEqual(self.step("uninstaller-out")["run"], "python packaging/build.py uninstaller-out")
        uploaded = self.step("uninstaller upload")["with"]["path"]
        self.assertEqual(ROOT / uploaded, build.UNINSTALLER_OUT / build.UNINSTALLER_NAME)
        directory = self.step("uninstaller signing")["with"]["output-artifact-directory"]
        signed = directory + "/" + build.UNINSTALLER_NAME
        self.assertEqual(self.step("uninstaller check")["env"]["SIGNED"], signed.replace("/", "\\"))
        self.assertEqual(self.step("uninstaller-in")["run"],
                         "python packaging/build.py uninstaller-in --signed " + signed)

    def test_the_compiler_gets_the_defines_the_script_expects(self):
        iss = Script()
        self.assertEqual(set(iss.defines), {"AppVersion", "PyVersion"})
        self.assertEqual(list(iss.conditional), ["SignUninstaller"])
        compile_step = self.step("compile")
        lines = [line.strip() for line in compile_step["run"].splitlines()]
        self.assertEqual([line for line in lines if "/D" in line],
                         ['$defines = "/DAppVersion=$env:APP_VERSION", "/DPyVersion=$env:PY_VERSION"',
                          """if ($env:SIGN_UNINSTALLER -eq 'true') { $defines += "/DSignUninstaller" }"""])
        self.assertEqual(compile_step["env"]["SIGN_UNINSTALLER"], "${{ steps.signing.outputs.uninstaller }}")
        for label in ("first compile", "compile"):
            with self.subTest(label):
                step = self.step(label)
                self.assertEqual({key: step["env"][key] for key in ("ISCC", "APP_VERSION", "PY_VERSION")},
                                 {"ISCC": "${{ steps.inno.outputs.iscc }}",
                                  "APP_VERSION": "${{ steps.versions.outputs.app }}",
                                  "PY_VERSION": "${{ steps.versions.outputs.python }}"})
                self.assertIn("packaging\\tapewright.iss", step["run"])
        self.assertIn('"iscc=$($iscc.FullName)" >> $env:GITHUB_OUTPUT', self.step("inno")["run"])
        # Inno Setup's programs carry no version resource (ISCC.exe 6.7.3 reads 0.0.0.0), so a check on
        # VersionInfo refuses every Inno Setup; tapewright.iss checks the version itself.
        self.assertNotIn("VersionInfo", self.step("inno")["run"])

    def test_the_signature_is_checked_before_anything_describes_the_installer(self):
        uninstaller, installer = self.step("uninstaller check"), self.step("installer check")
        # One set of rules for both, after which the installer's step puts the file it checked in dist.
        self.assertTrue(installer["run"].startswith(uninstaller["run"]))
        self.assertEqual(installer["run"][len(uninstaller["run"]):].strip(),
                         "Copy-Item -LiteralPath $env:SIGNED -Destination dist\\TapewrightSetup.exe -Force")
        rules = uninstaller["run"]
        release = "if ($env:MODE -eq 'release') {"
        test = "} elseif (-not $signature.TimeStamperCertificate) {"
        self.assertEqual((rules.count(release), rules.count(test)), (1, 1))
        always, on_release = rules[:rules.index(release)], rules[rules.index(release):rules.index(test)]
        on_test = rules[rules.index(test):]
        # Every run needs a signature, on a file that hasn't changed since it was signed.
        for part in ("$signature = Get-AuthenticodeSignature -LiteralPath $env:SIGNED",
                     "if (-not $signature.SignerCertificate) { throw",
                     "if ($signature.Status -eq 'HashMismatch') { throw"):
            self.assertIn(part, always)
        # A timestamp, Valid and SignPath Foundation are required of a release only: a test certificate isn't
        # trusted, and SignPath's documentation doesn't say whether a test signature is timestamped.
        for part in ("if (-not $signature.TimeStamperCertificate) { throw",
                     "if ($signature.Status -ne 'Valid') { throw",
                     "if (-not $subject.StartsWith('CN=SignPath Foundation', 'Ordinal')) {"):
            self.assertIn(part, on_release)
        self.assertEqual((always.count("throw"), on_release.count("throw")), (2, 3))
        # On a test run, a missing timestamp prints a warning annotation, which fails nothing, and that ends
        # the rules.
        lines = [line.strip() for line in on_test.splitlines()]
        self.assertEqual((lines[0], lines[-1]), (test, "}"))
        self.assertRegex(lines[-2], r'\A"::warning title=[^:,]+::\$warning"\Z')
        self.assertIn("test signature has no timestamp", lines[-3])
        for word in ("throw", "exit", "Write-Error", "$LASTEXITCODE"):
            self.assertNotIn(word, on_test)
        for step in (uninstaller, installer):
            self.assertEqual(step["env"]["MODE"], "${{ steps.signing.outputs.mode }}")
        directory = self.step("installer signing")["with"]["output-artifact-directory"]
        self.assertEqual(installer["env"]["SIGNED"], directory.replace("/", "\\") + "\\TapewrightSetup.exe")
        self.assertEqual(self.step("unsigned upload")["with"]["path"], "dist/TapewrightSetup.exe")
        # Then the sums, the artifact, the attestation and the release, all naming that dist file, and nothing
        # after them.
        self.assertEqual(self.step("sums")["run"], "python packaging/build.py sums dist/TapewrightSetup.exe")
        self.assertEqual(self.step("upload")["with"]["path"], "dist/")
        self.assertEqual(self.step("attest")["with"]["subject-path"], "dist/TapewrightSetup.exe")
        self.assertIn("dist/TapewrightSetup.exe dist/SHA256SUMS.txt", self.step("release")["run"])
        last = ("installer check", "sums", "upload", "attest", "release")
        self.assertEqual([self.find(label) for label in last], list(range(len(self.steps)))[-5:])

    def test_the_runtime_report_changes_nothing_and_fails_nothing(self):
        report = self.step("report")
        self.assertEqual(report["continue-on-error"], "true")
        self.assertIn("-Include *.exe, *.dll, *.pyd", report["run"])
        self.assertIn("(Get-AuthenticodeSignature -LiteralPath $file.FullName).Status", report["run"])
        for word in ("Remove-Item", "Copy-Item", "Move-Item", "New-Item", "Set-", "Out-File", ">", "throw",
                     "exit"):
            with self.subTest(word):
                self.assertNotIn(word, report["run"])

    def test_the_unit_tests_keep_their_settings_and_tools_in_the_runners_temp_folder(self):
        tests = self.step("unit tests")["env"]
        self.assertEqual(tests["TAPEWRIGHT_CONFIG_DIR"], "${{ runner.temp }}\\tapewright-config")
        self.assertEqual(tests["TAPEWRIGHT_TOOLS_DIR"], "${{ runner.temp }}\\tapewright-tools")

    def test_only_a_pushed_tag_is_checked_attested_and_released(self):
        self.assertIn('  push:\n    tags: ["v*"]', self.text)
        self.assertNotIn("branches:", self.text)
        release = self.step("release")["run"]
        for part in ("dist/TapewrightSetup.exe dist/SHA256SUMS.txt", "--draft",
                     "--notes-file packaging/release-notes.md"):
            self.assertIn(part, release)


class Signing(unittest.TestCase):
    """What README, the release notes and Help say about signing, held to packaging/signing.json, and the
    copies of the artifact configurations entered in SignPath's project."""

    LINKED = ("Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by "
              "[SignPath Foundation](https://signpath.org)")

    @classmethod
    def setUpClass(cls):
        cls.signed = build.load_signing()
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.notes = (PACKAGING / "release-notes.md").read_text(encoding="utf-8")
        cls.help_source = (ROOT / "tapewright" / "help_content.py").read_text(encoding="utf-8")
        warned = [topic for topic in help_content.TOPICS if topic["title"] == "Windows warned me about it"]
        assert len(warned) == 1, "Help has no one topic called Windows warned me about it"
        cls.warned = _help_words(warned)

    def test_nothing_says_signed_before_signing_json_does(self):
        readme, notes, help_words = _prose(self.readme), _prose(self.notes), _help_words(help_content.TOPICS)
        install = _prose(_section(self.readme, "Install on Windows"))
        policy = _prose(_section(self.readme, "Code signing policy"))
        if not self.signed:
            self.assertIn("The installer isn't signed yet", install)
            self.assertIn("isn't signed yet", policy)
            self.assertIn("installer isn't signed yet", self.warned)
            self.assertIn("installer isn't signed yet", notes)
            # SignPath's attribution line, however it is marked up or capitalized, so the raw text: _prose
            # unwraps only inline links. help_content.py's source counts too, comments and all. AGENTS.md
            # quotes the line on purpose, so it isn't read here.
            raw = {"README.md": self.readme, "packaging/release-notes.md": self.notes,
                   "tapewright/help_content.py": self.help_source, "Help": help_words}
            for name, text in raw.items():
                for words in ("signpath.io", "free code signing"):
                    with self.subTest(name, words=words):
                        self.assertNotIn(words, text.casefold())
            # The release notes and Help have no reason to name SignPath at all until the installer is signed.
            for name in ("packaging/release-notes.md", "tapewright/help_content.py", "Help"):
                with self.subTest(name, words="signpath"):
                    self.assertNotIn("signpath", raw[name].casefold())
            # README has to name SignPath Foundation, where Tapewright means to apply, so it is held to making
            # no claim: no "signed by", and no "is signed" but in a clause that asks whether, or says until or
            # if, as README's own "whether the installer is signed" and "until a release says its installer
            # is signed, it isn't" do. A comma after the words doesn't make a claim a question, so the whole
            # clause is read.
            with self.subTest("README.md", words="signed by"):
                self.assertNotIn("signed by", readme.casefold())
            claims = []
            for clause in re.split(r"[.;:]", readme.casefold()):
                said = re.search(r"\bis signed\b", clause)
                if said and not re.search(r"\b(whether|until|if)\b", clause[:said.start()]):
                    claims.append(clause.strip())
            self.assertEqual(claims, [], "README says the installer is signed")
        else:
            self.assertIn(self.LINKED, " ".join(self.readme.split()))
            self.assertNotIn("intends to apply", policy)
            for name, text in (("README.md", readme), ("packaging/release-notes.md", notes),
                               ("Help's Windows warned me about it", self.warned)):
                with self.subTest(name, words="SignPath Foundation"):
                    self.assertIn("SignPath Foundation", text)
            for name, text in (("README.md", readme), ("packaging/release-notes.md", notes),
                               ("Help", help_words)):
                with self.subTest(name, words="isn't signed"):
                    self.assertNotIn("isn't signed", text.casefold())

    def test_the_policy_is_linked_whichever_way_signing_json_says(self):
        self.assertIn("\n## Privacy\n", self.readme)
        self.assertIn("\n## Code signing policy\n", self.readme)
        self.assertIn("](#privacy)", _section(self.readme, "Code signing policy"))
        self.assertIn("](#code-signing-policy)", _section(self.readme, "Install on Windows"))
        self.assertIn("https://github.com/angrysandhill/tapewright#code-signing-policy", self.notes)

    def test_signpath_is_asked_to_sign_only_tapewright_at_the_version_built(self):
        folder = ROOT / ".signpath" / "artifact-configurations"
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["installer.xml", "uninstaller.xml"])
        iss = Script()
        self.assertEqual(iss.setup["VersionInfoProductName"], APP_NAME)
        self.assertEqual(iss.setup["VersionInfoProductVersion"], "{#AppVersion}")
        files = {"installer": iss.setup["OutputBaseFilename"] + ".exe", "uninstaller": build.UNINSTALLER_NAME}
        ns = SIGNPATH_NS
        for slug, name in files.items():
            with self.subTest(slug):
                text = (folder / (slug + ".xml")).read_text(encoding="utf-8")
                # The SPDX lines, and that this is only a copy of what SignPath's project holds, in a comment.
                self.assertTrue(text.startswith("<!--\nSPDX-FileCopyrightText: 2026 AngrySandhill\n"
                                                "SPDX-License-Identifier: GPL-3.0-or-later\n"))
                self.assertIn("SignPath doesn't read this file. It is a copy", text)
                self.assertIn(f'under the slug "{slug}"', " ".join(text.split()))
                root = ElementTree.fromstring(text)
                self.assertEqual(root.tag, ns + "artifact-configuration")
                self.assertEqual([child.tag for child in root], [ns + "parameters", ns + "zip-file"])
                self.assertEqual([(parameter.tag, parameter.attrib) for parameter in root[0]],
                                 [(ns + "parameter", {"name": "version", "required": "true"})])
                self.assertEqual([child.tag for child in root[1]], [ns + "pe-file"])
                pe_file = root[1][0]
                self.assertEqual(pe_file.attrib,
                                 {"path": name, "product-name": APP_NAME, "product-version": "${version}"})
                self.assertEqual([(child.tag, child.attrib, list(child)) for child in pe_file],
                                 [(ns + "authenticode-sign", {}, [])])


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
