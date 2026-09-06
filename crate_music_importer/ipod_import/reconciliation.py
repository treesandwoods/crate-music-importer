"""Auditable, checkpointed reconciliation of local Music.app files into the managed root."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from crate_music_importer.ipod_import.identity import normalized_artists, normalize_recording_title, version_markers
from crate_music_importer.ipod_import.manifest import ManagedPaths, file_sha256, safe_name


from crate_music_importer.ipod_import.config import configured_path

YT_ALBUMS_ROOT = configured_path("legacyAlbumsDirectory", "CRATE_LEGACY_ALBUMS_ROOT", Path.home() / "Music/YT Albums")
MUSIC_LIBRARY_PACKAGE = configured_path("musicLibraryPackage", "CRATE_MUSIC_LIBRARY_PACKAGE", Path.home() / "Music/Music/Music Library.musiclibrary")
PREVIOUS_LIBRARIES_ROOT = configured_path("previousLibrariesDirectory", "CRATE_PREVIOUS_LIBRARIES_ROOT", MUSIC_LIBRARY_PACKAGE.parent / "Previous Libraries.localized")
RECONCILIATION_VERSION = 1


class ReconciliationError(RuntimeError):
	pass


def _now() -> str:
	return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _timestamp() -> str:
	return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.stem + "-", suffix=".tmp", delete=False) as handle:
		json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
		handle.write("\n")
		temporary = Path(handle.name)
	os.replace(temporary, path)


def reconciliation_root(paths: ManagedPaths) -> Path:
	return paths.state_dir / "reconciliation"


def audit_path(paths: ManagedPaths, audit_id: str) -> Path:
	return reconciliation_root(paths) / "audits" / f"{audit_id}.json"


def journal_path(paths: ManagedPaths, audit_id: str) -> Path:
	return reconciliation_root(paths) / "journals" / f"{audit_id}.json"


def _tag_value(tags: Any, key: str) -> str:
	frame = tags.get(key)
	if not frame:
		return ""
	text = getattr(frame, "text", None)
	if not text:
		return ""
	return str(text[0] or "")


def id3_snapshot(path: Path) -> dict[str, str]:
	"""Return a deliberately small, read-only tag snapshot for audit evidence."""
	try:
		from mutagen.id3 import ID3, ID3NoHeaderError
	except ImportError:
		return {}
	try:
		tags = ID3(path)
	except (ID3NoHeaderError, OSError):
		return {}
	return {
		"title": _tag_value(tags, "TIT2"),
		"artist": _tag_value(tags, "TPE1"),
		"album": _tag_value(tags, "TALB"),
		"album_artist": _tag_value(tags, "TPE2"),
		"track": _tag_value(tags, "TRCK"),
		"disc": _tag_value(tags, "TPOS"),
	}


def _source_group(path: Path, paths: ManagedPaths) -> str:
	try:
		path.relative_to(YT_ALBUMS_ROOT)
		return "yt_albums"
	except ValueError:
		pass
	try:
		path.relative_to(paths.root)
		return "managed_root"
	except ValueError:
		return "other"


def _semantic_key(track: dict[str, Any]) -> tuple[str, tuple[str, ...], tuple[str, ...], int]:
	duration_ms = int(round(float(track.get("duration_s") or 0) * 1000))
	return (
		normalize_recording_title(track.get("title")),
		normalized_artists(track.get("artist")),
		version_markers(track.get("title")),
		int(round(duration_ms / 2000.0) * 2000) if duration_ms else 0,
	)


def _legacy_relative_path(track: dict[str, Any], sha256: str) -> str:
	label = safe_name(f"{track.get('artist') or 'Unknown Artist'} - {track.get('title') or 'Untitled'}", limit=100)
	return f"tracks/{sha256[:2]}/legacy_{sha256[:20]}--{label}.mp3"


def _manifest_references(manifest: dict[str, Any]) -> dict[str, list[str]]:
	references: dict[str, list[str]] = defaultdict(list)
	for recording_id, recording in (manifest.get("recordings") or {}).items():
		music = recording.get("music") or {}
		persistent_id = str(music.get("persistent_id") or (recording.get("active_reference") or {}).get("persistent_id") or "")
		if persistent_id:
			references[persistent_id].append(recording_id)
	return dict(references)


def build_library_audit(paths: ManagedPaths, manifest: dict[str, Any], music_tracks: list[dict[str, Any]]) -> dict[str, Any]:
	"""Hash and classify the current Music file tracks without changing Music or media."""
	seen_persistent_ids: set[str] = set()
	records: list[dict[str, Any]] = []
	references = _manifest_references(manifest)
	for music in music_tracks:
		persistent_id = str(music.get("persistent_id") or "")
		if not persistent_id or persistent_id in seen_persistent_ids:
			raise ReconciliationError("Music scan did not return unique persistent IDs.")
		seen_persistent_ids.add(persistent_id)
		location = str(music.get("location") or "")
		if not location:
			raise ReconciliationError(f"Music track {persistent_id} has no local file location.")
		file_path = Path(location)
		if not file_path.is_file():
			raise ReconciliationError(f"Music track {persistent_id} points to a missing file: {file_path}")
		if file_path.suffix.casefold() != ".mp3":
			raise ReconciliationError(f"Music track {persistent_id} is not an MP3 and is outside this migration: {file_path}")
		sha256 = file_sha256(file_path)
		records.append({
			"persistent_id": persistent_id,
			"database_id": str(music.get("database_id") or ""),
			"source_path": str(file_path),
			"source_group": _source_group(file_path, paths),
			"size_bytes": file_path.stat().st_size,
			"sha256": sha256,
			"title": str(music.get("title") or ""),
			"artist": str(music.get("artist") or ""),
			"album": str(music.get("album") or ""),
			"album_artist": str(music.get("album_artist") or ""),
			"duration_s": float(music.get("duration_s") or 0),
			"comment": str(music.get("comment") or ""),
			"id3": id3_snapshot(file_path),
			"manifest_recording_ids": references.get(persistent_id, []),
		})
	by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
	by_identity: dict[tuple[str, tuple[str, ...], tuple[str, ...], int], list[dict[str, Any]]] = defaultdict(list)
	for record in records:
		by_hash[record["sha256"]].append(record)
		by_identity[_semantic_key(record)].append(record)
	exact_duplicates = [
		{"sha256": sha256, "persistent_ids": [record["persistent_id"] for record in group], "paths": [record["source_path"] for record in group]}
		for sha256, group in sorted(by_hash.items()) if len(group) > 1
	]
	semantic_candidates = [
		{
			"identity": {"title": key[0], "artists": list(key[1]), "versions": list(key[2]), "duration_bucket_ms": key[3]},
			"persistent_ids": [record["persistent_id"] for record in group],
			"hashes": sorted({record["sha256"] for record in group}),
		}
		for key, group in sorted(by_identity.items(), key=lambda item: item[0])
		if len(group) > 1 and len({record["sha256"] for record in group}) > 1
	]
	groups: dict[str, int] = defaultdict(int)
	for record in records:
		groups[record["source_group"]] += 1
	return {
		"version": RECONCILIATION_VERSION,
		"audit_id": f"library-{_timestamp()}",
		"created_at": _now(),
		"managed_root": str(paths.root),
		"track_count": len(records),
		"source_groups": dict(sorted(groups.items())),
		"tracks": records,
		"exact_duplicates": exact_duplicates,
		"semantic_candidates": semantic_candidates,
	}


def save_library_audit(paths: ManagedPaths, audit: dict[str, Any]) -> Path:
	target = audit_path(paths, str(audit["audit_id"]))
	_atomic_json(target, audit)
	return target


def load_library_audit(paths: ManagedPaths, audit_id: str) -> dict[str, Any]:
	target = audit_path(paths, audit_id)
	try:
		with target.open("r", encoding="utf-8") as handle:
			audit = json.load(handle)
	except OSError as exc:
		raise ReconciliationError(f"Library audit is unavailable: {audit_id}") from exc
	if audit.get("version") != RECONCILIATION_VERSION or audit.get("audit_id") != audit_id:
		raise ReconciliationError("Library audit has an unsupported format.")
	return audit


def assert_audit_current(audit: dict[str, Any], music_tracks: list[dict[str, Any]]) -> None:
	current = {str(track.get("persistent_id") or ""): str(track.get("location") or "") for track in music_tracks}
	planned = {str(track["persistent_id"]): str(track["source_path"]) for track in audit.get("tracks") or []}
	if current != planned:
		raise ReconciliationError("Music locations changed since this audit. Run library-audit again; no media was moved.")
	for track in audit.get("tracks") or []:
		path = Path(track["source_path"])
		if not path.is_file() or file_sha256(path) != track["sha256"]:
			raise ReconciliationError(f"Source file changed since this audit: {path}. Run library-audit again.")


def _assert_resume_current(audit: dict[str, Any], journal: dict[str, Any], music_tracks: list[dict[str, Any]]) -> None:
	current = {str(track.get("persistent_id") or ""): Path(str(track.get("location") or "")) for track in music_tracks}
	for track in audit.get("tracks") or []:
		persistent_id = str(track["persistent_id"])
		entry = (journal.get("tracks") or {}).get(persistent_id) or {}
		expected = Path(str(entry.get("target_path") or track["source_path"])) if entry.get("relinked") else Path(track["source_path"])
		if current.get(persistent_id) != expected:
			raise ReconciliationError(f"Music location changed while reconciliation was paused: {persistent_id}.")
		if not expected.is_file() or file_sha256(expected) != track["sha256"]:
			raise ReconciliationError(f"Checkpointed file changed while reconciliation was paused: {expected}.")


def _backup_music_library(audit_id: str) -> Path:
	if not MUSIC_LIBRARY_PACKAGE.is_dir():
		raise ReconciliationError(f"Music library package is missing: {MUSIC_LIBRARY_PACKAGE}")
	PREVIOUS_LIBRARIES_ROOT.mkdir(parents=True, exist_ok=True)
	target = PREVIOUS_LIBRARIES_ROOT / f"Music Library [pre-unification {audit_id}].musiclibrary"
	if target.exists():
		return target
	shutil.copytree(MUSIC_LIBRARY_PACKAGE, target, copy_function=shutil.copy2)
	return target


def _target_for(track: dict[str, Any], paths: ManagedPaths) -> Path:
	source = Path(track["source_path"])
	if track["source_group"] == "managed_root":
		try:
			relative = source.relative_to(paths.root)
		except ValueError as exc:
			raise ReconciliationError(f"Managed source escaped the managed root: {source}") from exc
		return paths.root / relative
	return paths.root / _legacy_relative_path(track, track["sha256"])


def _copy_verified(source: Path, target: Path, sha256: str) -> None:
	if target.exists():
		if not target.is_file() or file_sha256(target) != sha256:
			raise ReconciliationError(f"Refusing to overwrite a nonmatching target: {target}")
		return
	target.parent.mkdir(parents=True, exist_ok=True)
	temporary = target.with_name(target.name + ".reconcile.partial")
	try:
		shutil.copy2(source, temporary)
		if file_sha256(temporary) != sha256:
			raise ReconciliationError(f"Copied file hash does not match source: {source}")
		os.replace(temporary, target)
	finally:
		if temporary.exists():
			temporary.unlink()


def _trash_path(source: Path) -> Path:
	trash = Path.home() / ".Trash"
	trash.mkdir(exist_ok=True)
	candidate = trash / source.name
	index = 2
	while candidate.exists():
		candidate = trash / f"{source.stem} {index}{source.suffix}"
		index += 1
	os.replace(source, candidate)
	return candidate


def _journal(paths: ManagedPaths, audit_id: str, backup: Path) -> dict[str, Any]:
	target = journal_path(paths, audit_id)
	if target.exists():
		with target.open("r", encoding="utf-8") as handle:
			return json.load(handle)
	return {
		"version": RECONCILIATION_VERSION,
		"audit_id": audit_id,
		"created_at": _now(),
		"backup": str(backup),
		"tracks": {},
		"state": "running",
	}


def _save_journal(paths: ManagedPaths, audit_id: str, journal: dict[str, Any]) -> None:
	journal["updated_at"] = _now()
	_atomic_json(journal_path(paths, audit_id), journal)


def _prune_legacy_directories() -> None:
	if not YT_ALBUMS_ROOT.exists():
		return
	for candidate in sorted(YT_ALBUMS_ROOT.rglob(".DS_Store"), reverse=True):
		candidate.unlink(missing_ok=True)
	for candidate in sorted((path for path in YT_ALBUMS_ROOT.rglob("*") if path.is_dir()), key=lambda item: len(item.parts), reverse=True):
		try:
			candidate.rmdir()
		except OSError:
			pass
	try:
		YT_ALBUMS_ROOT.rmdir()
	except OSError:
		pass


def _ensure_space(audit: dict[str, Any], paths: ManagedPaths) -> None:
	copy_bytes = sum(int(track["size_bytes"]) for track in audit.get("tracks") or [] if track.get("source_group") == "yt_albums")
	available = shutil.disk_usage(paths.root).free
	if available < int(copy_bytes * 1.05):
		raise ReconciliationError(f"Insufficient free space for a reversible migration: need {copy_bytes:,} bytes, have {available:,}.")


def migrate_library(
	paths: ManagedPaths,
	audit: dict[str, Any],
	music_tracks: list[dict[str, Any]],
	*,
	relink: Callable[[str, Path], dict[str, Any]],
	relink_batch: Callable[[dict[str, Path]], dict[str, Path]] | None = None,
	trash: Callable[[Path], Path] = _trash_path,
) -> dict[str, Any]:
	"""Copy, relink, verify, and only then retire every external YT source file."""
	existing_journal = journal_path(paths, str(audit["audit_id"]))
	if existing_journal.exists():
		with existing_journal.open("r", encoding="utf-8") as handle:
			journal = json.load(handle)
		_assert_resume_current(audit, journal, music_tracks)
	else:
		assert_audit_current(audit, music_tracks)
	if audit.get("source_groups", {}).get("other") or audit.get("source_groups", {}).get("missing"):
		raise ReconciliationError("Audit includes files outside the two approved roots.")
	_ensure_space(audit, paths)
	backup = Path(str(journal.get("backup"))) if existing_journal.exists() else _backup_music_library(str(audit["audit_id"]))
	journal = _journal(paths, str(audit["audit_id"]), backup)
	if journal.get("state") == "complete":
		return journal
	pending_relinks: dict[str, Path] = {}
	for track in audit.get("tracks") or []:
		persistent_id = str(track["persistent_id"])
		entry = journal["tracks"].setdefault(persistent_id, {"source_path": track["source_path"], "sha256": track["sha256"]})
		target = _target_for(track, paths)
		entry["target_path"] = str(target)
		if not entry.get("copied"):
			_copy_verified(Path(track["source_path"]), target, track["sha256"])
			entry["copied"] = True
			_save_journal(paths, str(audit["audit_id"]), journal)
		if not entry.get("relinked") and track["source_group"] == "managed_root" and target == Path(track["source_path"]):
			entry["relinked"] = True
			_save_journal(paths, str(audit["audit_id"]), journal)
		elif not entry.get("relinked"):
			pending_relinks[persistent_id] = target
	if pending_relinks:
		if relink_batch:
			batch_result = relink_batch(pending_relinks)
			for persistent_id, target in pending_relinks.items():
				if batch_result.get(persistent_id) != target:
					raise ReconciliationError(f"Music did not retain persistent ID/location for {persistent_id}; source was not retired.")
				journal["tracks"][persistent_id]["relinked"] = True
				_save_journal(paths, str(audit["audit_id"]), journal)
		else:
			for persistent_id, target in pending_relinks.items():
				result = relink(persistent_id, target)
				if result.get("persistent_id") != persistent_id or Path(str(result.get("location") or "")) != target:
					raise ReconciliationError(f"Music did not retain persistent ID/location for {persistent_id}; source was not retired.")
				journal["tracks"][persistent_id]["relinked"] = True
				_save_journal(paths, str(audit["audit_id"]), journal)
	for track in audit.get("tracks") or []:
		persistent_id = str(track["persistent_id"])
		entry = journal["tracks"][persistent_id]
		target = Path(entry["target_path"])
		if file_sha256(target) != track["sha256"]:
			raise ReconciliationError(f"Target changed after relinking Music: {target}")
		entry["verified"] = True
		_save_journal(paths, str(audit["audit_id"]), journal)
	retired_since_checkpoint = 0
	for track in audit.get("tracks") or []:
		if track["source_group"] != "yt_albums":
			continue
		entry = journal["tracks"][str(track["persistent_id"])]
		if not entry.get("retired"):
			entry["trash_path"] = str(trash(Path(track["source_path"])))
			entry["retired"] = True
			retired_since_checkpoint += 1
			if retired_since_checkpoint >= 25:
				_save_journal(paths, str(audit["audit_id"]), journal)
				retired_since_checkpoint = 0
	if retired_since_checkpoint:
		_save_journal(paths, str(audit["audit_id"]), journal)
	_prune_legacy_directories()
	journal["state"] = "complete"
	_save_journal(paths, str(audit["audit_id"]), journal)
	return journal


def verify_library_migration(paths: ManagedPaths, audit: dict[str, Any], journal: dict[str, Any], music_tracks: list[dict[str, Any]]) -> None:
	"""Require the final Music scan and filesystem state to match the journal exactly."""
	current = {str(track.get("persistent_id") or ""): Path(str(track.get("location") or "")) for track in music_tracks}
	planned = {str(track["persistent_id"]): track for track in audit.get("tracks") or []}
	if set(current) != set(planned):
		raise ReconciliationError("Music persistent IDs differ from the audited library after migration.")
	for persistent_id, track in planned.items():
		entry = (journal.get("tracks") or {}).get(persistent_id) or {}
		target = Path(str(entry.get("target_path") or ""))
		if not target.is_file() or current[persistent_id] != target:
			raise ReconciliationError(f"Music location does not match the verified target for {persistent_id}.")
		if file_sha256(target) != track["sha256"]:
			raise ReconciliationError(f"Target hash changed after migration: {target}")
		if track["source_group"] == "yt_albums" and Path(track["source_path"]).exists():
			raise ReconciliationError(f"Legacy YT source was not retired: {track['source_path']}")
	manifest = load_manifest_for_validation(paths)
	for recording in (manifest.get("recordings") or {}).values():
		managed = recording.get("managed_file") or {}
		relative = managed.get("relative_path")
		if relative and not (paths.root / relative).is_file():
			raise ReconciliationError(f"Manifest references a missing managed file: {relative}")
		active = recording.get("active_reference") or {}
		if active.get("kind") == "existing_music":
			persistent_id = str((recording.get("music") or {}).get("persistent_id") or active.get("persistent_id") or "")
			location = current.get(persistent_id)
			if not persistent_id or not location or not location.is_file():
				raise ReconciliationError("Manifest existing-Music reference does not resolve in Music.")
			try:
				location.relative_to(paths.root)
			except ValueError as exc:
				raise ReconciliationError("Manifest existing-Music reference escaped the managed root.") from exc
	_verify_generated_playlists(paths, manifest)
	if YT_ALBUMS_ROOT.exists() and any(YT_ALBUMS_ROOT.rglob("*.mp3")):
		raise ReconciliationError("MP3 files remain under YT Albums after migration.")


def load_manifest_for_validation(paths: ManagedPaths) -> dict[str, Any]:
	"""Avoid a module cycle while retaining the manifest validation in final acceptance."""
	from crate_music_importer.ipod_import.manifest import load_manifest
	return load_manifest(paths)


def _recording_playlist_ids(manifest: dict[str, Any], recording_id: str) -> set[str]:
	return {
		str(playlist_id)
		for playlist_id, playlist in (manifest.get("playlists") or {}).items()
		if any(str(item.get("recording_id") or "") == recording_id for item in (playlist.get("items") or []))
	}


def _under_managed_root(path: Path, paths: ManagedPaths) -> bool:
	try:
		path.relative_to(paths.root)
		return True
	except ValueError:
		return False


def reconcile_missing_manifest_references(paths: ManagedPaths, music_tracks: list[dict[str, Any]]) -> dict[str, int]:
	"""Retire stale importer file references without touching Music or audio media.

	A managed-file reference is authoritative only while its file exists.  Older
	manifests can retain records from earlier, independently deleted downloads;
	keeping those paths active makes generated M3U files misleading.  This moves
	the old bookkeeping into reconciliation history, marks the record unavailable,
	and rewrites only affected tool-owned M3Us.  A live persistent ID is retained
	only when it resolves to a local file beneath the managed root.
	"""
	from crate_music_importer.ipod_import.manifest import load_manifest, save_manifest, write_playlist_m3u8

	manifest = load_manifest(paths)
	by_persistent_id = {str(track.get("persistent_id") or ""): track for track in music_tracks}
	affected_playlists: set[str] = set()
	resolved_to_music = 0
	marked_unavailable = 0
	for recording_id, recording in (manifest.get("recordings") or {}).items():
		managed = recording.get("managed_file") or {}
		relative = str(managed.get("relative_path") or "")
		if not relative or (paths.root / relative).is_file():
			continue
		music = recording.get("music") or {}
		active = recording.get("active_reference") or {}
		persistent_id = str(music.get("persistent_id") or active.get("persistent_id") or "")
		live = by_persistent_id.get(persistent_id)
		history = recording.setdefault("reconciliation", {}).setdefault("unavailable_managed_files", [])
		history.append({"relative_path": relative, "managed_file": managed, "recorded_at": _now()})
		recording["managed_file"] = None
		if live:
			location = Path(str(live.get("location") or ""))
			if location.is_file() and _under_managed_root(location, paths):
				updated_music = dict(music)
				updated_music["persistent_id"] = persistent_id
				updated_music["database_id"] = str(live.get("database_id") or updated_music.get("database_id") or "")
				updated_music["location"] = str(location)
				recording["music"] = updated_music
				recording["active_reference"] = {"kind": "existing_music", "persistent_id": persistent_id}
				resolved_to_music += 1
			else:
				live = None
		if not live:
			recording["active_reference"] = {
				"kind": "unavailable_historical_reference",
				"persistent_id": persistent_id or None,
				"reason": "managed file and Music persistent ID are no longer present",
			}
			marked_unavailable += 1
		recording["updated_at"] = _now()
		affected_playlists.update(_recording_playlist_ids(manifest, recording_id))
	if affected_playlists:
		save_manifest(paths, manifest)
		for playlist_id in sorted(affected_playlists):
			write_playlist_m3u8(manifest, playlist_id, paths)
		save_manifest(paths, manifest)
	elif resolved_to_music or marked_unavailable:
		save_manifest(paths, manifest)
	return {
		"resolved_to_music": resolved_to_music,
		"marked_unavailable": marked_unavailable,
		"rewritten_playlists": len(affected_playlists),
	}


def _verify_generated_playlists(paths: ManagedPaths, manifest: dict[str, Any]) -> None:
	"""Every non-comment line in a generated M3U must name a live managed file."""
	for playlist_id, playlist in (manifest.get("playlists") or {}).items():
		if not playlist.get("m3u8_tool_owned"):
			continue
		target = paths.root / str(playlist.get("m3u8_relative_path") or "")
		if not target.is_file():
			raise ReconciliationError(f"Generated playlist is missing: {target}")
		for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
			if not line or line.startswith("#"):
				continue
			location = Path(line)
			if not location.is_file() or not _under_managed_root(location, paths):
				raise ReconciliationError(f"Generated playlist {playlist_id} references an unavailable file: {line}")


def reconciliation_status(paths: ManagedPaths) -> dict[str, Any]:
	root = reconciliation_root(paths)
	audits = sorted(path.stem for path in (root / "audits").glob("*.json")) if (root / "audits").is_dir() else []
	journals: list[dict[str, Any]] = []
	for path in sorted((root / "journals").glob("*.json")) if (root / "journals").is_dir() else []:
		with path.open("r", encoding="utf-8") as handle:
			value = json.load(handle)
		journals.append({"audit_id": value.get("audit_id"), "state": value.get("state"), "updated_at": value.get("updated_at")})
	return {"audits": audits, "journals": journals}
