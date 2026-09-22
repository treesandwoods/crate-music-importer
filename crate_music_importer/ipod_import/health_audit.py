"""Durable, read-only health worker and short-lived control operations."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from itertools import combinations
from typing import Any
from datetime import datetime, timezone

from crate_music_importer.ipod_import.health import DUPLICATE_DISMISSALS, apply_duplicate_dismissals, build_health_report, duplicate_pair, failed_health_report, load_duplicate_dismissals, save_health_report
from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest
from crate_music_importer.ipod_import.music import scan_music_library_for_health
from crate_music_importer.ipod_import.music_cache import refresh_music_cache
from crate_music_importer.ipod_import.dependency_lock import dependency_lock


def _now() -> str:
	return datetime.now(timezone.utc).isoformat()


def _read(path: Path) -> dict[str, Any]:
	try:
		return json.loads(path.read_text())
	except FileNotFoundError:
		return {}


def _save(paths: ManagedPaths, state: dict[str, Any], filename: str = "health-audit.json") -> None:
	# Reuse the report writer's fsync/replace guarantees for controller snapshots.
	from tempfile import NamedTemporaryFile
	temporary = None
	try:
		with NamedTemporaryFile("w", dir=paths.state_dir, prefix="health-state-", delete=False) as handle:
			temporary = handle.name
			json.dump(state, handle)
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary, paths.state_dir / filename)
	finally:
		if temporary:
			Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def _lock(paths: ManagedPaths):
	paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
	with (paths.state_dir / "health-audit.lock").open("a+") as handle:
		fcntl.flock(handle, fcntl.LOCK_EX)
		yield


def _alive(pid: int | None) -> bool:
	if not pid:
		return False
	try:
		os.kill(pid, 0)
		return True
	except ProcessLookupError:
		return False
	except PermissionError:
		return True


def _load(paths: ManagedPaths, include_report: bool = True) -> dict[str, Any]:
	state = _read(paths.state_dir / "health-audit.json") or {
		"status": "idle", "mode": "normal", "pid": None, "startedAt": None,
		"finishedAt": None, "updatedAt": None, "phase": "Idle", "checked": 0, "total": 0, "error": None,
	}
	interrupted = state["status"] == "running" and not _alive(state.get("pid"))
	report = _read(paths.state_dir / "health-last-result.json") if include_report or interrupted else {}
	if report:
		report = apply_duplicate_dismissals(report, load_duplicate_dismissals(paths))
	if interrupted:
		# Recover a crash between publishing the terminal report and terminal state.
		if state.get("reportCheckedAt") and report.get("checkedAt") == state["reportCheckedAt"]:
			state.update(status="failed" if report["status"] == "failed" else "complete", error=report.get("error"), phase="Finished")
		else:
			state.update(status="failed", error="Audit interrupted because its worker stopped. Run the audit again to retry.", phase="Interrupted")
		state.update(finishedAt=_now(), updatedAt=_now())
		_save(paths, state)
	return {**state, "running": state["status"] == "running", **({"lastReport": report or None} if include_report else {})}


def load_health_audit(paths: ManagedPaths, include_report: bool = True) -> dict[str, Any]:
	with _lock(paths):
		return _load(paths, include_report)


def dismiss_duplicate_alert(paths: ManagedPaths, issue_id: str) -> dict[str, Any]:
	with _lock(paths):
		current = _load(paths)
		report = current.get("lastReport") or {}
		issue = next((item for item in report.get("issues", []) if item.get("id") == issue_id and item.get("category") == "possible_recording_duplicate"), None)
		if issue is None:
			raise ValueError("Possible recording duplicate finding is no longer in the saved report.")
		ids = sorted(set(issue.get("persistentIds") or []))
		if len(ids) < 2 or not all(ids):
			raise ValueError("Duplicate finding has no valid persistent ID pair.")
		pairs = load_duplicate_dismissals(paths)
		pairs.update(duplicate_pair(left, right) for left, right in combinations(ids, 2))
		_save(paths, {"schemaVersion": 1, "pairs": [list(pair) for pair in sorted(pairs)]}, DUPLICATE_DISMISSALS)
		stored = _read(paths.state_dir / "health-last-result.json")
		if stored:
			save_health_report(paths, apply_duplicate_dismissals(stored, pairs))
		return _load(paths)


def start_health_audit(paths: ManagedPaths, deep_all: bool = False) -> dict[str, Any]:
	with _lock(paths):
		current = _load(paths)
		if current["running"]:
			return current
		state = {"runId": uuid.uuid4().hex, "status": "running", "mode": "deep" if deep_all else "normal",
			"pid": None, "startedAt": _now(), "updatedAt": _now(), "finishedAt": None,
			"phase": "Starting", "checked": 0, "total": 0, "error": None}
		_save(paths, state)
		try:
			process = subprocess.Popen(
				[sys.executable, "-u", "-m", "crate_music_importer.ipod_import.health_audit", state["runId"]],
				cwd=Path(__file__).resolve().parents[2], env={**os.environ, "CRATE_MANAGED_ROOT": str(paths.root)},
				stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
				start_new_session=True, close_fds=True,
			)
			state["pid"] = process.pid
		except Exception as exc:
			state.update(status="failed", phase="Start failed", error=str(exc), finishedAt=_now())
		_save(paths, state)
		return {**state, "running": state["status"] == "running", "lastReport": current["lastReport"]}


def _notify(failed: bool) -> None:
	try:
		subprocess.run(["/usr/bin/osascript", "-e", 'display notification "' +
			("Audit failed. Open Library Health & Updates to retry." if failed else "Audit complete. Open Library Health & Updates to review.") +
			'" with title "Crate Music Importer"'], capture_output=True, timeout=10, check=False)
	except (OSError, subprocess.TimeoutExpired):
		pass


def run_worker(paths: ManagedPaths, run_id: str) -> None:
	with _lock(paths):
		state = _read(paths.state_dir / "health-audit.json")
		if state.get("runId") != run_id or state.get("status") != "running":
			return
		# Reject accidental duplicate invocations of the same run.
		if state.get("pid") != os.getpid():
			return
	def progress(event: dict[str, Any]) -> None:
		state.update(phase="Finalizing" if event.get("phase") == "health_finalizing" else "Checking files",
			checked=event["checked"], total=event["total"], updatedAt=_now())
		with _lock(paths):
			_save(paths, state)
	try:
		with dependency_lock():
			state.update(phase="Reading Music", updatedAt=_now())
			with _lock(paths):
				_save(paths, state)
			manifest = load_manifest(paths)
			music_tracks = scan_music_library_for_health()
			state.update(phase="Refreshing preview index", updatedAt=_now())
			with _lock(paths):
				_save(paths, state)
			refresh_music_cache(paths, music_tracks)
			report = build_health_report(paths, manifest, music_tracks, on_progress=progress, deep_all=state["mode"] == "deep")
	except Exception as exc:
		report = failed_health_report(str(exc))
	report["checkedAt"] = _now()
	state.update(phase="Finalizing", updatedAt=_now(), reportCheckedAt=report["checkedAt"])
	with _lock(paths):
		_save(paths, state)
		try:
			report = apply_duplicate_dismissals(report, load_duplicate_dismissals(paths))
			save_health_report(paths, report)
		except Exception as exc:
			report = failed_health_report(f"Could not save audit report: {exc}")
		state.update(status="failed" if report["status"] == "failed" else "complete", phase="Finished",
			finishedAt=_now(), updatedAt=_now(), error=report.get("error"))
		_save(paths, state)
	_notify(state["status"] == "failed")


if __name__ == "__main__":
	run_worker(ManagedPaths(), sys.argv[1])
