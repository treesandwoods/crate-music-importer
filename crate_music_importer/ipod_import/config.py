"""Per-user paths shared by the CLI, Raycast, and detached workers."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def config_path() -> Path:
	return Path(os.environ.get("CRATE_CONFIG_FILE") or Path.home() / "Library/Application Support/Crate Music Importer/config.json").expanduser()


def load_config() -> dict[str, Any]:
	path = config_path()
	if not path.exists():
		return {}
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError) as exc:
		raise ValueError(f"Cannot read Crate configuration at {path}: {exc}") from exc
	if not isinstance(value, dict):
		raise ValueError(f"Crate configuration must be a JSON object: {path}")
	return value


def configured_path(key: str, environment: str, default: Path) -> Path:
	value = os.environ.get(environment) or load_config().get(key) or str(default)
	if not isinstance(value, str):
		raise ValueError(f"{key} must be an absolute path or a path beginning with ~/.")
	path = Path(value).expanduser()
	if not path.is_absolute():
		raise ValueError(f"{key} must be an absolute path or a path beginning with ~/.")
	return path
