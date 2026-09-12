"""Persistent Music-library index used by fast previews and targeted validation."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from crate_music_importer.ipod_import.identity import clean_release_labels, normalize_recording_title, normalize_text
from crate_music_importer.ipod_import.manifest import ManagedPaths


MUSIC_CACHE_SCHEMA_VERSION = 1
MUSIC_CACHE_MIGRATION_VERSION = 1
CACHE_REFRESH_INSTRUCTION = "Run or refresh Library Health before previewing or importing."


class MusicCacheError(RuntimeError):
	pass


class MusicCacheUnavailableError(MusicCacheError):
	pass


def _now() -> str:
	return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _integer(value: Any) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return 0


def _number(value: Any) -> float:
	try:
		return float(value)
	except (TypeError, ValueError):
		return 0.0


def normalize_cache_track(track: dict[str, Any]) -> dict[str, Any]:
	"""Keep only stable Music identity and matching fields."""
	return {
		"persistent_id": str(track.get("persistent_id") or ""),
		"database_id": str(track.get("database_id") or ""),
		"title": str(track.get("title") or ""),
		"artist": str(track.get("artist") or track.get("artists") or ""),
		"album": str(track.get("album") or ""),
		"album_artist": str(track.get("album_artist") or ""),
		"duration_s": _number(track.get("duration_s")),
		"location": str(track.get("location")) if track.get("location") else None,
		"comment": str(track.get("comment") or ""),
		"track_no": _integer(track.get("track_no")),
		"track_total": _integer(track.get("track_total")),
		"disc_no": _integer(track.get("disc_no")),
		"disc_total": _integer(track.get("disc_total")),
		"compilation": bool(track.get("compilation", False)),
		"validation_required": bool(track.get("validation_required", False)),
		"migration_source": track.get("migration_source"),
		"stale": bool(track.get("stale", False)),
		"stale_reason": track.get("stale_reason"),
		"stale_at": track.get("stale_at"),
	}


def _manifest_music_references(manifest: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
	"""Build conservative, validation-required entries without inventing Music metadata."""
	entries: dict[str, dict[str, Any]] = {}
	incomplete: list[str] = []
	for recording in (manifest.get("recordings") or {}).values():
		if not isinstance(recording, dict):
			continue
		music = recording.get("music") or {}
		active = recording.get("active_reference") or {}
		persistent_id = str(music.get("persistent_id") or active.get("persistent_id") or "")
		if not persistent_id:
			continue
		metadata = recording.get("source_metadata") or {}
		entry = normalize_cache_track({
			"persistent_id": persistent_id,
			"database_id": music.get("database_id"),
			"title": metadata.get("title"),
			"artist": metadata.get("artists"),
			"duration_s": _number(metadata.get("duration_ms")) / 1000.0,
			"location": music.get("location"),
			"validation_required": True,
			"migration_source": "manifest",
		})
		entries[persistent_id] = entry
		if not all((entry["database_id"], entry["title"], entry["artist"], entry["duration_s"])):
			incomplete.append(persistent_id)
	return entries, sorted(incomplete)


def new_partial_cache(paths: ManagedPaths, manifest: dict[str, Any]) -> dict[str, Any]:
	"""Represent legacy manifest IDs without claiming that Music was fully scanned."""
	now = _now()
	tracks, incomplete = _manifest_music_references(manifest)
	return {
		"schema_version": MUSIC_CACHE_SCHEMA_VERSION,
		"managed_root": str(paths.root),
		"created_at": now,
		"updated_at": now,
		"last_full_scan_at": None,
		"total_cached_tracks": len(tracks),
		"initial_scan_completed": False,
		"migration": {
			"version": MUSIC_CACHE_MIGRATION_VERSION,
			"manifest_version": manifest.get("version"),
			"manifest_reference_count": len(tracks),
			"incomplete_persistent_ids": incomplete,
		},
		"tracks": tracks,
	}


def _validate_cache(cache: Any, paths: ManagedPaths) -> dict[str, Any]:
	if not isinstance(cache, dict):
		raise MusicCacheUnavailableError(f"The Music library cache is invalid. {CACHE_REFRESH_INSTRUCTION}")
	if cache.get("schema_version") != MUSIC_CACHE_SCHEMA_VERSION:
		raise MusicCacheUnavailableError(f"The Music library cache uses an unsupported format. {CACHE_REFRESH_INSTRUCTION}")
	if cache.get("managed_root") != str(paths.root):
		raise MusicCacheUnavailableError(f"The Music library cache belongs to a different managed library. {CACHE_REFRESH_INSTRUCTION}")
	tracks = cache.get("tracks")
	if not isinstance(tracks, dict):
		raise MusicCacheUnavailableError(f"The Music library cache has no valid track index. {CACHE_REFRESH_INSTRUCTION}")
	for persistent_id, track in tracks.items():
		if not persistent_id or not isinstance(track, dict) or str(track.get("persistent_id") or "") != persistent_id:
			raise MusicCacheUnavailableError(f"The Music library cache contains an invalid track entry. {CACHE_REFRESH_INSTRUCTION}")
	if _integer(cache.get("total_cached_tracks")) != len(tracks):
		raise MusicCacheUnavailableError(f"The Music library cache track count is inconsistent. {CACHE_REFRESH_INSTRUCTION}")
	return cache


def load_music_cache(paths: ManagedPaths, *, require_complete: bool = True) -> dict[str, Any]:
	if not paths.music_cache.is_file():
		raise MusicCacheUnavailableError(f"The Music library cache has not been created. {CACHE_REFRESH_INSTRUCTION}")
	try:
		with paths.music_cache.open("r", encoding="utf-8") as handle:
			cache = _validate_cache(json.load(handle), paths)
	except MusicCacheUnavailableError:
		raise
	except (OSError, ValueError) as exc:
		raise MusicCacheUnavailableError(f"The Music library cache could not be read safely. {CACHE_REFRESH_INSTRUCTION}") from exc
	if require_complete and not cache.get("initial_scan_completed"):
		raise MusicCacheUnavailableError(f"The migrated Music cache is incomplete and cannot be used for matching. {CACHE_REFRESH_INSTRUCTION}")
	return cache


def music_cache_tracks(cache: dict[str, Any]) -> list[dict[str, Any]]:
	return [copy.deepcopy(track) for track in cache["tracks"].values()]


def save_music_cache(paths: ManagedPaths, cache: dict[str, Any]) -> None:
	cache = _validate_cache(cache, paths)
	paths.state_dir.mkdir(parents=True, exist_ok=True)
	temporary = None
	try:
		with tempfile.NamedTemporaryFile(
			"w",
			encoding="utf-8",
			dir=paths.state_dir,
			prefix="music-library-cache-",
			suffix=".tmp",
			delete=False,
		) as handle:
			json.dump(cache, handle, ensure_ascii=False, indent=2, sort_keys=True)
			handle.write("\n")
			handle.flush()
			os.fsync(handle.fileno())
			temporary = handle.name
		os.replace(temporary, paths.music_cache)
		temporary = None
	finally:
		if temporary:
			try:
				os.unlink(temporary)
			except FileNotFoundError:
				pass


def build_full_cache(
	paths: ManagedPaths,
	manifest: dict[str, Any],
	tracks: list[dict[str, Any]],
	*,
	previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
	now = _now()
	indexed: dict[str, dict[str, Any]] = {}
	for raw_track in tracks:
		track = normalize_cache_track(raw_track)
		persistent_id = track["persistent_id"]
		if not persistent_id:
			raise MusicCacheError("Music returned a track without a persistent ID; the previous cache was preserved.")
		if persistent_id in indexed:
			raise MusicCacheError(f"Music returned duplicate persistent ID {persistent_id}; the previous cache was preserved.")
		track.update({"validation_required": False, "migration_source": None, "stale": False, "stale_reason": None, "stale_at": None})
		indexed[persistent_id] = track
	migrated, incomplete = _manifest_music_references(manifest)
	missing_ids = sorted(set(migrated) - set(indexed))
	migration_entries: dict[str, dict[str, Any]] = {}
	for persistent_id in missing_ids:
		entry = copy.deepcopy(migrated[persistent_id])
		entry["stale"] = True
		entry["stale_reason"] = "the saved manifest ID was not present in the authoritative full Music scan"
		entry["stale_at"] = now
		migration_entries[persistent_id] = entry
	return {
		"schema_version": MUSIC_CACHE_SCHEMA_VERSION,
		"managed_root": str(paths.root),
		"created_at": str((previous or {}).get("created_at") or now),
		"updated_at": now,
		"last_full_scan_at": now,
		"total_cached_tracks": len(indexed),
		"initial_scan_completed": True,
		"migration": {
			"version": MUSIC_CACHE_MIGRATION_VERSION,
			"manifest_version": manifest.get("version"),
			"manifest_reference_count": len(migrated),
			"incomplete_persistent_ids": sorted(set(incomplete) & set(missing_ids)),
			"manifest_ids_missing_from_music": missing_ids,
			"entries_requiring_validation": migration_entries,
		},
		"tracks": indexed,
	}


def refresh_music_cache(
	paths: ManagedPaths,
	manifest: dict[str, Any],
	tracks: list[dict[str, Any]],
) -> dict[str, Any]:
	"""Atomically refresh the preview index from an already completed Music scan."""
	previous = None
	if paths.music_cache.is_file():
		try:
			previous = load_music_cache(paths, require_complete=False)
		except MusicCacheUnavailableError:
			previous = None
	cache = build_full_cache(paths, manifest, tracks, previous=previous)
	save_music_cache(paths, cache)
	return cache


def upsert_music_cache_track(paths: ManagedPaths, track: dict[str, Any]) -> None:
	cache = load_music_cache(paths)
	entry = normalize_cache_track(track)
	persistent_id = entry["persistent_id"]
	if not persistent_id:
		raise MusicCacheError("A Music persistent ID is required for an incremental cache update.")
	entry.update({"validation_required": False, "migration_source": None, "stale": False, "stale_reason": None, "stale_at": None})
	cache["tracks"][persistent_id] = entry
	cache["total_cached_tracks"] = len(cache["tracks"])
	cache["updated_at"] = _now()
	save_music_cache(paths, cache)


def remove_music_cache_tracks(paths: ManagedPaths, persistent_ids: set[str]) -> int:
	"""Atomically forget explicitly removed Music items without disturbing the rest of the index."""
	requested = {str(value) for value in persistent_ids if str(value)}
	if not requested:
		return 0
	cache = load_music_cache(paths)
	removed = sum(1 for persistent_id in requested if cache["tracks"].pop(persistent_id, None) is not None)
	migration = cache.get("migration") or {}
	entries = migration.get("entries_requiring_validation")
	if isinstance(entries, dict):
		for persistent_id in requested:
			entries.pop(persistent_id, None)
	for field in ("incomplete_persistent_ids", "manifest_ids_missing_from_music"):
		values = migration.get(field)
		if isinstance(values, list):
			migration[field] = [value for value in values if str(value) not in requested]
	cache["total_cached_tracks"] = len(cache["tracks"])
	cache["updated_at"] = _now()
	save_music_cache(paths, cache)
	return removed


def mark_music_cache_entry_stale(paths: ManagedPaths, persistent_id: str, reason: str) -> None:
	cache = load_music_cache(paths)
	entry = cache["tracks"].get(persistent_id)
	if entry is None:
		return
	entry["stale"] = True
	entry["stale_reason"] = reason
	entry["stale_at"] = _now()
	cache["updated_at"] = _now()
	save_music_cache(paths, cache)


def _same_title(left: Any, right: Any) -> bool:
	return normalize_recording_title(left) == normalize_recording_title(right)


def _same_artist(left: Any, right: Any) -> bool:
	return normalize_text(left) == normalize_text(right)


def _same_album(left: Any, right: Any) -> bool:
	return normalize_text(clean_release_labels(left)) == normalize_text(clean_release_labels(right))


def validate_exact_track(
	cached: dict[str, Any],
	actual: dict[str, Any] | None,
	*,
	require_importer_owned: bool = False,
	recording_id: str | None = None,
	importer_owned_location: str | None = None,
) -> tuple[bool, str]:
	if actual is None:
		return False, "the persistent ID no longer exists in Music"
	if str(actual.get("persistent_id") or "") != str(cached.get("persistent_id") or ""):
		return False, "Music returned a different persistent ID"
	checks = (
		("title", _same_title),
		("artist", _same_artist),
		("album", _same_album),
	)
	for field, comparison in checks:
		if cached.get(field) and not comparison(cached.get(field), actual.get(field)):
			return False, f"the Music {field} changed"
	wanted_duration = _number(cached.get("duration_s"))
	actual_duration = _number(actual.get("duration_s"))
	if wanted_duration and (not actual_duration or abs(wanted_duration - actual_duration) > 4.0):
		return False, "the Music duration changed materially"
	if require_importer_owned:
		marker = f"recording_id={recording_id or ''}"
		actual_location = str(actual.get("location") or "")
		location_matches = bool(importer_owned_location and actual_location == importer_owned_location)
		if not recording_id or (marker not in str(actual.get("comment") or "") and not location_matches):
			return False, "the importer ownership marker is missing or changed"
	return True, ""


def cache_track_for_recording(
	recording: dict[str, Any],
	music_identity: dict[str, Any],
	*,
	album_profile: bool,
) -> dict[str, Any]:
	metadata = recording.get("source_metadata") or {}
	album = recording.get("album_metadata") or {}
	managed = recording.get("managed_file") or {}
	return normalize_cache_track({
		"persistent_id": music_identity.get("persistent_id"),
		"database_id": music_identity.get("database_id"),
		"title": metadata.get("title"),
		"artist": metadata.get("artists"),
		"album": album.get("album") if album_profile else "Playlist Imports",
		"album_artist": album.get("album_artist") if album_profile else "Various Artists",
		# Music reports the duration of the imported file, which can legitimately
		# differ by a few seconds from Spotify after an approved YouTube match.
		"duration_s": _number(managed.get("duration_ms") or metadata.get("duration_ms")) / 1000.0,
		"location": music_identity.get("location"),
		"comment": f"Managed by Crate Music Importer; recording_id={recording['recording_id']}",
		"track_no": album.get("track_no") if album_profile else 0,
		"track_total": album.get("track_total") if album_profile else 0,
		"disc_no": album.get("disc_no") if album_profile else 0,
		"disc_total": album.get("disc_total") if album_profile else 0,
		"compilation": bool(album.get("is_compilation")) if album_profile else True,
	})
