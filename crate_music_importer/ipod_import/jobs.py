"""Durable, single-runner background jobs for the native Raycast extension."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
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
from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest, save_manifest
from crate_music_importer.ipod_import.music import delete_importer_owned_music_track, lookup_importer_owned_music_track
from crate_music_importer.ipod_import.music_cache import MusicCacheUnavailableError, load_music_cache, remove_music_cache_tracks
from crate_music_importer.ipod_import.spotify import parse_source_url


ACTIVE_STATES = {"queued", "running"}
TERMINAL_STATES = {"complete", "needs_attention", "ready_to_continue", "failed", "cancelled", "superseded"}
RAYCAST_EXTENSION = os.environ.get("CRATE_RAYCAST_EXTENSION", "crate-music-importer")
RAYCAST_ACTIVITY_COMMAND = "import-activity-problems"
RAYCAST_AUTHOR = os.environ.get("CRATE_RAYCAST_AUTHOR", "elijahsanner")


def _now() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.NamedTemporaryFile(
		"w",
		encoding="utf-8",
		dir=path.parent,
		prefix=path.stem + "-",
		suffix=".tmp",
		delete=False,
	) as handle:
		json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
		handle.write("\n")
		temporary = Path(handle.name)
	os.replace(temporary, path)


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


class JobStore:
	def __init__(self, root: Path = MANAGED_ROOT):
		self.root = Path(root)
		self.state_dir = self.root / ".state"
		self.jobs_dir = self.state_dir / "jobs"
		self.logs_dir = self.state_dir / "logs"
		self.runner_path = self.state_dir / "job-runner.json"
		self.enqueue_lock_path = self.state_dir / "job-enqueue.lock"
		self.worker_lock_path = self.state_dir / "import-mutation.lock"

	def job_path(self, job_id: str) -> Path:
		return self.jobs_dir / f"{job_id}.json"

	def save(self, job: dict[str, Any]) -> dict[str, Any]:
		job["updatedAt"] = _now()
		_atomic_json(self.job_path(str(job["jobId"])), job)
		return job

	def load(self, job_id: str) -> dict[str, Any]:
		path = self.job_path(job_id)
		if not path.is_file():
			raise ValueError(f"Unknown import job: {job_id}")
		with path.open("r", encoding="utf-8") as handle:
			value = json.load(handle)
		if not isinstance(value, dict):
			raise ValueError(f"Invalid import job: {job_id}")
		return value

	def list(self) -> list[dict[str, Any]]:
		jobs: list[dict[str, Any]] = []
		if not self.jobs_dir.is_dir():
			return jobs
		for path in self.jobs_dir.glob("*.json"):
			try:
				with path.open("r", encoding="utf-8") as handle:
					value = json.load(handle)
			except (OSError, ValueError):
				continue
			if isinstance(value, dict) and value.get("jobId"):
				jobs.append(value)
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
		locked_pid = int(current.get("pid") or 0)
		if locked_pid and locked_pid != pid:
			return
		try:
			self.runner_path.unlink()
		except FileNotFoundError:
			pass

	@contextlib.contextmanager
	def enqueue_lock(self):
		self.state_dir.mkdir(parents=True, exist_ok=True)
		with self.enqueue_lock_path.open("a+", encoding="utf-8") as handle:
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
	cache_baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
	source_type, source_id, canonical = parse_source_url(url)
	expected_action = f"{source_type}_combined"
	if action not in ("source_combined", expected_action):
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
			"id": source_id,
			"url": canonical,
			"name": str(seed.get("name") or ("Spotify Album" if source_type == "album" else "Spotify Playlist")),
			"total": total,
		},
		"action": expected_action,
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
		"cacheBaseline": cache_baseline or {"captured": False, "persistentIds": []},
	}


def _capture_cache_baseline(root: Path) -> dict[str, Any]:
	try:
		cache = load_music_cache(ManagedPaths(root))
	except MusicCacheUnavailableError:
		return {"captured": False, "persistentIds": []}
	return {"captured": True, "persistentIds": sorted(cache["tracks"])}


def _runner_command() -> list[str]:
	return [sys.executable, "-u", "-m", "crate_music_importer.ipod_import.raycast", "__runner"]


def _ensure_runner(
	store: JobStore,
	*,
	popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> int | None:
	current = store.runner() or {}
	current_pid = int(current.get("pid") or 0)
	if _pid_running(current_pid):
		return current_pid
	try:
		runner_age = time.time() - store.runner_path.stat().st_mtime
	except FileNotFoundError:
		runner_age = None
	if runner_age is not None and runner_age < 30 and (not current or current.get("status") == "starting"):
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
		json.dump({"pid": None, "status": "starting", "startedAt": _now()}, handle)
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
		_atomic_json(store.runner_path, {"pid": process.pid, "status": "running", "startedAt": _now()})
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
	cache_baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
	store = JobStore(root)
	job = _new_job(action, url, seed, cache_baseline=cache_baseline or _capture_cache_baseline(root))
	if retry_of:
		job["retryOf"] = retry_of
	with store.enqueue_lock():
		existing_jobs = store.list()
		for existing in existing_jobs:
			if (
				existing.get("status") in ACTIVE_STATES
				and existing.get("source", {}).get("type") == job["source"]["type"]
				and existing.get("source", {}).get("id") == job["source"]["id"]
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
		job["runnerPid"] = _ensure_runner(store, popen=popen)
		store.save(job)
	except Exception:
		job["status"] = "failed"
		job["phase"] = "failed"
		job["finishedAt"] = _now()
		job["errorSummary"] = "Could not start the import runner."
		job["retryable"] = True
		job["notification"] = {"pending": True, "notifiedAt": None}
		store.save(job)
		raise
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


def _delete_source_progress(
	job: dict[str, Any],
	store: JobStore,
	*,
	delete_music: Callable[[str, str], bool] = delete_importer_owned_music_track,
	lookup_imported: Callable[[str], dict[str, Any] | None] = lookup_importer_owned_music_track,
) -> dict[str, int]:
	paths = ManagedPaths(store.root)
	manifest = load_manifest(paths)
	source = job.get("source") or {}
	source_type = str(source.get("type") or "")
	source_id = str(source.get("id") or "")
	collection_name = "albums" if source_type == "album" else "playlists"
	collection = manifest.get(collection_name) or {}
	source_value = collection.pop(source_id, None)
	if not isinstance(source_value, dict):
		return {"recordings": 0, "files": 0, "music_tracks": 0, "cache_entries": 0}
	baseline = job.get("cacheBaseline") or {}
	baseline_captured = baseline.get("captured") is True
	protected_ids = {str(value) for value in baseline.get("persistentIds") or [] if str(value)}

	deleted_files = 0
	if source_type == "playlist" and source_value.get("m3u8_tool_owned"):
		deleted_files += int(_delete_managed_file(store.root, str(source_value.get("m3u8_relative_path") or "")))
	affected_ids = {str(item.get("recording_id") or "") for item in source_value.get("items") or []}
	deleted_recordings = 0
	deleted_music_tracks = 0
	cache_ids_to_remove: set[str] = set()
	for recording_id in affected_ids:
		recording = manifest.get("recordings", {}).get(recording_id)
		if not isinstance(recording, dict):
			continue
		memberships = recording.setdefault(f"{source_type}_memberships", {})
		memberships.pop(source_id, None)
		if recording.get("album_memberships") or recording.get("playlist_memberships"):
			continue
		music = recording.get("music") or {}
		active = recording.get("active_reference") or {}
		persistent_id = str(music.get("persistent_id") or active.get("persistent_id") or "")
		if persistent_id in protected_ids:
			continue
		music_source = str(music.get("source") or "")
		importer_owned = music_source in {"managed_import", "managed_album_import", "managed_album_upgrade"}
		if persistent_id:
			if not baseline_captured or not importer_owned:
				continue
			deleted_music_tracks += int(delete_music(persistent_id, recording_id))
			cache_ids_to_remove.add(persistent_id)
		elif baseline_captured and recording.get("music_import_pending"):
			candidate = lookup_imported(recording_id)
			candidate_id = str((candidate or {}).get("persistent_id") or "")
			if candidate_id in protected_ids:
				continue
			if candidate_id:
				deleted_music_tracks += int(delete_music(candidate_id, recording_id))
				cache_ids_to_remove.add(candidate_id)
		managed = recording.get("managed_file") or {}
		if managed.get("tool_owned"):
			deleted_files += int(_delete_managed_file(store.root, str(managed.get("relative_path") or "")))
		staging = paths.staging / recording_id
		if staging.is_dir():
			shutil.rmtree(staging)
		manifest.get("manual_youtube_overrides", {}).pop(recording_id, None)
		manifest["recordings"].pop(recording_id, None)
		deleted_recordings += 1
	save_manifest(paths, manifest)
	removed_cache_entries = remove_music_cache_tracks(paths, cache_ids_to_remove) if cache_ids_to_remove else 0
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
	delete_music: Callable[[str, str], bool] = delete_importer_owned_music_track,
	lookup_imported: Callable[[str], dict[str, Any] | None] = lookup_importer_owned_music_track,
) -> dict[str, Any]:
	store = JobStore(root)
	job = store.load(job_id)
	if job.get("status") == "complete":
		raise ValueError("A completed import cannot be cancelled or have its progress removed.")
	if job.get("status") in {"cancelled", "superseded"}:
		raise ValueError("This import is no longer active or resumable.")
	with store.enqueue_lock():
		job = store.load(job_id)
		if job.get("status") == "running":
			pid = int(job.get("pid") or 0)
			runner_pid = int((store.runner() or {}).get("pid") or 0)
			if pid and runner_pid and pid != runner_pid:
				raise ValueError("The running import no longer matches the active importer process.")
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
			store.release_runner(pid)

		job["status"] = "cancelled"
		job["phase"] = "cancelled"
		job["finishedAt"] = _now()
		job["notification"] = {"pending": False, "notifiedAt": None}
		store.save(job)
		removed = _delete_source_progress(job, store, delete_music=delete_music, lookup_imported=lookup_imported)
		removed_job_ids: list[str] = []
		for existing in store.list():
			if existing.get("status") == "complete" or not _same_source(existing, job):
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
	delete_music: Callable[[str, str], bool] = delete_importer_owned_music_track,
	lookup_imported: Callable[[str], dict[str, Any] | None] = lookup_importer_owned_music_track,
) -> dict[str, Any]:
	"""Remove an incomplete saved source even when its legacy job was marked complete."""
	if source_type not in {"album", "playlist"}:
		raise ValueError("The source type must be album or playlist.")
	store = JobStore(root)
	with store.enqueue_lock():
		matching_jobs = [
			job for job in store.list()
			if (job.get("source") or {}).get("type") == source_type
			and (job.get("source") or {}).get("id") == source_id
		]
		if any(job.get("status") in ACTIVE_STATES for job in matching_jobs):
			raise ValueError("This import became active. Cancel it from the Active & Queued section instead.")
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
			active = recording.get("active_reference") or {}
			return active.get("kind") == "existing_music" or bool(
				active.get("kind") == "managed_file" and (recording.get("music") or {}).get("persistent_id")
			)
		fully_imported = bool(items) and all(item_fully_imported(item) for item in items)
		if fully_imported:
			raise ValueError("A completed import cannot be cancelled or have its progress removed.")
		baseline_values = [job.get("cacheBaseline") or {} for job in matching_jobs]
		job_like = {
			"source": {
				"type": source_type,
				"id": source_id,
				"name": str(source_value.get("name") or "Spotify source"),
				"url": str(source_value.get("spotify_url") or ""),
			},
			"cacheBaseline": {
				"captured": any(value.get("captured") is True for value in baseline_values),
				"persistentIds": sorted({
					str(persistent_id)
					for value in baseline_values
					for persistent_id in value.get("persistentIds") or []
					if str(persistent_id)
				}),
			},
		}
		removed = _delete_source_progress(job_like, store, delete_music=delete_music, lookup_imported=lookup_imported)
		removed_job_ids: list[str] = []
		for job in matching_jobs:
			if job.get("status") == "complete":
				continue
			try:
				store.job_path(str(job["jobId"])).unlink()
			except FileNotFoundError:
				pass
			removed_job_ids.append(str(job["jobId"]))
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
		cache_baseline=original.get("cacheBaseline") or {"captured": False, "persistentIds": []},
	)
	original["status"] = "superseded"
	original["phase"] = "superseded"
	original["retryable"] = False
	original["supersededBy"] = retried["jobId"]
	store.save(original)
	return retried


def acknowledge_notification(job_id: str, *, root: Path = MANAGED_ROOT) -> dict[str, Any]:
	store = JobStore(root)
	job = store.load(job_id)
	notification = job.setdefault("notification", {})
	notification["pending"] = False
	notification["notifiedAt"] = _now()
	return store.save(job)


def _source_from_manifest(job: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any] | None:
	source = job["source"]
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
	active = recording.get("active_reference") or {}
	if active.get("kind") == "existing_music":
		return "reused"
	if active.get("kind") == "managed_file":
		return "complete" if (recording.get("music") or {}).get("persistent_id") else "downloaded"
	if (recording.get("youtube") or {}).get("url"):
		return "youtube_match_found"
	return "not_started"


def sync_from_manifest(job: dict[str, Any], store: JobStore) -> dict[str, Any]:
	before = json.dumps({
		"source": job.get("source"),
		"currentTrack": job.get("currentTrack"),
		"tracks": job.get("tracks"),
		"counts": job.get("counts"),
	}, sort_keys=True)
	try:
		manifest = load_manifest(ManagedPaths(store.root))
	except (OSError, ValueError):
		return job
	source_value = _source_from_manifest(job, manifest)
	if not source_value:
		return job
	job["source"]["name"] = str(source_value.get("name") or job["source"]["name"])
	items = sorted(source_value.get("items") or [], key=lambda item: int(item.get("position") or 0))
	job["source"]["total"] = len(items)
	current_id = str((job.get("currentTrack") or {}).get("recordingId") or "") or None
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
		state = _track_state(recording, current_id=current_id, current_state=current_state)
		if state == "downloaded":
			counts["downloaded"] += 1
		if state == "reused":
			counts["reused"] += 1
		if state in {"downloaded", "complete", "reused"}:
			counts["ready"] += 1
		if state in {"complete", "reused"}:
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
			"title": str(metadata.get("title") or ""),
			"artists": str(metadata.get("artists") or ""),
			"state": state,
			"detail": str(recording.get("last_error") or (recording.get("review") or {}).get("message") or ""),
		})
	job["tracks"] = tracks
	job["counts"] = counts
	after = json.dumps({
		"source": job.get("source"),
		"currentTrack": job.get("currentTrack"),
		"tracks": job.get("tracks"),
		"counts": job.get("counts"),
	}, sort_keys=True)
	return store.save(job) if after != before else job


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
		elif line.startswith("Upgrading importer-owned MP3") or "ffmpeg" in line.casefold():
			self.job["phase"] = "tagging"
			sync_from_manifest(self.job, self.store)
		elif line.startswith(("Needs review:", "Resumable failure:")):
			sync_from_manifest(self.job, self.store)


def _commands(job: dict[str, Any]) -> list[list[str]]:
	url = str(job["source"]["url"])
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
	sync_from_manifest(job, store)


def _terminal_status(job: dict[str, Any], code: int) -> str:
	counts = job.get("counts") or {}
	if counts.get("review") or counts.get("failed"):
		return "needs_attention"
	return "complete" if code == 0 else "failed"


def _raycast_deeplink(job_id: str) -> str:
	context = quote(json.dumps({"jobId": job_id, "terminalEvent": True}, separators=(",", ":")))
	return (
		f"raycast://extensions/{RAYCAST_AUTHOR}/{RAYCAST_EXTENSION}/{RAYCAST_ACTIVITY_COMMAND}"
		f"?launchType=background&context={context}"
	)


def notify_raycast(job_id: str, *, opener: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
	try:
		opener(
			["/usr/bin/open", "-g", _raycast_deeplink(job_id)],
			capture_output=True,
			text=True,
			timeout=10,
			check=False,
		)
	except (OSError, subprocess.TimeoutExpired):
		# The pending event remains durable and will be shown next time Activity opens.
		return


def run_job(
	job: dict[str, Any],
	store: JobStore,
	*,
	engine: Callable[[list[str]], int] = cli.run,
	notifier: Callable[[str], None] = notify_raycast,
) -> dict[str, Any]:
	store.logs_dir.mkdir(parents=True, exist_ok=True)
	log_path = store.logs_dir / f"import-{job['source']['type']}-{job['jobId'][:10]}.log"
	job.update({
		"status": "running",
		"phase": "loading_metadata",
		"pid": os.getpid(),
		"startedAt": job.get("startedAt") or _now(),
		"finishedAt": None,
		"logPath": str(log_path),
		"errorSummary": None,
		"retryable": False,
	})
	store.save(job)
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
	sync_from_manifest(job, store)
	status = _terminal_status(job, code)
	job["status"] = status
	job["phase"] = status
	job["pid"] = None
	job["currentTrack"] = None
	job["finishedAt"] = _now()
	if status == "needs_attention":
		job["errorSummary"] = "Resolve or retry tracks in Import Activity."
	elif status == "failed" and not job.get("errorSummary"):
		job["errorSummary"] = "The import stopped before completion."
	job["notification"] = {"pending": True, "notifiedAt": None}
	store.save(job)
	notifier(str(job["jobId"]))
	return job


def recover_interrupted(store: JobStore) -> None:
	for job in store.list():
		if job.get("status") != "running":
			continue
		pid = int(job.get("pid") or 0)
		if _pid_running(pid):
			continue
		job["status"] = "failed"
		job["phase"] = "failed"
		job["pid"] = None
		job["finishedAt"] = _now()
		job["errorSummary"] = "The previous runner stopped. Retry this import."
		job["retryable"] = True
		job["notification"] = {"pending": True, "notifiedAt": None}
		store.save(job)


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
	with store.worker_lock() as acquired:
		if not acquired:
			return
		try:
			recover_interrupted(store)
			while True:
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
				run_job(queued_job, store, engine=engine, notifier=notifier)
		finally:
			store.release_runner(os.getpid())


def _same_source(left: dict[str, Any], right: dict[str, Any]) -> bool:
	left_source = left.get("source") or {}
	right_source = right.get("source") or {}
	return left_source.get("type") == right_source.get("type") and left_source.get("id") == right_source.get("id")


def reconcile_terminal_jobs(store: JobStore) -> list[dict[str, Any]]:
	"""Clear resolved attention rows and retire older attempts for the same source."""
	jobs = store.list()
	for job in sorted(jobs, key=lambda value: str(value.get("createdAt") or "")):
		if job.get("status") not in {"needs_attention", "ready_to_continue", "failed"}:
			continue
		newer = next(
			(
				candidate for candidate in jobs
				if str(candidate.get("createdAt") or "") > str(job.get("createdAt") or "")
				and candidate.get("status") in {"queued", "running", "complete"}
				and _same_source(job, candidate)
			),
			None,
		)
		if not newer:
			continue
		job["status"] = "superseded"
		job["phase"] = "superseded"
		job["supersededBy"] = newer.get("jobId")
		job["retryable"] = False
		job["notification"] = {"pending": False, "notifiedAt": (job.get("notification") or {}).get("notifiedAt")}
		store.save(job)
	jobs = store.list()
	for job in jobs:
		if job.get("status") != "needs_attention":
			continue
		job = sync_from_manifest(job, store)
		counts = job.get("counts") or {}
		if counts.get("review") or counts.get("failed"):
			continue
		job["status"] = "complete" if counts.get("total") and counts.get("complete") == counts.get("total") else "ready_to_continue"
		job["phase"] = job["status"]
		job["errorSummary"] = None
		job["retryable"] = False
		job["notification"] = {"pending": False, "notifiedAt": (job.get("notification") or {}).get("notifiedAt")}
		store.save(job)
	return store.list()


def jobs_snapshot(*, root: Path = MANAGED_ROOT) -> dict[str, Any]:
	store = JobStore(root)
	jobs = reconcile_terminal_jobs(store)
	return {
		"version": 1,
		"managedRoot": str(root),
		"jobs": jobs,
		"pendingNotifications": [
			job for job in jobs if (job.get("notification") or {}).get("pending")
		],
	}
