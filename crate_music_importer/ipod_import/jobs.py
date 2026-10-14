"""Durable, single-runner background jobs for the native Raycast extension."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO
from urllib.parse import quote
from uuid import uuid4

from crate_music_importer.ipod_import import cli
from crate_music_importer.ipod_import.constants import MANAGED_ROOT
from crate_music_importer.ipod_import.manifest import ManagedPaths, file_sha256, load_manifest, save_manifest, update_manifest
from crate_music_importer.ipod_import.music import delete_managed_music_track, lookup_music_track, music_binding_id
from crate_music_importer.ipod_import.music_cache import remove_music_cache_tracks
from crate_music_importer.ipod_import.music_lock import music_operation_lock
from crate_music_importer.ipod_import.spotify import parse_source_url


ACTIVE_STATES = {"queued", "running", "cancelling"}
TERMINAL_STATES = {"complete", "needs_attention", "ready_to_continue", "failed", "cancelled", "superseded"}
RAYCAST_NOTIFICATION_COMMAND = "completion-notifications"


def _now() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary: Path | None = None
	try:
		with tempfile.NamedTemporaryFile(
			"w",
			encoding="utf-8",
			dir=path.parent,
			prefix=path.stem + "-",
			suffix=".tmp",
			delete=False,
		) as handle:
			temporary = Path(handle.name)
			json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
			handle.write("\n")
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary, path)
		temporary = None
	finally:
		if temporary is not None:
			try:
				temporary.unlink()
			except FileNotFoundError:
				pass


def _pid_running(pid: int) -> bool:
	if pid <= 0:
		return False
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	except PermissionError:
		return True
	return True


def _migrate_job(value: dict[str, Any]) -> dict[str, Any]:
	legacy = value.pop("cacheBaseline", None)
	if "stateBaseline" not in value and isinstance(legacy, dict):
		value["stateBaseline"] = legacy
	return value


def _safe_pid(value: Any) -> int:
	try:
		return int(value or 0)
	except (TypeError, ValueError, OverflowError):
		return 0


def _normalize_job_record(value: dict[str, Any]) -> dict[str, Any]:
	"""Keep malformed JSON objects visible and actionable without rewriting them."""
	job = _migrate_job(value)
	status = job.get("status")
	valid_status = isinstance(status, str)
	needs_source = valid_status and status in {"queued", "running", "cancelling", "needs_attention", "ready_to_continue", "failed"}
	source = job.get("source")
	valid_source = isinstance(source, dict) and isinstance(source.get("type"), str) and source.get("type") in {"album", "playlist"} and isinstance(source.get("id"), str) and bool(source.get("id"))
	malformed = not valid_status or (needs_source and not valid_source) or ("source" in job and source is not None and not valid_source)
	pid = job.get("pid")
	if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
		malformed = True
	if needs_source:
		for field, expected in (("notification", dict), ("counts", dict), ("tracks", list), ("trackProgress", dict)):
			if field in job and not isinstance(job.get(field), expected):
				malformed = True
		if job.get("currentTrack") is not None and not isinstance(job.get("currentTrack"), dict):
			malformed = True
	if malformed:
		job["source"] = source if isinstance(source, dict) and isinstance(source.get("type"), str) and source.get("type") in {"album", "playlist"} and isinstance(source.get("id"), str) else {}
		job["status"] = "failed"
		job["phase"] = "failed"
		job["pid"] = None
		job["retryable"] = False
		job["errorSummary"] = "Saved job record is malformed; the original record was preserved."
		job["malformedRecord"] = True
	return job


class JobStore:
	def __init__(self, root: Path = MANAGED_ROOT):
		self.root = Path(root)
		self.state_dir = self.root / ".state"
		self.jobs_dir = self.state_dir / "jobs"
		self.logs_dir = self.state_dir / "logs"
		self.runner_path = self.state_dir / "job-runner.json"
		self.enqueue_lock_path = self.state_dir / "job-enqueue.lock"
		self.transaction_lock_path = self.enqueue_lock_path
		self.worker_lock_path = self.state_dir / "import-mutation.lock"

	def job_path(self, job_id: str) -> Path:
		if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", job_id) or job_id in {".", ".."}:
			raise ValueError("Invalid import job ID.")
		return self.jobs_dir / f"{job_id}.json"

	def save(self, job: dict[str, Any]) -> dict[str, Any]:
		job["updatedAt"] = _now()
		_atomic_json(self.job_path(str(job["jobId"])), job)
		return job

	def update(self, job_id: str, mutate: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
		"""Read and update one job under the lifecycle lock."""
		with self.transaction_lock():
			job = self.load(job_id)
			if mutate(job) is False:
				return job
			return self.save(job)

	def load(self, job_id: str) -> dict[str, Any]:
		path = self.job_path(job_id)
		if not path.is_file():
			raise ValueError(f"Unknown import job: {job_id}")
		with path.open("r", encoding="utf-8") as handle:
			value = json.load(handle)
		if not isinstance(value, dict):
			raise ValueError(f"Invalid import job: {job_id}")
		return _migrate_job(value)

	def list(self) -> list[dict[str, Any]]:
		jobs: list[dict[str, Any]] = []
		if not self.jobs_dir.is_dir():
			return jobs
		for path in self.jobs_dir.glob("*.json"):
			try:
				with path.open("r", encoding="utf-8") as handle:
					value = json.load(handle)
			except (OSError, ValueError):
				jobs.append({
					"jobId": path.stem,
					"source": {},
					"status": "failed",
					"phase": "failed",
					"retryable": False,
					"errorSummary": "Saved job record is invalid JSON; the original record was preserved.",
					"malformedRecord": True,
				})
				continue
			if isinstance(value, dict) and value.get("jobId"):
				job_id = value.get("jobId")
				if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", job_id) or job_id in {".", ".."} or job_id != path.stem:
					jobs.append({
						"jobId": path.stem,
						"source": {},
						"status": "failed",
						"phase": "failed",
						"retryable": False,
						"errorSummary": "Saved job record has an invalid job ID; the original record was preserved.",
						"malformedRecord": True,
					})
				else:
					jobs.append(_normalize_job_record(value))
			else:
				jobs.append({
					"jobId": path.stem,
					"source": {},
					"status": "failed",
					"phase": "failed",
					"retryable": False,
					"errorSummary": "Saved job record has an invalid shape; the original record was preserved.",
					"malformedRecord": True,
				})
		return sorted(jobs, key=lambda job: str(job.get("createdAt") or ""), reverse=True)

	def runner(self) -> dict[str, Any] | None:
		if not self.runner_path.is_file():
			return None
		try:
			with self.runner_path.open("r", encoding="utf-8") as handle:
				value = json.load(handle)
		except (OSError, ValueError):
			return None
		return value if isinstance(value, dict) else None

	def release_runner(self, pid: int) -> None:
		current = self.runner() or {}
		locked_pid = _safe_pid(current.get("pid"))
		if locked_pid and locked_pid != pid:
			return
		try:
			self.runner_path.unlink()
		except FileNotFoundError:
			pass

	@contextlib.contextmanager
	def enqueue_lock(self):
		"""Compatibility name for the shared queue/job lifecycle transaction."""
		with self.transaction_lock():
			yield

	@contextlib.contextmanager
	def transaction_lock(self):
		self.state_dir.mkdir(parents=True, exist_ok=True)
		with self.transaction_lock_path.open("a+", encoding="utf-8") as handle:
			fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
			try:
				yield
			finally:
				fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

	@contextlib.contextmanager
	def worker_lock(self):
		self.state_dir.mkdir(parents=True, exist_ok=True)
		with self.worker_lock_path.open("a+", encoding="utf-8") as handle:
			try:
				fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
			except BlockingIOError:
				yield False
				return
			try:
				yield True
			finally:
				fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _new_job(
	action: str,
	url: str,
	seed: dict[str, Any] | None = None,
	*,
	state_baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
	source_type, source_id, canonical = parse_source_url(url)
	expected_action = f"{source_type}_combined"
	allowed_actions = {expected_action}
	if source_type == "playlist":
		allowed_actions.add("playlist_update_combined")
	if action not in allowed_actions:
		raise ValueError(f"The {action} action does not accept a Spotify {source_type} URL.")
	now = _now()
	seed = seed if isinstance(seed, dict) else {}
	seed_tracks = seed.get("tracks") if isinstance(seed.get("tracks"), list) else []
	tracks = [
		{
			"position": int(track.get("position") or position),
			"trackNumber": int(track.get("trackNumber") or track.get("position") or position),
			"discNumber": int(track.get("discNumber") or 1),
			"recordingId": str(track.get("recordingId") or ""),
			"title": str(track.get("title") or ""),
			"artists": str(track.get("artists") or ""),
			"state": "not_started",
			"detail": "",
		}
		for position, track in enumerate(seed_tracks, start=1)
		if isinstance(track, dict)
	]
	total = len(tracks) or int(seed.get("total") or 0)
	return {
		"version": 1,
		"jobId": uuid4().hex,
		"source": {
			"type": source_type,
			"id": str(seed.get("savedPlaylistId") or source_id),
			"url": canonical,
			"name": str(seed.get("name") or ("Spotify Album" if source_type == "album" else "Spotify Playlist")),
			"total": total,
		},
		"action": "playlist_update_combined" if action == "playlist_update_combined" else expected_action,
		"mode": "update" if action == "playlist_update_combined" else "import",
		"status": "queued",
		"phase": "queued",
		"pid": None,
		"createdAt": now,
		"updatedAt": now,
		"startedAt": None,
		"finishedAt": None,
		"currentTrack": None,
		"counts": {
			"total": total,
			"notStarted": total,
			"searching": 0,
			"matched": 0,
			"noMatches": 0,
			"downloading": 0,
			"ready": 0,
			"downloaded": 0,
			"reused": 0,
			"approval": 0,
			"adding": 0,
			"checking": 0,
			"complete": 0,
			"review": 0,
			"failed": 0,
			"pending": total,
		},
		"tracks": tracks,
		"errorSummary": None,
		"retryable": False,
		"logPath": None,
		"notification": {"pending": False, "notifiedAt": None},
		"stateBaseline": state_baseline or {"captured": False, "persistentIds": []},
		"confirmationToken": str(seed.get("confirmationToken") or "") or None,
	}


def _capture_state_baseline(root: Path) -> dict[str, Any]:
	try:
		manifest = load_manifest(ManagedPaths(root))
	except (OSError, ValueError):
		return {"captured": False, "persistentIds": []}
	return {
		"captured": True,
		"persistentIds": sorted({music_binding_id(recording) for recording in manifest.get("recordings", {}).values() if music_binding_id(recording)}),
	}


def _runner_command() -> list[str]:
	return [sys.executable, "-u", "-m", "crate_music_importer.ipod_import.raycast", "__runner"]


def _ensure_runner(
	store: JobStore,
	*,
	popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> int | None:
	current = store.runner() or {}
	current_pid = _safe_pid(current.get("pid"))
	if _pid_running(current_pid):
		return current_pid
	starter_pid = _safe_pid(current.get("starterPid"))
	dead_new_starter = current.get("status") == "starting" and starter_pid > 0 and not _pid_running(starter_pid)
	try:
		runner_age = time.time() - store.runner_path.stat().st_mtime
	except FileNotFoundError:
		runner_age = None
	if not dead_new_starter and runner_age is not None and runner_age < 30 and (not current or current.get("status") == "starting"):
		# Another enqueue owns the atomic startup lock but has not published its PID yet.
		return None
	try:
		store.runner_path.unlink()
	except FileNotFoundError:
		pass
	store.state_dir.mkdir(parents=True, exist_ok=True)
	try:
		descriptor = os.open(store.runner_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
	except FileExistsError:
		return None
	with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
		startup_token = uuid4().hex
		json.dump({"pid": None, "status": "starting", "startedAt": _now(), "startupToken": startup_token, "starterPid": os.getpid()}, handle)
		handle.write("\n")
	try:
		process = popen(
			_runner_command(),
			cwd=Path(__file__).resolve().parents[2],
			env={**os.environ, "CRATE_MANAGED_ROOT": str(store.root)},
			stdin=subprocess.DEVNULL,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			start_new_session=True,
			close_fds=True,
		)
		current = store.runner() or {}
		if current.get("startupToken") == startup_token:
			_atomic_json(store.runner_path, {"pid": process.pid, "status": "running", "startedAt": current.get("startedAt") or _now()})
		return int(process.pid)
	except Exception:
		store.release_runner(0)
		raise


def enqueue(
	action: str,
	url: str,
	*,
	root: Path = MANAGED_ROOT,
	popen: Callable[..., subprocess.Popen] = subprocess.Popen,
	retry_of: str | None = None,
	seed: dict[str, Any] | None = None,
	state_baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
	store = JobStore(root)
	job = _new_job(action, url, seed, state_baseline=state_baseline or _capture_state_baseline(root))
	if retry_of:
		job["retryOf"] = retry_of
	with store.enqueue_lock():
		original = store.load(retry_of) if retry_of else None
		if original is not None and (original.get("status") != "failed" or not original.get("retryable")):
			raise ValueError("Only a retryable failed import can be queued again.")
		if action == "playlist_update_combined":
			manifest = load_manifest(ManagedPaths(root))
			pending = ((manifest.get("playlists", {}).get(job["source"]["id"]) or {}).get("pending_update") or {})
			token = str(pending.get("confirmation_token") or "")
			if token and (not job.get("confirmationToken") or job["confirmationToken"] == token):
				job["confirmationToken"] = token
				job["resumeUpdate"] = True
		existing_jobs = store.list()
		for existing in existing_jobs:
			existing_source = existing.get("source") if isinstance(existing.get("source"), dict) else {}
			if (
				existing.get("status") in ACTIVE_STATES
				and existing_source.get("type") == job["source"]["type"]
				and existing_source.get("id") == job["source"]["id"]
			):
				raise ValueError("This Spotify source is already queued or running.")
		job["queueSequence"] = max((int(existing.get("queueSequence") or 0) for existing in existing_jobs), default=0) + 1
		for existing in existing_jobs:
			source = existing.get("source") or {}
			if (
				existing.get("status") in {"needs_attention", "ready_to_continue", "failed"}
				and source.get("type") == job["source"]["type"]
				and source.get("id") == job["source"]["id"]
			):
				existing["status"] = "superseded"
				existing["phase"] = "superseded"
				existing["supersededBy"] = job["jobId"]
				existing["retryable"] = False
				existing["notification"] = {"pending": False, "notifiedAt": (existing.get("notification") or {}).get("notifiedAt")}
				store.save(existing)
		store.save(job)
		try:
			runner_pid = _ensure_runner(store, popen=popen)
			latest = store.load(str(job["jobId"]))
			latest["runnerPid"] = runner_pid
			job.clear()
			job.update(store.save(latest))
		except Exception:
			latest = store.load(str(job["jobId"]))
			latest["status"] = "failed"
			latest["phase"] = "failed"
			latest["finishedAt"] = _now()
			latest["errorSummary"] = "Could not start the import runner."
			latest["retryable"] = True
			latest["notification"] = {"pending": True, "notifiedAt": None}
			job.clear()
			job.update(store.save(latest))
			raise
		if original is not None:
			original["status"] = "superseded"
			original["phase"] = "superseded"
			original["retryable"] = False
			original["supersededBy"] = job["jobId"]
			store.save(original)
	return job


def _delete_managed_file(root: Path, relative_path: str) -> bool:
	if not relative_path:
		return False
	resolved_root = root.resolve()
	target = (root / relative_path).resolve()
	try:
		target.relative_to(resolved_root)
	except ValueError:
		return False
	if not target.is_file():
		return False
	target.unlink()
	return True


def _record_cancellation_cleanup(
	job: dict[str, Any],
	store: JobStore,
	key: str,
	*,
	initial: dict[str, Any] | None = None,
	step: str | None = None,
) -> dict[str, Any]:
	"""Durably authorize and checkpoint one cancellation cleanup step."""
	job_id = str(job["jobId"])
	def update(current: dict[str, Any]) -> bool:
		if current.get("status") not in {"cancelling", "failed"}:
			raise ValueError("Cancellation cleanup lost its durable job reservation.")
		cleanup = current.setdefault("cancellationCleanup", {})
		checkpoint = cleanup.setdefault(key, {})
		if initial:
			for field, value in initial.items():
				if field in checkpoint and checkpoint[field] != value:
					raise ValueError("Cancellation cleanup provenance changed while resuming.")
				checkpoint.setdefault(field, value)
		if step:
			checkpoint.setdefault(step, _now() if step.endswith("At") else True)
		return True
	updated = store.update(job_id, update)
	job["cancellationCleanup"] = updated.get("cancellationCleanup") or {}
	return job["cancellationCleanup"].get(key) or {}


def _delete_source_progress(
	job: dict[str, Any],
	store: JobStore,
	*,
	delete_music: Callable[[str, Path], bool] = delete_managed_music_track,
	exact_lookup: Callable[[str], dict[str, Any] | None] = lookup_music_track,
) -> dict[str, int]:
	paths = ManagedPaths(store.root)
	source = job.get("source") or {}
	source_type = str(source.get("type") or "")
	source_id = str(source.get("id") or "")
	if source_type not in {"album", "playlist"} or not source_id:
		raise ValueError("Cancellation cleanup has no valid saved source identity.")
	collection_name = "albums" if source_type == "album" else "playlists"
	manifest = load_manifest(paths)
	collection = manifest.get(collection_name) or {}
	source_value = collection.get(source_id)
	if not isinstance(source_value, dict):
		return {"recordings": 0, "files": 0, "music_tracks": 0, "cache_entries": 0}
	baseline = job.get("stateBaseline") or {}
	baseline_captured = baseline.get("captured") is True
	protected_ids = {str(value) for value in baseline.get("persistentIds") or [] if str(value)}
	deleted_files = 0
	deleted_recordings = 0
	deleted_music_tracks = 0
	removed_cache_entries = 0
	remove_recording_ids: set[str] = set()
	cleanup_map = job.get("cancellationCleanup") if isinstance(job.get("cancellationCleanup"), dict) else {}
	if source_type == "playlist" and source_value.get("m3u8_managed_by_crate"):
		relative_path = str(source_value.get("m3u8_relative_path") or "")
		if not relative_path:
			raise ValueError("The saved playlist file has no managed path.")
		checkpoint = _record_cancellation_cleanup(job, store, "__source_playlist_file__", initial={
			"sourceType": source_type, "sourceId": source_id, "managedByCrate": True, "relativePath": relative_path,
		})
		playlist_path = (store.root / relative_path).resolve()
		try:
			playlist_path.relative_to(store.root.resolve())
		except ValueError as exc:
			raise ValueError("The saved playlist file is outside the managed directory.") from exc
		if playlist_path.exists():
			_record_cancellation_cleanup(job, store, "__source_playlist_file__", step="fileDeleteStartedAt")
			deleted_files += int(_delete_managed_file(store.root, relative_path))
			_record_cancellation_cleanup(job, store, "__source_playlist_file__", step="fileDeleted")
		elif not checkpoint.get("fileDeleteStartedAt") and not checkpoint.get("fileDeleted"):
			raise ValueError("The saved playlist file is missing without an authorized deletion checkpoint.")
		else:
			_record_cancellation_cleanup(job, store, "__source_playlist_file__", step="fileDeleted")
	affected_ids = {str(item.get("recording_id") or "") for item in source_value.get("items") or [] if isinstance(item, dict)}
	pending_ids = list(affected_ids)
	processed_ids: set[str] = set()
	while pending_ids:
		recording_id = pending_ids.pop()
		if recording_id in processed_ids:
			continue
		processed_ids.add(recording_id)
		# Refresh immediately before each external deletion. A newer source reference
		# protects the shared recording even when this cancellation began from an older snapshot.
		manifest = load_manifest(paths)
		current_source = (manifest.get(collection_name) or {}).get(source_id)
		if not isinstance(current_source, dict):
			continue
		current_ids = {
			str(item.get("recording_id") or "")
			for item in current_source.get("items") or []
			if isinstance(item, dict) and item.get("recording_id")
		}
		for current_id in current_ids - affected_ids:
			affected_ids.add(current_id)
			pending_ids.append(current_id)
		if recording_id not in current_ids:
			continue
		recording = manifest.get("recordings", {}).get(recording_id)
		if not isinstance(recording, dict):
			checkpoint = cleanup_map.get(recording_id) if isinstance(cleanup_map.get(recording_id), dict) else {}
			if not checkpoint.get("recordingRemoved"):
				raise ValueError("A missing recording has no durable cancellation cleanup proof.")
			continue
		staging_root = paths.staging.resolve()
		staging_target = (paths.staging / recording_id).resolve()
		try:
			staging_target.relative_to(staging_root)
		except ValueError as exc:
			raise ValueError("The saved staging path is outside the managed staging directory.") from exc
		if staging_target == staging_root:
			raise ValueError("The saved staging path does not identify a recording directory.")
		source_membership_key = f"{source_type}_memberships"
		other_references = any(
			(key, membership_id) != (source_membership_key, source_id)
			for key in ("album_memberships", "playlist_memberships")
			for membership_id, values in ((recording.get(key) if isinstance(recording.get(key), dict) else {}).items())
			if values
		)
		if other_references:
			continue
		persistent_id = music_binding_id(recording)
		if persistent_id in protected_ids:
			continue
		managed = recording.get("managed_file") if isinstance(recording.get("managed_file"), dict) else {}
		relative_path = str(managed.get("relative_path") or "")
		managed_path = (store.root / relative_path).resolve() if relative_path else None
		if managed_path is not None:
			try:
				managed_path.relative_to(store.root.resolve())
			except ValueError as exc:
				raise ValueError("The saved managed file is outside the managed directory.") from exc
		checkpoint = cleanup_map.get(recording_id) if isinstance(cleanup_map.get(recording_id), dict) else {}
		initial = {
			"sourceType": source_type,
			"sourceId": source_id,
			"persistentId": persistent_id,
			"relativePath": relative_path,
			"sha256": str(managed.get("sha256") or ""),
			"managedByCrate": bool(managed.get("managed_by_crate")),
			"baselineCaptured": baseline_captured,
		}
		checkpoint = _record_cancellation_cleanup(job, store, recording_id, initial=initial)
		cleanup_map = job.get("cancellationCleanup") or {}
		if (
			checkpoint.get("relativePath") != relative_path
			or checkpoint.get("sha256") != str(managed.get("sha256") or "")
			or checkpoint.get("managedByCrate") != bool(managed.get("managed_by_crate"))
		):
			raise ValueError("Cancellation cleanup provenance changed while resuming.")
		managed_proven = bool(
			managed_path is not None
			and managed.get("managed_by_crate")
			and managed.get("sha256")
		)
		if managed_proven and managed_path.is_file():
			if file_sha256(managed_path) != str(managed.get("sha256") or ""):
				raise ValueError("The managed file no longer matches its saved hash; cancellation stopped for review.")
		elif managed_path is not None and not managed_path.exists() and not (checkpoint.get("fileDeleteStartedAt") or checkpoint.get("fileDeleted")):
			# Do not mistake an unrelated missing file for this cancellation's deletion.
			raise ValueError("The managed file is missing without an authorized cancellation deletion checkpoint.")
		elif managed_path is not None and not managed_proven:
			continue

		if persistent_id and baseline_captured and managed_proven:
			track = exact_lookup(persistent_id)
			track_path = Path(str((track or {}).get("location") or "")).resolve() if (track or {}).get("location") else None
			if checkpoint.get("musicDeleteStartedAt"):
				if track is not None and track_path != managed_path:
					raise ValueError("The exact Music item moved after cancellation deletion began; cancellation stopped for review.")
				if track is not None and checkpoint.get("musicDeleted"):
					raise ValueError("The exact Music item reappeared after cancellation deletion was checkpointed.")
				if track is not None:
					deleted_music_tracks += int(delete_music(persistent_id, managed_path))
					if exact_lookup(persistent_id) is not None:
						raise ValueError("Music did not confirm deletion of the exact managed track.")
				elif not checkpoint.get("musicDeleted"):
					deleted_music_tracks += 1
				_record_cancellation_cleanup(job, store, recording_id, step="musicDeleted")
				checkpoint = (job.get("cancellationCleanup") or {}).get(recording_id) or {}
				if persistent_id and not checkpoint.get("cacheRemoved"):
					removed_cache_entries += remove_music_cache_tracks(paths, {persistent_id})
					_record_cancellation_cleanup(job, store, recording_id, step="cacheRemoved")
			else:
				if track is not None and track_path == managed_path:
					_record_cancellation_cleanup(job, store, recording_id, step="musicDeleteStartedAt")
					deleted_music_tracks += int(delete_music(persistent_id, managed_path))
					if exact_lookup(persistent_id) is not None:
						raise ValueError("Music did not confirm deletion of the exact managed track.")
					_record_cancellation_cleanup(job, store, recording_id, step="musicDeleted")
					if not ((job.get("cancellationCleanup") or {}).get(recording_id) or {}).get("cacheRemoved"):
						removed_cache_entries += remove_music_cache_tracks(paths, {persistent_id})
						_record_cancellation_cleanup(job, store, recording_id, step="cacheRemoved")
		if managed_path is not None and managed_proven:
			checkpoint = (job.get("cancellationCleanup") or {}).get(recording_id) or {}
			if managed_path.exists():
				if file_sha256(managed_path) != str(managed.get("sha256") or ""):
					raise ValueError("The managed file no longer matches its saved hash; cancellation stopped for review.")
				if not checkpoint.get("fileDeleteStartedAt"):
					_record_cancellation_cleanup(job, store, recording_id, step="fileDeleteStartedAt")
				deleted_files += int(_delete_managed_file(store.root, relative_path))
				_record_cancellation_cleanup(job, store, recording_id, step="fileDeleted")
			elif checkpoint.get("fileDeleteStartedAt") or checkpoint.get("fileDeleted"):
				if checkpoint.get("fileDeleteStartedAt") and not checkpoint.get("fileDeleted"):
					deleted_files += 1
				_record_cancellation_cleanup(job, store, recording_id, step="fileDeleted")
		if managed_path is not None and not (job.get("cancellationCleanup", {}).get(recording_id) or {}).get("fileDeleted"):
			continue
		if staging_target.is_dir():
			shutil.rmtree(staging_target)
		remove_recording_ids.add(recording_id)
		_record_cancellation_cleanup(job, store, recording_id, step="recordingRemoved")
		deleted_recordings += 1
	def finalize(manifest: dict[str, Any]) -> None:
		collection = manifest.get(collection_name) or {}
		collection.pop(source_id, None)
		for recording_id in affected_ids:
			recording = manifest.get("recordings", {}).get(recording_id)
			if not isinstance(recording, dict):
				continue
			(recording.get(f"{source_type}_memberships") or {}).pop(source_id, None)
			if recording_id not in remove_recording_ids:
				continue
			if recording.get("album_memberships") or recording.get("playlist_memberships"):
				continue
			if music_binding_id(recording) in protected_ids:
				continue
			manifest.get("manual_youtube_overrides", {}).pop(recording_id, None)
			manifest["recordings"].pop(recording_id, None)
	update_manifest(paths, finalize)
	return {
		"recordings": deleted_recordings,
		"files": deleted_files,
		"music_tracks": deleted_music_tracks,
		"cache_entries": removed_cache_entries,
	}


def _terminate_runner(pid: int) -> None:
	os.killpg(pid, signal.SIGTERM)


def cancel_incomplete(
	job_id: str,
	*,
	root: Path = MANAGED_ROOT,
	terminate: Callable[[int], None] = _terminate_runner,
	popen: Callable[..., subprocess.Popen] = subprocess.Popen,
	delete_music: Callable[[str, Path], bool] = delete_managed_music_track,
	exact_lookup: Callable[[str], dict[str, Any] | None] = lookup_music_track,
) -> dict[str, Any]:
	store = JobStore(root)
	with store.enqueue_lock():
		job = store.load(job_id)
		if job.get("status") == "complete":
			raise ValueError("A completed import cannot be cancelled or have its progress removed.")
		if job.get("status") in {"cancelled", "cancelling", "superseded"}:
			raise ValueError("This import is no longer active or resumable.")
		pid = _safe_pid(job.get("pid")) if job.get("status") == "running" else 0
		runner_pid = _safe_pid((store.runner() or {}).get("pid"))
		if pid and runner_pid and pid != runner_pid:
			raise ValueError("The running import no longer matches the active importer process.")
		if not pid:
			job["status"] = "cancelling"
			job["phase"] = "cancelling"
			job["cancellationOwnerPid"] = os.getpid()
			job["notification"] = {"pending": False, "notifiedAt": (job.get("notification") or {}).get("notifiedAt")}
			store.save(job)

	if pid and _pid_running(pid):
		try:
			terminate(pid)
		except ProcessLookupError:
			pass
		for _ in range(40):
			if not _pid_running(pid):
				break
			time.sleep(0.05)
		if _pid_running(pid):
			raise ValueError("The running import did not stop; its saved progress was kept.")

	if pid:
		with store.enqueue_lock():
			job = store.load(job_id)
			if job.get("status") == "complete":
				raise ValueError("A completed import cannot be cancelled or have its progress removed.")
			if job.get("status") in {"cancelled", "superseded"}:
				raise ValueError("This import is no longer active or resumable.")
			job["status"] = "cancelling"
			job["phase"] = "cancelling"
			job["cancellationOwnerPid"] = os.getpid()
			job["pid"] = None
			job["notification"] = {"pending": False, "notifiedAt": (job.get("notification") or {}).get("notifiedAt")}
			store.save(job)
			store.release_runner(pid)

	try:
		with music_operation_lock():
			removed = (
				{"recordings": 0, "files": 0, "music_tracks": 0, "cache_entries": 0}
				if job.get("action") == "playlist_update_combined"
				else _delete_source_progress(job, store, delete_music=delete_music, exact_lookup=exact_lookup)
			)
		removed_job_ids: list[str] = []
		with store.enqueue_lock():
			job["status"] = "cancelled"
			job["phase"] = "cancelled"
			job.pop("cancellationOwnerPid", None)
			job["finishedAt"] = _now()
			for existing in store.list():
				if existing.get("jobId") == job_id or existing.get("status") == "complete" or not _same_source(existing, job):
					continue
				log_path = Path(str(existing.get("logPath") or ""))
				if log_path.name and log_path.parent.resolve() == store.logs_dir.resolve() and log_path.is_file():
					log_path.unlink()
				try:
					store.job_path(str(existing["jobId"])).unlink()
				except FileNotFoundError:
					pass
				removed_job_ids.append(str(existing["jobId"]))
			if any(existing.get("status") == "queued" for existing in store.list()):
				_ensure_runner(store, popen=popen)
			try:
				store.job_path(job_id).unlink()
			except FileNotFoundError:
				pass
	except Exception as exc:
		with store.enqueue_lock():
			current = store.load(job_id)
			current["status"] = "failed"
			current["phase"] = "failed"
			current["finishedAt"] = _now()
			current["errorSummary"] = f"Cancellation cleanup failed: {str(exc)[:400]}"
			current["retryable"] = True
			store.save(current)
		raise
	return {
		**job,
		"removedJobIds": removed_job_ids,
		"removedRecordings": removed["recordings"],
		"removedFiles": removed["files"],
		"removedMusicTracks": removed["music_tracks"],
		"removedCacheEntries": removed["cache_entries"],
	}


def cancel_queued(job_id: str, *, root: Path = MANAGED_ROOT) -> dict[str, Any]:
	"""Backward-compatible alias for callers that previously removed only queued jobs."""
	return cancel_incomplete(job_id, root=root)


def cancel_source_progress(
	source_type: str,
	source_id: str,
	*,
	root: Path = MANAGED_ROOT,
	delete_music: Callable[[str, Path], bool] = delete_managed_music_track,
	exact_lookup: Callable[[str], dict[str, Any] | None] = lookup_music_track,
) -> dict[str, Any]:
	"""Remove an incomplete saved source even when its legacy job was marked complete."""
	if source_type not in {"album", "playlist"}:
		raise ValueError("The source type must be album or playlist.")
	store = JobStore(root)
	reservation_id = uuid4().hex
	with music_operation_lock():
		paths = ManagedPaths(root)
		manifest = load_manifest(paths)
		collection = manifest.get("albums", {}) if source_type == "album" else manifest.get("playlists", {})
		source_value = collection.get(source_id)
		if not isinstance(source_value, dict):
			raise ValueError("This saved Spotify source no longer exists.")
		items = source_value.get("items") or []
		def item_fully_imported(item: Any) -> bool:
			if not isinstance(item, dict):
				return False
			recording = manifest.get("recordings", {}).get(item.get("recording_id"))
			if not isinstance(recording, dict):
				return False
			return bool(music_binding_id(recording))
		if bool(items) and all(item_fully_imported(item) for item in items):
			raise ValueError("A completed import cannot be cancelled or have its progress removed.")
		with store.enqueue_lock():
			matching_jobs = [
				job for job in store.list()
				if isinstance(job.get("source"), dict)
				and job["source"].get("type") == source_type
				and job["source"].get("id") == source_id
			]
			if any(job.get("status") in ACTIVE_STATES for job in matching_jobs):
				raise ValueError("This import became active. Cancel it from the Active & Queued section instead.")
			baseline_values = [job.get("stateBaseline") or {} for job in matching_jobs]
			cleanup_values = [job.get("cancellationCleanup") for job in matching_jobs if isinstance(job.get("cancellationCleanup"), dict)]
			job_like = {
				"jobId": reservation_id,
				"status": "cancelling",
				"cancellationOwnerPid": os.getpid(),
				"createdAt": _now(),
				"source": {
					"type": source_type,
					"id": source_id,
					"name": str(source_value.get("name") or "Spotify source"),
					"url": str(source_value.get("spotify_url") or ""),
				},
				"stateBaseline": {
					"captured": any(value.get("captured") is True for value in baseline_values),
					"persistentIds": sorted({
						str(persistent_id)
						for value in baseline_values
						for persistent_id in value.get("persistentIds") or []
						if str(persistent_id)
					}),
				},
				"cancellationCleanup": cleanup_values[-1] if cleanup_values else {},
			}
			store.save(job_like)
		try:
			removed = _delete_source_progress(job_like, store, delete_music=delete_music, exact_lookup=exact_lookup)
			removed_job_ids: list[str] = []
			with store.enqueue_lock():
				for job in matching_jobs:
					if job.get("status") == "complete":
						continue
					try:
						store.job_path(str(job["jobId"])).unlink()
					except FileNotFoundError:
						pass
					removed_job_ids.append(str(job["jobId"]))
				try:
					store.job_path(reservation_id).unlink()
				except FileNotFoundError:
					pass
		except Exception as exc:
			with store.enqueue_lock():
				current = store.load(reservation_id)
				current["status"] = "failed"
				current["phase"] = "failed"
				current["cancellationOwnerPid"] = None
				current["finishedAt"] = _now()
				current["errorSummary"] = f"Cancellation cleanup failed: {str(exc)[:400]}"
				current["retryable"] = False
				current["notification"] = {"pending": True, "notifiedAt": None}
				store.save(current)
			raise
	return {
		"sourceType": source_type,
		"sourceId": source_id,
		"removedJobIds": removed_job_ids,
		"removedRecordings": removed["recordings"],
		"removedFiles": removed["files"],
		"removedMusicTracks": removed["music_tracks"],
		"removedCacheEntries": removed["cache_entries"],
	}


def retry_job(
	job_id: str,
	*,
	root: Path = MANAGED_ROOT,
	popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> dict[str, Any]:
	store = JobStore(root)
	original = store.load(job_id)
	if original.get("status") != "failed" or not original.get("retryable"):
		raise ValueError("Only a retryable failed import can be queued again.")
	retried = enqueue(
		str(original["action"]),
		str(original["source"]["url"]),
		root=root,
		popen=popen,
		retry_of=str(original["jobId"]),
		seed={
			"savedPlaylistId": original.get("source", {}).get("id"),
			"name": original.get("source", {}).get("name"),
			"total": original.get("source", {}).get("total"),
			"tracks": original.get("tracks") or [],
			"confirmationToken": original.get("confirmationToken"),
		},
		state_baseline=original.get("stateBaseline") or {"captured": False, "persistentIds": []},
	)
	return retried


def acknowledge_notification(job_id: str, *, root: Path = MANAGED_ROOT) -> dict[str, Any]:
	store = JobStore(root)
	def acknowledge(job: dict[str, Any]) -> None:
		notification = job.setdefault("notification", {})
		notification["pending"] = False
		notification["notifiedAt"] = _now()
	return store.update(job_id, acknowledge)


def _source_from_manifest(job: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any] | None:
	source = job.get("source")
	if (
		not isinstance(source, dict)
		or not isinstance(source.get("type"), str)
		or source.get("type") not in {"album", "playlist"}
		or not isinstance(source.get("id"), str)
		or not source.get("id")
	):
		return None
	collection = manifest.get("albums", {}) if source["type"] == "album" else manifest.get("playlists", {})
	value = collection.get(source["id"])
	return value if isinstance(value, dict) else None


def _track_state(recording: dict[str, Any], *, current_id: str | None, current_state: str) -> str:
	if current_id == recording.get("recording_id") and current_state in {
		"searching_youtube",
		"youtube_match_found",
		"matches_need_approval",
		"no_youtube_matches",
		"downloading",
		"tagging",
		"checking_music_ids",
		"adding_to_music",
	}:
		return current_state
	review = recording.get("review") or {}
	if review:
		if review.get("kind") in ("youtube_missing", "youtube_ambiguity"):
			return "matches_need_approval" if review.get("candidates") else "no_youtube_matches"
		return "needs_approval"
	if recording.get("last_error"):
		return "failed"
	if recording.get("music_import_pending"):
		return "checking_music_ids"
	if music_binding_id(recording):
		return "reused"
	if (recording.get("managed_file") or {}).get("relative_path"):
		return "downloaded"
	if (recording.get("youtube") or {}).get("url"):
		return "youtube_match_found"
	return "not_started"


def _persist_progress(job: dict[str, Any], store: JobStore) -> None:
	job_id = str(job.get("jobId") or "")
	if not job_id:
		return
	def merge(current: dict[str, Any]) -> bool:
		if current.get("status") in {"cancelled", "cancelling", "superseded", "complete", "failed", "needs_attention"}:
			return False
		changed = False
		if current.get("status") == "queued":
			current["status"] = "running"
			current["pid"] = int(job.get("pid") or os.getpid())
			current["startedAt"] = current.get("startedAt") or _now()
			changed = True
		if current.get("phase") != job.get("phase"):
			current["phase"] = job.get("phase")
			changed = True
		if current.get("currentTrack") != job.get("currentTrack"):
			current["currentTrack"] = job.get("currentTrack")
			changed = True
		if job.get("trackProgress") is not None and current.get("trackProgress") != job.get("trackProgress"):
			current["trackProgress"] = job.get("trackProgress")
			changed = True
		source = current.setdefault("source", {})
		for field in ("name", "total"):
			value = (job.get("source") or {}).get(field)
			if source.get(field) != value:
				source[field] = value
				changed = True
		return changed
	updated = store.update(job_id, merge)
	for key in ("phase", "currentTrack", "trackProgress", "source"):
		if key in updated:
			job[key] = updated[key]


def sync_from_manifest(
	job: dict[str, Any],
	store: JobStore,
	manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
	try:
		if manifest is None:
			manifest = load_manifest(ManagedPaths(store.root))
	except (OSError, ValueError):
		return job
	source_value = _source_from_manifest(job, manifest)
	if not source_value:
		return job
	job["source"]["name"] = str(source_value.get("name") or job["source"]["name"])
	if job.get("action") == "playlist_update_combined":
		pending = source_value.get("pending_update")
		if not isinstance(pending, dict):
			if job.get("status") == "running":
				tracks = [dict(track, state="complete") for track in job.get("tracks") or []]
				count = len(tracks)
				counts = {key: 0 for key in job.get("counts") or {}}
				counts.update({"total": count, "complete": count, "ready": count})
				def complete_pending(current: dict[str, Any]) -> bool:
					if current.get("status") != "running":
						return False
					if current.get("tracks") == tracks and current.get("counts") == counts:
						return False
					current["tracks"] = tracks
					current["counts"] = counts
					return True
				updated = store.update(str(job["jobId"]), complete_pending)
				for key in ("tracks", "counts"):
					if key in updated:
						job[key] = updated[key]
				return job
			return job
		items = list(pending.get("addition_items") or []) + [
			{
				"position": int(removal.get("saved_position") or 0),
				"recording_id": str(removal.get("recording_id") or ""),
				"status": "removal",
			}
			for removal in pending.get("removals") or []
		] + [
			{
				"position": int(reorder.get("to_position") or 0),
				"recording_id": str(reorder.get("recording_id") or ""),
				"status": "reorder",
				"title": str(reorder.get("title") or ""),
				"artists": str(reorder.get("artists") or ""),
			}
			for reorder in pending.get("reorders") or []
		] + [
			{
				"position": int(change.get("to_position") or change.get("from_position") or 0),
				"recording_id": str(change.get("recording_id") or ""),
				"status": "music_change",
				"title": str(change.get("title") or ""),
				"artists": str(change.get("artists") or ""),
			}
			for change in pending.get("music_changes") or []
		]
	else:
		items = sorted(source_value.get("items") or [], key=lambda item: int(item.get("position") or 0))
	job["source"]["total"] = len(items)
	current_id = str((job.get("currentTrack") or {}).get("recordingId") or "") if job.get("status") == "running" else None
	current_state = str((job.get("currentTrack") or {}).get("state") or "")
	tracks: list[dict[str, Any]] = []
	counts = {
		"total": len(items),
		"notStarted": 0,
		"searching": 0,
		"matched": 0,
		"noMatches": 0,
		"downloading": 0,
		"ready": 0,
		"downloaded": 0,
		"reused": 0,
		"approval": 0,
		"adding": 0,
		"checking": 0,
		"complete": 0,
		"review": 0,
		"failed": 0,
		"pending": 0,
	}
	for item in items:
		recording = manifest.get("recordings", {}).get(item.get("recording_id"), {})
		metadata = recording.get("source_metadata") or {}
		if job.get("action") == "playlist_update_combined" and item.get("status") in {"removal", "reorder", "music_change"}:
			checkpoint = (source_value.get("pending_update") or {}).get("music_checkpoint") or {}
			state = "complete" if checkpoint.get("applied_at") else "not_started"
		else:
			state = _track_state(recording, current_id=current_id, current_state=current_state)
			completed = (job.get("trackProgress") or {}).get(str(item.get("recording_id") or "")) == "complete" or job.get("status") == "complete"
			if completed and state == "reused":
				state = "complete"
		if state == "downloaded":
			counts["downloaded"] += 1
		if state == "reused":
			counts["reused"] += 1
		if state in {"downloaded", "complete", "reused"}:
			counts["ready"] += 1
		if state == "complete":
			counts["complete"] += 1
		if state == "not_started":
			counts["notStarted"] += 1
		elif state == "searching_youtube":
			counts["searching"] += 1
		elif state == "youtube_match_found":
			counts["matched"] += 1
		elif state == "no_youtube_matches":
			counts["noMatches"] += 1
			counts["approval"] += 1
			counts["review"] += 1
		elif state in {"matches_need_approval", "needs_approval"}:
			counts["approval"] += 1
			counts["review"] += 1
		elif state == "downloading":
			counts["downloading"] += 1
		elif state == "adding_to_music":
			counts["adding"] += 1
		elif state == "checking_music_ids":
			counts["checking"] += 1
		elif state == "failed":
			counts["failed"] += 1
		if state in {
			"not_started",
			"searching_youtube",
			"youtube_match_found",
			"downloading",
			"tagging",
			"checking_music_ids",
			"adding_to_music",
		}:
			counts["pending"] += 1
		tracks.append({
			"position": int(item.get("position") or 0),
			"trackNumber": int(item.get("track_no") or item.get("position") or 0),
			"discNumber": int(item.get("disc_no") or 1),
			"recordingId": str(item.get("recording_id") or ""),
			"title": str(metadata.get("title") or item.get("title") or ""),
			"artists": str(metadata.get("artists") or item.get("artists") or ""),
			"state": state,
			"detail": str(recording.get("last_error") or (recording.get("review") or {}).get("message") or ""),
		})
	job["tracks"] = tracks
	job["counts"] = counts
	job_id = str(job.get("jobId") or "")
	if job_id:
		def merge(current: dict[str, Any]) -> bool:
			if current.get("status") in {"cancelled", "cancelling", "superseded"}:
				return False
			new_source = dict(current.get("source") or {})
			new_source.update({"name": job["source"]["name"], "total": job["source"]["total"]})
			if (
				new_source == current.get("source")
				and current.get("currentTrack") == job.get("currentTrack")
				and current.get("tracks") == tracks
				and current.get("counts") == counts
			):
				return False
			current["source"] = new_source
			current["currentTrack"] = job.get("currentTrack")
			current["tracks"] = tracks
			current["counts"] = counts
			return True
		updated = store.update(job_id, merge)
		for key in ("source", "currentTrack", "tracks", "counts"):
			if key in updated:
				job[key] = updated[key]
	return job


def _set_current_track(job: dict[str, Any], store: JobStore, label: str, phase: str) -> None:
	job["phase"] = phase
	needle = label.casefold().strip()
	match = next(
		(
			track for track in job.get("tracks") or []
			if track.get("title", "").casefold() in needle or needle.endswith(track.get("title", "").casefold())
		),
		None,
	)
	job["currentTrack"] = dict(match) if match else {"title": label.strip(), "artists": "", "recordingId": None}
	job["currentTrack"]["state"] = phase
	_persist_progress(job, store)
	sync_from_manifest(job, store)


class JobLogWriter:
	def __init__(self, handle: TextIO, job: dict[str, Any], store: JobStore):
		self.handle = handle
		self.job = job
		self.store = store
		self.buffer = ""

	def write(self, value: str) -> int:
		self.handle.write(value)
		self.handle.flush()
		self.buffer += value
		while "\n" in self.buffer:
			line, self.buffer = self.buffer.split("\n", 1)
			self._line(line.strip())
		return len(value)

	def flush(self) -> None:
		self.handle.flush()

	def _line(self, line: str) -> None:
		if not line:
			return
		if line.startswith("Searching YouTube:"):
			_set_current_track(self.job, self.store, line.split(":", 1)[1], "searching_youtube")
		elif line.startswith(("Downloading album track:", "Downloading:")):
			_set_current_track(self.job, self.store, line.split(":", 1)[1], "downloading")
		elif line.startswith("Upgrading Crate-managed MP3") or "ffmpeg" in line.casefold():
			self.job["phase"] = "tagging"
			sync_from_manifest(self.job, self.store)
		elif line.startswith(("Needs review:", "Resumable failure:")):
			sync_from_manifest(self.job, self.store)


def _commands(job: dict[str, Any]) -> list[list[str]]:
	url = str(job["source"]["url"])
	if job.get("action") == "playlist_update_combined":
		playlist_id = str(job["source"]["id"])
		prepare = ["playlist-update-prepare", playlist_id, "--confirm-download"]
		if job.get("confirmationToken"):
			prepare += ["--confirmation-token", str(job["confirmationToken"])]
		if job.get("resumeUpdate"):
			prepare.append("--resume")
		return [prepare, ["playlist-update-apply", playlist_id, "--confirm-music-write"]]
	if job["source"]["type"] == "album":
		return [["album-import", url, "--confirm-download"], ["album-apply", url, "--confirm-music-write"]]
	return [["import", url, "--confirm-download"], ["apply", url, "--confirm-music-write"]]


def update_structured_progress(job: dict[str, Any], store: JobStore, event: dict[str, Any]) -> None:
	phase = str(event.get("phase") or "")
	if event.get("name"):
		job["source"]["name"] = str(event["name"])
	if event.get("total") is not None:
		job["source"]["total"] = int(event.get("total") or 0)
	phase_map = {
		"downloaded": "downloading",
		"reused": "downloading",
		"youtube_match_found": "matching_youtube",
		"matches_need_approval": "matching_youtube",
		"no_youtube_matches": "matching_youtube",
		"complete": "adding_to_music",
		"needs_attention": "planning_downloads",
		"failed": "downloading",
	}
	job["phase"] = phase_map.get(phase, phase or str(job.get("phase") or "running"))
	if event.get("recording_id") or event.get("title"):
		job["currentTrack"] = {
			"recordingId": event.get("recording_id"),
			"position": int(event.get("position") or 0),
			"title": str(event.get("title") or ""),
			"artists": str(event.get("artists") or ""),
			"state": phase,
		}
		if event.get("recording_id"):
			job.setdefault("trackProgress", {})[str(event["recording_id"])] = phase
	_persist_progress(job, store)
	sync_from_manifest(job, store)


def _terminal_status(job: dict[str, Any], code: int) -> str:
	counts = job.get("counts") or {}
	if code and job.get("errorSummary"):
		return "failed"
	if counts.get("review") or counts.get("failed"):
		return "needs_attention"
	return "complete" if code == 0 else "failed"


def _raycast_deeplink(job_id: str) -> str:
	author = str(os.environ.get("CRATE_RAYCAST_AUTHOR") or "").strip()
	extension = str(os.environ.get("CRATE_RAYCAST_EXTENSION") or "crate-music-importer").strip()
	if not author:
		raise ValueError("CRATE_RAYCAST_AUTHOR is required for background notifications.")
	context = quote(json.dumps({"jobId": job_id, "terminalEvent": True}, separators=(",", ":")))
	return (
		f"raycast://extensions/{author}/{extension}/{RAYCAST_NOTIFICATION_COMMAND}"
		f"?launchType=background&context={context}"
	)


def notify_raycast(
	job_id: str,
	*,
	opener: Callable[..., subprocess.CompletedProcess] = subprocess.run,
	sleeper: Callable[[float], None] = time.sleep,
) -> None:
	try:
		url = _raycast_deeplink(job_id)
	except ValueError:
		return
	for attempt in range(3):
		try:
			result = opener(
				["/usr/bin/open", "-g", url],
				capture_output=True,
				text=True,
				timeout=10,
				check=False,
			)
		except (OSError, subprocess.TimeoutExpired):
			result = None
		if result is not None and getattr(result, "returncode", 1) == 0:
			return
		if attempt < 2:
			sleeper(0.25 * (2 ** attempt))
	# The pending event remains durable and the next successful launch drains it.


def run_job(
	job: dict[str, Any],
	store: JobStore,
	*,
	engine: Callable[[list[str]], int] = cli.run,
	notifier: Callable[[str], None] = notify_raycast,
) -> dict[str, Any]:
	store.logs_dir.mkdir(parents=True, exist_ok=True)
	log_path = store.logs_dir / f"import-{job['source']['type']}-{job['jobId'][:10]}.log"
	with store.transaction_lock():
		current = store.load(str(job["jobId"]))
		if current.get("status") in {"cancelled", "cancelling", "superseded", "complete"}:
			return current
		current.update({
			"status": "running",
			"phase": "loading_metadata",
			"pid": os.getpid(),
			"startedAt": current.get("startedAt") or _now(),
			"finishedAt": None,
			"logPath": str(log_path),
			"errorSummary": None,
			"retryable": False,
		})
		job.clear()
		job.update(store.save(current))
	code = 0
	try:
		with log_path.open("a", encoding="utf-8") as log_handle:
			writer = JobLogWriter(log_handle, job, store)
			with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
				for index, command in enumerate(_commands(job)):
					job["phase"] = "loading_metadata" if index == 0 else "updating_music"
					job["currentTrack"] = None
					sync_from_manifest(job, store)
					if engine is cli.run:
						code = cli.run(command, on_progress=lambda event: update_structured_progress(job, store, event))
					else:
						code = engine(command)
					sync_from_manifest(job, store)
					if code:
						break
	except Exception as exc:
		code = 2
		job["errorSummary"] = str(exc)[:500]
		job["retryable"] = True
		current_id = str((job.get("currentTrack") or {}).get("recordingId") or "")
		if current_id:
			def record_failure(manifest: dict[str, Any]) -> None:
				recording = manifest.get("recordings", {}).get(current_id)
				if recording is not None:
					recording["last_error"] = str(exc)
					recording["last_error_source"] = {"type": job["source"]["type"], "id": job["source"]["id"]}
			update_manifest(ManagedPaths(store.root), record_failure)
		job["currentTrack"] = None
	sync_from_manifest(job, store)
	status = _terminal_status(job, code)
	job["status"] = status
	job["phase"] = status
	job["retryable"] = status == "failed"
	job["pid"] = None
	job["currentTrack"] = None
	job["finishedAt"] = _now()
	sync_from_manifest(job, store)
	if status == "needs_attention":
		job["errorSummary"] = job.get("errorSummary") or "Resolve or retry tracks in Import Activity."
	elif status == "failed" and not job.get("errorSummary"):
		job["errorSummary"] = "The import stopped before completion."
	job["notification"] = {"pending": True, "notifiedAt": None}
	with store.transaction_lock():
		current = store.load(str(job["jobId"]))
		if current.get("status") in {"cancelled", "cancelling", "superseded", "complete"}:
			job.clear()
			job.update(current)
			return job
		current.update(job)
		job.clear()
		job.update(store.save(current))
	try:
		notifier(str(job["jobId"]))
	except Exception:
		# Notification delivery is best effort; durable pending state must not stop the import queue.
		pass
	return job


def recover_interrupted(store: JobStore, jobs: list[dict[str, Any]] | None = None) -> None:
	job_rows = store.list() if jobs is None else jobs
	# A newly written startup marker carries the PID that owns its bounded
	# startup lease. If that process died before publishing a runner PID, make
	# its queued work retryable on a read/poll; do not launch imports here.
	marker = store.runner()
	starter_pid = _safe_pid((marker or {}).get("starterPid"))
	if marker and marker.get("status") == "starting" and starter_pid > 0 and not _pid_running(starter_pid):
		with store.transaction_lock():
			latest_marker = store.runner() or {}
			if latest_marker.get("status") == "starting" and latest_marker.get("startupToken") == marker.get("startupToken") and _safe_pid(latest_marker.get("starterPid")) == starter_pid and not _pid_running(starter_pid):
				for row in job_rows:
					if row.get("malformedRecord") or row.get("status") != "queued":
						continue
					job_id = str(row.get("jobId") or "")
					try:
						current = store.load(job_id)
					except (OSError, ValueError, json.JSONDecodeError):
						continue
					if current.get("status") != "queued":
						continue
					current["status"] = "failed"
					current["phase"] = "failed"
					current["pid"] = None
					current["finishedAt"] = _now()
					current["errorSummary"] = "The queue runner stopped during startup. Retry this import."
					current["retryable"] = True
					current["notification"] = {"pending": True, "notifiedAt": None}
					row.clear()
					row.update(store.save(current))
				try:
					store.runner_path.unlink()
				except FileNotFoundError:
					pass
	for job in job_rows:
		if job.get("malformedRecord"):
			continue
		if job.get("status") not in {"running", "cancelling"}:
			continue
		pid = _safe_pid(job.get("pid"))
		if job.get("status") == "cancelling":
			owner_pid = _safe_pid(job.get("cancellationOwnerPid"))
			if owner_pid and _pid_running(owner_pid):
				continue
			def recover_cancellation(current: dict[str, Any]) -> bool:
				if current.get("status") != "cancelling" or _safe_pid(current.get("cancellationOwnerPid")) != owner_pid:
					return False
				current["status"] = "failed"
				current["phase"] = "failed"
				current["pid"] = None
				current.pop("cancellationOwnerPid", None)
				current["finishedAt"] = _now()
				current["errorSummary"] = "Cancellation cleanup stopped before it finished. Review this source before resuming."
				current["retryable"] = False
				current["notification"] = {"pending": True, "notifiedAt": None}
				return True
			recovered = store.update(str(job["jobId"]), recover_cancellation)
			job.clear()
			job.update(recovered)
			continue
		if _pid_running(pid):
			continue
		def recover(current: dict[str, Any]) -> bool:
			if current.get("status") != "running" or _safe_pid(current.get("pid")) != pid:
				return False
			current["status"] = "failed"
			current["phase"] = "failed"
			current["pid"] = None
			current["finishedAt"] = _now()
			current["errorSummary"] = "The previous runner stopped. Retry this import."
			current["retryable"] = True
			current["notification"] = {"pending": True, "notifiedAt": None}
			return True
		recovered = store.update(str(job["jobId"]), recover)
		job.clear()
		job.update(recovered)


def run_queue(*, root: Path = MANAGED_ROOT, engine: Callable[[list[str]], int] = cli.run, notifier: Callable[[str], None] = notify_raycast) -> None:
	from crate_music_importer.ipod_import.dependency_lock import dependency_lock
	with dependency_lock():
		_run_queue(root=root, engine=engine, notifier=notifier)


def _run_queue(
	*,
	root: Path = MANAGED_ROOT,
	engine: Callable[[list[str]], int] = cli.run,
	notifier: Callable[[str], None] = notify_raycast,
) -> None:
	store = JobStore(root)
	while True:
		with store.worker_lock() as acquired:
			if not acquired:
				return
			recover_interrupted(store)
			while True:
				with store.transaction_lock():
					queued = sorted(
						(job for job in store.list() if job.get("status") == "queued"),
						key=lambda job: (int(job.get("queueSequence") or 0), str(job.get("createdAt") or "")),
					)
					if not queued:
						break
					try:
						queued_job = store.load(str(queued[0]["jobId"]))
					except ValueError:
						continue
					if queued_job.get("status") != "queued":
						continue
					queued_job.update(status="running", phase="loading_metadata", pid=os.getpid(), startedAt=queued_job.get("startedAt") or _now())
					store.save(queued_job)
				run_job(queued_job, store, engine=engine, notifier=notifier)
		# Drop the worker lock before checking idle state. If enqueue arrives in
		# this gap it sees our live runner marker; this check then reacquires work.
		with store.transaction_lock():
			if any(job.get("status") == "queued" for job in store.list()):
				continue
			store.release_runner(os.getpid())
			return


def _same_source(left: dict[str, Any], right: dict[str, Any]) -> bool:
	left_source = left.get("source") if isinstance(left.get("source"), dict) else {}
	right_source = right.get("source") if isinstance(right.get("source"), dict) else {}
	return left_source.get("type") == right_source.get("type") and left_source.get("id") == right_source.get("id")


def public_job(job: dict[str, Any]) -> dict[str, Any]:
	"""Remove internal recovery data that the Raycast UI never reads."""
	value = dict(job)
	value.pop("stateBaseline", None)
	value.pop("cacheBaseline", None)
	value.pop("runnerPid", None)
	value.pop("cancellationOwnerPid", None)
	value.pop("cancellationCleanup", None)
	value.pop("cancellationCleanup", None)
	value.pop("queueSequence", None)
	return value


def reconcile_terminal_jobs(store: JobStore, manifest: dict[str, Any] | None = None) -> list[dict[str, Any]]:
	"""Clear resolved attention rows and retire older attempts using one snapshot."""
	if manifest is None:
		try:
			manifest = load_manifest(ManagedPaths(store.root))
		except (OSError, ValueError):
			manifest = None
	jobs = store.list()
	recover_interrupted(store, jobs)
	eligible = {"queued", "running", "complete", "needs_attention", "ready_to_continue", "failed"}
	by_source: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
	for candidate in jobs:
		source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
		by_source.setdefault((source.get("type"), source.get("id")), []).append(candidate)
	newest_by_source = {
		source_key: max(
			(attempt for attempt in attempts if attempt.get("status") in eligible),
			key=lambda value: (str(value.get("createdAt") or ""), str(value.get("jobId") or "")),
			default=None,
		)
		for source_key, attempts in by_source.items()
	}
	for job in jobs:
		if job.get("status") not in {"needs_attention", "ready_to_continue", "failed"}:
			continue
		source = job.get("source") if isinstance(job.get("source"), dict) else {}
		newer = newest_by_source.get((source.get("type"), source.get("id")))
		if newer and str(newer.get("createdAt") or "") <= str(job.get("createdAt") or ""):
			newer = None
		if not newer:
			continue
		job_id = str(job["jobId"])
		def retire(current: dict[str, Any]) -> bool:
			if current.get("status") not in {"needs_attention", "ready_to_continue", "failed"}:
				return False
			current["status"] = "superseded"
			current["phase"] = "superseded"
			current["supersededBy"] = newer.get("jobId")
			current["retryable"] = False
			current["notification"] = {"pending": False, "notifiedAt": (current.get("notification") or {}).get("notifiedAt")}
			return True
		job.update(store.update(job_id, retire))
	for job in jobs:
		if job.get("status") not in {"needs_attention", "ready_to_continue", "failed"}:
			continue
		job = sync_from_manifest(job, store, manifest)
		if job.get("status") == "failed":
			continue
		counts = job.get("counts") or {}
		if counts.get("review") or counts.get("failed"):
			if job.get("status") == "ready_to_continue":
				def attention(current: dict[str, Any]) -> bool:
					if current.get("status") != "ready_to_continue":
						return False
					current["status"] = "needs_attention"
					current["phase"] = "needs_attention"
					return True
				job.update(store.update(str(job["jobId"]), attention))
			continue
		target_status = "complete" if counts.get("total") and counts.get("complete") == counts.get("total") else "ready_to_continue"
		def resolve(current: dict[str, Any]) -> bool:
			if current.get("status") not in {"needs_attention", "ready_to_continue"}:
				return False
			if current.get("status") == target_status and not current.get("errorSummary") and not current.get("retryable") and not (current.get("notification") or {}).get("pending"):
				return False
			current["status"] = target_status
			current["phase"] = target_status
			current["errorSummary"] = None
			current["retryable"] = False
			current["notification"] = {"pending": False, "notifiedAt": (current.get("notification") or {}).get("notifiedAt")}
			return True
		job.update(store.update(str(job["jobId"]), resolve))
	return jobs


def jobs_snapshot(
	*,
	root: Path = MANAGED_ROOT,
	manifest: dict[str, Any] | None = None,
	jobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
	store = JobStore(root)
	if jobs is None:
		if manifest is None:
			try:
				manifest = load_manifest(ManagedPaths(root))
			except (OSError, ValueError):
				manifest = None
		jobs = reconcile_terminal_jobs(store, manifest)
	visible = [job for job in jobs if job.get("status") in {"queued", "running", "needs_attention", "ready_to_continue", "failed"}]
	visible.extend(job for job in jobs if job.get("status") == "complete")
	return {
		"version": 1,
		"managedRoot": str(root),
		"jobs": [public_job(job) for job in visible],
		"pendingNotifications": [
			public_job(job) for job in jobs if (job.get("notification") or {}).get("pending")
		],
	}
