"""Durable preview controller; reading a saved result never starts a scan."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from typing import Any

from crate_music_importer.ipod_import.catalog import atomic_json
from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest
from crate_music_importer.ipod_import.preservation import capture_baseline, PreservationReader
from crate_music_importer.ipod_import.unification import build_preview, save_preview


def status(paths: ManagedPaths) -> dict[str, Any]:
	state_path = paths.state_dir / "unification-worker.json"
	state = json.loads(state_path.read_text()) if state_path.exists() else {"status": "idle"}
	if state.get("status") == "running":
		try:
			os.kill(state["pid"], 0)
		except ProcessLookupError:
			state.update(status="failed", error="Preview was interrupted. Run a new scan.")
	last = paths.state_dir / "unification-last.json"
	preview = json.loads(last.read_text()) if last.exists() else None
	if preview:
		# Keep the polling response bounded; the complete evidence stays on disk.
		state["preview"] = {key: preview[key] for key in ("preview_id", "ready", "blockers", "collisions", "orphans", "temporary_audio_bytes", "backup_bytes", "possible_recording_duplicates")}
		state["preview"]["report_path"] = str(last)
		state["preview"]["tracks"] = [{key: row.get(key) for key in ("persistent_id", "source", "target", "status")} for row in preview["tracks"]]
	else:
		state["preview"] = None
	if state.get("preview_id"):
		journal = paths.state_dir / "unification" / state["preview_id"] / "journal.json"
		if journal.exists():
			state["journal"] = json.loads(journal.read_text())
	return state


def start(paths: ManagedPaths, operation: str = "preview", preview_id: str | None = None) -> dict[str, Any]:
	paths.state_dir.mkdir(parents=True, exist_ok=True)
	with (paths.state_dir / "unification-worker.lock").open("a+") as lock:
		fcntl.flock(lock, fcntl.LOCK_EX)
		current = status(paths)
		if current["status"] == "running":
			return current
		if operation != "preview":
			import re
			if not preview_id or not re.fullmatch(r"[a-f0-9]{32}", preview_id):
				raise ValueError("Select an exact saved preview.")
			preview = json.loads((paths.state_dir / "unification" / (preview_id + ".json")).read_text())
			if not preview.get("ready"):
				raise ValueError("Resolve the preview prerequisites before organization.")
		run_id = uuid.uuid4().hex
		process = subprocess.Popen([sys.executable, "-m", __name__, run_id], cwd=Path(__file__).resolve().parents[2],
			env={**os.environ, "CRATE_MANAGED_ROOT": str(paths.root)}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL, start_new_session=True)
		atomic_json(paths.state_dir / "unification-worker.json", {"run_id": run_id, "pid": process.pid, "status": "running", "operation": operation, "preview_id": preview_id})
	return status(paths)


def run(paths: ManagedPaths, run_id: str) -> None:
	with (paths.state_dir / "unification-worker.lock").open("a+") as lock:
		fcntl.flock(lock, fcntl.LOCK_EX)
		state = status(paths)
		state.pop("preview", None)
		if state.get("run_id") != run_id or state.get("pid") != os.getpid():
			return
	try:
		from crate_music_importer.ipod_import.dependency_lock import dependency_lock
		def progress(event: dict[str, Any]) -> None:
			state.update(event)
			atomic_json(paths.state_dir / "unification-worker.json", state)
		if state.get("operation", "preview") == "preview":
			with dependency_lock():
				baseline = capture_baseline()
				preview = build_preview(paths, load_manifest(paths), baseline, on_progress=progress)
				save_preview(paths, preview)
		else:
			from crate_music_importer.ipod_import.unification import migrate
			from crate_music_importer.ipod_import.music import relink_music_file_track, relink_music_file_tracks
			from crate_music_importer.ipod_import.reconciliation import MUSIC_LIBRARY_PACKAGE
			preview = json.loads((paths.state_dir / "unification" / (state["preview_id"] + ".json")).read_text())
			journal = migrate(paths, preview, snapshot=PreservationReader(), relink=relink_music_file_track,
				music_package=MUSIC_LIBRARY_PACKAGE, batch_size=100, relink_batch=relink_music_file_tracks, rollback=state["operation"] == "rollback")
			state["journal"] = journal
		state.update(status="complete")
	except Exception as exc:
		state.update(status="failed", error=str(exc))
	atomic_json(paths.state_dir / "unification-worker.json", state)


if __name__ == "__main__":
	run(ManagedPaths(), sys.argv[1])
