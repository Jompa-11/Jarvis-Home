"""User-adjustable runtime settings, persisted locally in ``data/settings.json``.

Defaults come from the configuration file. Values changed in the UI override
them and survive restarts.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ..config import JarvisConfig, Sensitivity

log = logging.getLogger(__name__)


class RuntimeSettings(BaseModel):
    active_profile: str
    dry_run_computer: bool
    dry_run_smart_home: bool = False
    microphone_enabled: bool
    camera_enabled: bool
    local_only: bool
    cloud_max_sensitivity: Sensitivity
    # Read replies aloud with the browser's built-in speech synthesis.
    speak_replies: bool = False

    def dry_run_categories(self) -> frozenset[str]:
        cats = set()
        if self.dry_run_computer:
            cats.add("computer")
        if self.dry_run_smart_home:
            cats.update({"smart_home", "media"})
        return frozenset(cats)


class SettingsStore:
    def __init__(self, cfg: JarvisConfig, path: Path | None) -> None:
        self._cfg = cfg
        self._path = path
        defaults = RuntimeSettings(
            active_profile=cfg.default_profile,
            dry_run_computer=cfg.computer.dry_run,
            microphone_enabled=cfg.privacy.microphone_enabled,
            camera_enabled=cfg.privacy.camera_enabled,
            local_only=cfg.privacy.local_only,
            cloud_max_sensitivity=cfg.privacy.cloud_max_sensitivity,
        )
        self.current = defaults
        if path and path.exists():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                merged = {**defaults.model_dump(), **{k: v for k, v in stored.items() if k in RuntimeSettings.model_fields}}
                candidate = RuntimeSettings.model_validate(merged)
                if cfg.profile(candidate.active_profile) is None:
                    candidate.active_profile = cfg.default_profile
                self.current = candidate
            except (OSError, json.JSONDecodeError, ValidationError):
                log.warning("Stored settings are invalid; using defaults")

    def update(self, patch: dict[str, Any]) -> tuple[RuntimeSettings, set[str]]:
        unknown = set(patch) - set(RuntimeSettings.model_fields)
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
        candidate = RuntimeSettings.model_validate({**self.current.model_dump(), **patch})
        if self._cfg.profile(candidate.active_profile) is None:
            raise ValueError(f"Unknown profile '{candidate.active_profile}'")
        changed = {k for k in patch if getattr(self.current, k) != getattr(candidate, k)}
        self.current = candidate
        self._save()
        return candidate, changed

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(self.current.model_dump_json(indent=2), encoding="utf-8")
        except OSError:
            log.warning("Could not persist settings")
