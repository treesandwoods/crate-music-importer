"""Coordinate this user's tool consumers and updater, independently of Music state."""
import contextlib
import fcntl
import os
from pathlib import Path
from crate_music_importer.ipod_import.config import config_path


class DependenciesBusyError(RuntimeError):
	pass


def state_directory() -> Path:
	return Path(os.environ.get("CRATE_APPLICATION_STATE") or config_path().parent)


@contextlib.contextmanager
def dependency_lock(*, exclusive: bool = False):
	state = state_directory()
	state.mkdir(parents=True, exist_ok=True)
	with (state / "dependencies.lock").open("a+") as handle:
		try:
			fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
		except BlockingIOError as exc:
			raise DependenciesBusyError("Crate Music Importer is using or updating downloader tools. Wait for active work to finish and retry.") from exc
		try:
			yield
		finally:
			fcntl.flock(handle, fcntl.LOCK_UN)
