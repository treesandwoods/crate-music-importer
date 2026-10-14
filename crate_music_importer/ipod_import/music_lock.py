"""Serialize Music operations across processes, roots, and nested adapter calls."""

import contextlib
import fcntl
import threading

from crate_music_importer.ipod_import.dependency_lock import state_directory


_held = threading.local()


@contextlib.contextmanager
def music_operation_lock():
	# The same thread may enter through CLI, cache rebuild, then the adapter.
	if getattr(_held, "depth", 0):
		_held.depth += 1
		try:
			yield
		finally:
			_held.depth -= 1
		return
	state = state_directory()
	state.mkdir(parents=True, exist_ok=True)
	with (state / "music-operation.lock").open("a+") as handle:
		fcntl.flock(handle, fcntl.LOCK_EX)
		_held.depth = 1
		try:
			yield
		finally:
			_held.depth = 0
			fcntl.flock(handle, fcntl.LOCK_UN)
