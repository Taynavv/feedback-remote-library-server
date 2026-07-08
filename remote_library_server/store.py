# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock

from .models import utc_now_iso

_SETTINGS_KEYS = ("enabled", "host", "port", "sourceName", "shareNamToneAssets", "authToken", "irohEnabled")


def _default_settings() -> dict:
    return {
        "enabled": False,
        "host": "",
        "port": "",
        "sourceName": "",
        "shareNamToneAssets": False,
        "authToken": "",
        "irohEnabled": False,
    }


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class RemoteLibraryServerStore:
    def __init__(self, config_dir: Path) -> None:
        self.root = Path(config_dir) / "remote_library_server"
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.root / "settings.json"
        self.state_path = self.root / "state.json"
        self._lock = RLock()

    def load_settings(self) -> dict:
        settings = _default_settings()
        with self._lock:
            if self.settings_path.exists():
                try:
                    loaded = json.loads(self.settings_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        settings.update(loaded)
                except (json.JSONDecodeError, OSError):
                    pass
        return {key: settings.get(key) for key in _SETTINGS_KEYS}

    def save_settings(self, data: dict) -> dict:
        with self._lock:
            settings = self.load_settings()
            settings.update({key: value for key, value in data.items() if value is not None and key in _SETTINGS_KEYS})
            _atomic_write(self.settings_path, json.dumps(settings, indent=2, sort_keys=True))
        return settings

    def _load_state(self) -> dict:
        with self._lock:
            if not self.state_path.exists():
                return {"activity": []}
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {"activity": []}
        if not isinstance(state, dict):
            return {"activity": []}
        state.setdefault("activity", [])
        return state

    def _save_state(self, state: dict) -> None:
        with self._lock:
            _atomic_write(self.state_path, json.dumps(state, indent=2, sort_keys=True))

    def add_activity(self, event_type: str, outcome: str, message: str, **extra) -> None:
        with self._lock:
            state = self._load_state()
            activity = list(state.get("activity", []))
            activity.append({
                "eventType": event_type,
                "outcome": outcome,
                "message": message,
                "createdAt": utc_now_iso(),
                **extra,
            })
            state["activity"] = activity[-200:]
            self._save_state(state)

    def list_activity(self) -> list[dict]:
        return list(reversed(self._load_state().get("activity", [])))