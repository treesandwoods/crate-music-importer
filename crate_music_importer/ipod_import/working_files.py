"""Explicit review of working material; audio is never an automatic cleanup candidate."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crate_music_importer.ipod_import.manifest import ManagedPaths, file_sha256


def review_working_files(paths: ManagedPaths, manifest: dict[str, Any]) -> dict[str, Any]:
	rows = []
	jobs = []
	for path in paths.state_dir.rglob("*.json") if paths.state_dir.exists() else []:
		if "job" in path.name or "jobs" in path.parts or "journal" in path.parts:
			try:
				jobs.append(json.loads(path.read_text()))
			except (OSError, ValueError):
				jobs.append({"unreadable": str(path)})
	active_text = json.dumps([job for job in jobs if job.get("status") in ("running", "queued", "starting")])
	recovery_text = json.dumps([job for job in jobs if job.get("status") not in ("complete", "cancelled", "superseded") or job.get("unreadable")])
	for path in sorted(paths.staging.rglob("*")) if paths.staging.exists() else []:
		if not path.is_file() and not path.is_symlink():
			continue
		relative = path.relative_to(paths.staging)
		rid = relative.parts[0]
		recording = manifest.get("recordings", {}).get(rid)
		category, reason, eligible = "unknown", "No verified lifecycle record; retain for review.", False
		if path.is_symlink():
			category, reason = "unknown", "Symbolic link; destination is not a cleanup candidate."
		elif str(path) in active_text or (rid in active_text and recording):
			category, reason = "active", "Required by an active job."
		elif str(path) in recovery_text or (rid in recovery_text and recording):
			category, reason = "recovery", "Required by a resumable job or recovery journal."
		elif recording:
			category, reason = "recovery", "Source material can support resumable imports or integrity recovery."
		if recording and category == "recovery" and rid not in recovery_text and rid not in active_text:
			entry = manifest.get("catalog", {}).get("entries", {}).get(recording.get("library_id"), {})
			finished = Path(entry["location"]) if entry.get("location") else None
			verified = entry.get("fingerprints") or {}
			# Only known downloader sidecar descriptions, never audio or recovery sources.
			video_id = str((recording.get("youtube") or {}).get("video_id") or "")
			if video_id and path.name in (f"source--{video_id}.info.json", f"source--{video_id}.description") and verified.get("audio_verified") and finished and finished.is_file() and file_sha256(finished) == verified.get("sha256"):
				category, reason, eligible = "completed", "Unreferenced downloader description; final catalog audio is verified.", True
		if path.suffix.casefold() == ".mp3":
			category, reason = "finished_audio", "MP3 files must be reconciled as library audio, never staging cleanup."
		rows.append({"path": str(path), "size_bytes": path.lstat().st_size, "category": category, "reason": reason, "cleanup_eligible": eligible,
			"sha256": file_sha256(path) if not path.is_symlink() else None})
	return {"files": rows, "total_bytes": sum(row["size_bytes"] for row in rows)}


def trash_reviewed_file(paths: ManagedPaths, row: dict[str, Any], manifest: dict[str, Any], *, trash: Any) -> str:
	"""Recheck lifecycle and bytes immediately before an explicitly requested cleanup."""
	current = next((item for item in review_working_files(paths, manifest)["files"] if item["path"] == row["path"]), None)
	if not current or current != row or not current["cleanup_eligible"]:
		raise ValueError("Working file is not a current, confirmed cleanup candidate.")
	return str(trash(Path(row["path"])))


def cleanup(paths: ManagedPaths, row: dict[str, Any]) -> str:
	from crate_music_importer.ipod_import.dependency_lock import dependency_lock
	from crate_music_importer.ipod_import.manifest import load_manifest
	from crate_music_importer.ipod_import.unification import trash_destination
	import uuid
	def trash(source: Path) -> Path:
		target = trash_destination(source, "crate-working-" + uuid.uuid4().hex + "-" + source.name)
		source.rename(target)
		return target
	with dependency_lock(exclusive=True):
		return trash_reviewed_file(paths, row, load_manifest(paths), trash=trash)
