"""Portable executable discovery shared by backend consumers."""

from __future__ import annotations

import shutil
from pathlib import Path


HOMEBREW_BIN_DIRECTORIES = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))


def resolve_executable(name: str) -> str | None:
	"""Prefer PATH, then the standard Apple Silicon and Intel Homebrew locations."""
	candidates = [shutil.which(name), *(str(directory / name) for directory in HOMEBREW_BIN_DIRECTORIES)]
	for candidate in candidates:
		if candidate and Path(candidate).is_file():
			return str(candidate)
	return None
