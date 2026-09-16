"""Disposable searchable snapshot of Music.app used by fast previews."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from crate_music_importer.ipod_import.identity import clean_release_labels, normalize_recording_title, normalize_text
from crate_music_importer.ipod_import.manifest import ManagedPaths


MUSIC_CACHE_SCHEMA_VERSION = 2
MUSIC_CACHE_MAX_AGE = timedelta(hours=24)


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
	"""Keep only Music metadata needed for lookup and identity checks."""
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
	}


def _validate_cache(cache: Any, paths: ManagedPaths) -> dict[str, Any]:
	if not isinstance(cache, dict) or cache.get("schema_version") != MUSIC_CACHE_SCHEMA_VERSION:
		raise MusicCacheUnavailableError("The Music cache is missing or uses an obsolete format.")
	if cache.get("managed_root") != str(paths.root) or not isinstance(cache.get("tracks"), dict):
		raise MusicCacheUnavailableError("The Music cache is invalid for this managed library.")
	try:
		scanned_at = datetime.fromisoformat(str(cache.get("scanned_at") or ""))
		if scanned_at.tzinfo is None:
			scanned_at = scanned_at.replace(tzinfo=timezone.utc)
	except ValueError as exc:
		raise MusicCacheUnavailableError("The Music cache has no valid scan timestamp.") from exc
	if datetime.now(timezone.utc) - scanned_at.astimezone(timezone.utc) > MUSIC_CACHE_MAX_AGE:
		raise MusicCacheUnavailableError("The Music cache is older than its refresh window.")
	for persistent_id, track in cache["tracks"].items():
		if not persistent_id or not isinstance(track, dict) or track.get("persistent_id") != persistent_id:
			raise MusicCacheUnavailableError("The Music cache contains an invalid track entry.")
	return cache


def build_full_cache(paths: ManagedPaths, tracks: list[dict[str, Any]]) -> dict[str, Any]:
	indexed: dict[str, dict[str, Any]] = {}
	for raw_track in tracks:
		track = normalize_cache_track(raw_track)
		persistent_id = track["persistent_id"]
		if not persistent_id:
			raise MusicCacheError("Music returned a track without a persistent ID; the previous cache was preserved.")
		if persistent_id in indexed:
			raise MusicCacheError(f"Music returned duplicate persistent ID {persistent_id}; the previous cache was preserved.")
		indexed[persistent_id] = track
	return {"schema_version": MUSIC_CACHE_SCHEMA_VERSION, "managed_root": str(paths.root), "scanned_at": _now(), "tracks": indexed}


def save_music_cache(paths: ManagedPaths, cache: dict[str, Any]) -> None:
	cache = _validate_cache(cache, paths)
	paths.state_dir.mkdir(parents=True, exist_ok=True)
	temporary = None
	try:
		with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=paths.state_dir, prefix="music-library-cache-", suffix=".tmp", delete=False) as handle:
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


def refresh_music_cache(paths: ManagedPaths, tracks: list[dict[str, Any]]) -> dict[str, Any]:
	"""Replace the complete disposable snapshot with one Music.app scan."""
	cache = build_full_cache(paths, tracks)
	save_music_cache(paths, cache)
	return cache


def load_music_cache(paths: ManagedPaths, *, rebuild: Callable[[], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
	"""Load the cache, rebuilding it from Music.app whenever it cannot be trusted."""
	try:
		with paths.music_cache.open("r", encoding="utf-8") as handle:
			return _validate_cache(json.load(handle), paths)
	except (OSError, ValueError, MusicCacheUnavailableError):
		if rebuild is None:
			from crate_music_importer.ipod_import.music import scan_music_library

			rebuild = scan_music_library
		try:
			return refresh_music_cache(paths, rebuild())
		except Exception as exc:
			raise MusicCacheUnavailableError(f"The Music cache could not be rebuilt from Music.app: {exc}") from exc


def music_cache_tracks(cache: dict[str, Any]) -> list[dict[str, Any]]:
	return [copy.deepcopy(track) for track in cache["tracks"].values()]


def upsert_music_cache_track(paths: ManagedPaths, track: dict[str, Any]) -> None:
	cache = load_music_cache(paths)
	entry = normalize_cache_track(track)
	persistent_id = entry["persistent_id"]
	if not persistent_id:
		raise MusicCacheError("A Music persistent ID is required for a cache update.")
	cache["tracks"][persistent_id] = entry
	cache["scanned_at"] = _now()
	save_music_cache(paths, cache)


def remove_music_cache_tracks(paths: ManagedPaths, persistent_ids: set[str]) -> int:
	requested = {str(value) for value in persistent_ids if str(value)}
	if not requested:
		return 0
	cache = load_music_cache(paths)
	removed = sum(1 for persistent_id in requested if cache["tracks"].pop(persistent_id, None) is not None)
	cache["scanned_at"] = _now()
	save_music_cache(paths, cache)
	return removed


def _same_title(left: Any, right: Any) -> bool:
	return normalize_recording_title(left) == normalize_recording_title(right)


def _same_artist(left: Any, right: Any) -> bool:
	return normalize_text(left) == normalize_text(right)


def _same_album(left: Any, right: Any) -> bool:
	return normalize_text(clean_release_labels(left)) == normalize_text(clean_release_labels(right))


def validate_exact_track(expected: dict[str, Any], actual: dict[str, Any] | None) -> tuple[bool, str]:
	if actual is None:
		return False, "the persistent ID no longer exists in Music"
	if str(actual.get("persistent_id") or "") != str(expected.get("persistent_id") or ""):
		return False, "Music returned a different persistent ID"
	for field, comparison in (("title", _same_title), ("artist", _same_artist), ("album", _same_album)):
		if expected.get(field) and not comparison(expected.get(field), actual.get(field)):
			return False, f"the Music {field} changed"
	wanted_duration = _number(expected.get("duration_s"))
	actual_duration = _number(actual.get("duration_s"))
	if wanted_duration and (not actual_duration or abs(wanted_duration - actual_duration) > 4.0):
		return False, "the Music duration changed materially"
	return True, ""
