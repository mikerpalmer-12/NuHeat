"""Persistent configuration that survives container restarts.

Saves runtime changes (account, settings) to a JSON file in the Docker
volume so they persist across restarts and rebuilds. On startup, the
persisted config overrides .env defaults for any fields that are present.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = "/app/logs"  # same Docker volume as activity logs


class PersistentConfig:
    """Reads and writes a config.json file for persistent settings."""

    def __init__(self):
        config_dir = Path(os.environ.get("NUHEAT_LOG_DIR", DEFAULT_CONFIG_DIR))
        config_dir.mkdir(parents=True, exist_ok=True)
        self._path = config_dir / "config.json"
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
                logger.info("Loaded persistent config from %s", self._path)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Could not load persistent config: %s", e)
                self._data = {}

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._data, indent=2))
        except OSError as e:
            logger.error("Could not save persistent config: %s", e)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._save()

    def update(self, values: dict[str, Any]) -> None:
        self._data.update(values)
        self._save()

    def get_all(self) -> dict[str, Any]:
        return dict(self._data)


persistent_config = PersistentConfig()
