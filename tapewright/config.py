# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings: one JSON file, replaced atomically. A damaged file never stops the app starting."""

import copy
import json
import os
import sys
from pathlib import Path

from tapewright import APP_NAME

CHOICES = {
    "ytdlp_channel": ("stable", "nightly"),
    "outdated_policy": ("warn", "block"),
    "js_runtime": ("auto", "deno", "node", "none"),
    "mp3_quality": ("V0", "V2", "320k", "192k", "128k"),
    "mp4_mode": ("compatible", "best"),
    "mp4_max_height": ("best", "2160", "1440", "1080", "720", "480", "360"),
}

DEFAULTS = {
    # Dependencies
    "check_on_startup": True,
    "auto_update_ytdlp": False,
    "ytdlp_channel": "stable",
    "ytdlp_extras": True,
    "outdated_policy": "warn",
    "offline_max_age_days": 60,
    "js_runtime": "auto",
    "latest_cache": {},
    # The setup screen: True once someone finished it, or the first check found nothing to install
    "setup_done": False,
    # Conversion tabs
    "mp3_out_dir": "",
    "mp4_out_dir": "",
    "mp3_quality": "V0",
    "mp4_mode": "compatible",
    "mp4_max_height": "1080",
    "mp3_thumbnail": True,
    "mp4_thumbnail": True,
    "mp3_playlist": False,
    "mp4_playlist": False,
    # Help tab
    "help_text_size": 11,
}


def config_dir():
    override = os.environ.get("TAPEWRIGHT_CONFIG_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / APP_NAME.lower()


def default_download_dir():
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def _valid(key, value):
    default = DEFAULTS[key]
    if key in CHOICES:
        return value in CHOICES[key]
    # bool before int, and exactly: isinstance(True, int) is True.
    if type(default) is bool:
        return type(value) is bool
    if type(default) is int:
        return type(value) is int and 1 <= value <= 3650
    if type(default) is dict:
        return isinstance(value, dict)
    return isinstance(value, str)


class Settings:
    def __init__(self, path=None):
        self.path = Path(path) if path else config_dir() / "settings.json"
        self.data = copy.deepcopy(DEFAULTS)
        # A sentence for the UI when the file on disk could not be used.
        self.problem = None

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def load(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            self.problem = f"Couldn't read the settings file ({e}), so defaults are in use."
            return
        try:
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                raise ValueError("it is not a JSON object")
        except ValueError as e:
            # Set the file aside rather than overwrite it on the next save: it may be
            # hand-edited, and the person who broke it will want to see what they wrote.
            bad = self.path.with_name(self.path.name + ".bad")
            try:
                os.replace(self.path, bad)
                kept = f" The old file was kept as {bad.name}."
            except OSError:
                kept = ""
            self.problem = f"The settings file was unreadable ({e}), so defaults are in use.{kept}"
            return
        for key, value in loaded.items():
            if key not in DEFAULTS:
                self.data[key] = value  # written by a newer version; keep it
            elif _valid(key, value):
                self.data[key] = value

    def save(self):
        """Write the file. Returns None on success, or a sentence describing the failure."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self.path)
        except OSError as e:
            return f"Couldn't save settings to {self.path}: {e}"
        return None
