"""Resumable orchestration for preview, managed downloads, and Music apply."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from crate_music_importer.ipod_import.manifest import (
	ManagedPaths,
	clone_manifest,
	file_sha256,
	load_manifest,
	refresh_manual_youtube_overrides,
	save_manifest,
	set_album,
	set_playlist,
	upsert_recording,
	write_playlist_m3u8,
)
from crate_music_importer.ipod_import.identity import clean_release_labels, match_music_track, normalize_text
from crate_music_importer.ipod_import.media import (
	MediaError,
	download_recording,
	extract_embedded_artwork,
	managed_duration_is_valid,
	recover_moved_managed_file,
	retag_managed_recording_artwork,
	retag_managed_recording_as_album,
)
from crate_music_importer.ipod_import.music import (
	MusicAutomationError,
	MusicIndex,
	import_managed_file,
	playlist_status,
	sync_music_playlist,
	update_managed_music_artwork,
	update_managed_music_track,
)
from crate_music_importer.ipod_import.music_cache import cache_track_for_recording, validate_exact_track
from crate_music_importer.ipod_import.youtube import YouTubeError, choose_candidate, search_candidates


ProgressCallback = Callable[[dict[str, Any]], None]
ExactMusicLookup = Callable[[str], dict[str, Any] | None]
CacheTrackUpdater = Callable[[dict[str, Any]], None]
CacheStaleMarker = Callable[[str, str], None]


def _progress(callback: ProgressCallback | None, phase: str, **values: Any) -> None:
	if callback:
		callback({"phase": phase, **values})


@dataclass
class Preview:
	manifest: dict[str, Any]
	playlist_id: str
	counts: dict[str, int]
	rows: list[dict[str, Any]]


@dataclass
class AlbumPreview:
	manifest: dict[str, Any]
	album_id: str
	counts: dict[str, int]
	rows: list[dict[str, Any]]


def _managed_exists(recording: dict[str, Any], paths: ManagedPaths) -> bool:
	managed = recording.get("managed_file") or {}
	return bool(managed.get("relative_path") and (paths.root / managed["relative_path"]).is_file())


def _album_matches(track: dict[str, Any], candidate: dict[str, Any]) -> bool:
	return bool(
		normalize_text(clean_release_labels(track.get("album")))
		and normalize_text(clean_release_labels(track.get("album"))) == normalize_text(clean_release_labels(candidate.get("album")))
	)


def _is_importer_owned_music(recording: dict[str, Any], candidate: dict[str, Any] | None) -> bool:
	comment = str((candidate or {}).get("comment") or "")
	if f"recording_id={recording['recording_id']}" in comment:
		return True
	source = str((recording.get("music") or {}).get("source") or "")
	return source in ("managed_import", "managed_album_import", "managed_album_upgrade")


def _music_reference_is_importer_owned(recording: dict[str, Any]) -> bool:
	source = str((recording.get("music") or {}).get("source") or "")
	return source in ("managed_import", "managed_album_import", "managed_album_upgrade")


def _trusted_managed_location(recording: dict[str, Any], paths: ManagedPaths) -> str | None:
	managed = recording.get("managed_file") or {}
	relative_path = str(managed.get("relative_path") or "")
	if not managed.get("tool_owned") or not relative_path:
		return None
	target = paths.root / relative_path
	if not target.is_file():
		return None
	expected_sha = str(managed.get("sha256") or "")
	if not expected_sha or file_sha256(target) != expected_sha:
		return None
	return str(target)


def _cache_warning(exc: Exception) -> MusicAutomationError:
	return MusicAutomationError(
		"Music changed successfully, but its persistent cache could not be saved. "
		"The Music ID was checkpointed so retrying will not create a duplicate. "
		"Run ‘Rebuild Music Library Cache’ before continuing. " + str(exc)
	)


def _stale_music_reference(
	manifest: dict[str, Any],
	recording: dict[str, Any],
	persistent_id: str,
	reason: str,
	paths: ManagedPaths,
	mark_stale: CacheStaleMarker | None,
) -> None:
	message = (
		f"Cached Music track {persistent_id} is stale because {reason}. "
		"Run ‘Rebuild Music Library Cache’; this track was stopped so a duplicate cannot be created."
	)
	recording["review"] = {
		"kind": "music_cache_stale",
		"message": message,
		"candidates": [],
	}
	recording["last_error"] = message
	save_manifest(paths, manifest)
	if mark_stale:
		try:
			mark_stale(persistent_id, reason)
		except Exception:
			pass
	raise MusicAutomationError(message)


def _expected_track_from_manifest(recording: dict[str, Any], persistent_id: str) -> dict[str, Any]:
	music = dict(recording.get("music") or {})
	music["persistent_id"] = persistent_id
	importer_owned = _music_reference_is_importer_owned(recording) or bool(recording.get("cache_sync_pending"))
	managed = recording.get("managed_file") or {}
	album_profile = importer_owned and managed.get("metadata_profile") == "album"
	expected = cache_track_for_recording(recording, music, album_profile=album_profile)
	if not importer_owned:
		# A manifest records Spotify identity, not the user's chosen Music album/comment.
		expected["album"] = ""
		expected["album_artist"] = ""
		expected["comment"] = ""
	return expected


def _validate_exact_reference(
	manifest: dict[str, Any],
	recording: dict[str, Any],
	persistent_id: str,
	index: MusicIndex,
	paths: ManagedPaths,
	*,
	exact_lookup: ExactMusicLookup,
	cache_updater: CacheTrackUpdater | None,
	mark_stale: CacheStaleMarker | None,
	validated: dict[str, dict[str, Any]],
	require_importer_owned: bool | None = None,
) -> dict[str, Any]:
	if persistent_id in validated:
		return validated[persistent_id]
	trusted_managed_location = _trusted_managed_location(recording, paths)
	importer_owned = _music_reference_is_importer_owned(recording) or bool(recording.get("cache_sync_pending"))
	ownership_required = importer_owned if require_importer_owned is None else require_importer_owned
	stale_entry = index.stale_by_persistent_id.get(persistent_id)
	marker_only_stale = (stale_entry or {}).get("stale_reason") == "the importer ownership marker is missing or changed"
	if stale_entry and not trusted_managed_location and not (marker_only_stale and not ownership_required):
		_stale_music_reference(manifest, recording, persistent_id, "it was already marked stale", paths, mark_stale)
	cached = index.by_persistent_id.get(persistent_id)
	if cached is None:
		cached = stale_entry
	actual = exact_lookup(persistent_id)
	validation_actual = actual
	if (
		actual
		and ownership_required
		and trusted_managed_location
		and not actual.get("location")
		and str((cached or {}).get("location") or "") == trusted_managed_location
	):
		# Music can omit the location for a cloud-managed track. The full cache
		# location remains usable as ownership evidence only while the exact ID and
		# metadata still match and the registered file still matches its SHA.
		validation_actual = dict(actual, location=trusted_managed_location)
	validate_managed_playlist_identity = require_importer_owned is False and importer_owned
	managed = recording.get("managed_file") or {}
	managed_album_transition = bool(
		importer_owned
		and managed.get("metadata_profile") == "album"
		and str((recording.get("music") or {}).get("source") or "") == "managed_import"
		and actual
		and _album_matches({"album": (recording.get("album_metadata") or {}).get("album")}, actual)
	)
	expected = (
		_expected_track_from_manifest(recording, persistent_id)
		if recording.get("cache_sync_pending")
		or cached is None
		or cached.get("stale")
		or validate_managed_playlist_identity
		or managed_album_transition
		else cached
	)
	valid, reason = validate_exact_track(
		expected,
		validation_actual,
		require_importer_owned=ownership_required,
		recording_id=recording.get("recording_id"),
		importer_owned_location=trusted_managed_location,
	)
	if not valid:
		_stale_music_reference(manifest, recording, persistent_id, reason, paths, mark_stale)
	assert actual is not None
	stale_review = (recording.get("review") or {}).get("kind") == "music_cache_stale"
	if recording.get("cache_sync_pending") or cached is None or cached.get("validation_required") or cached.get("stale") or stale_review:
		if cache_updater:
			try:
				cache_updater(actual)
			except Exception as exc:
				raise MusicAutomationError(
					"The Music ID is valid, but the persistent cache could not be reconciled. "
					"Run ‘Rebuild Music Library Cache’ before continuing. " + str(exc)
				) from exc
		recording.pop("cache_sync_pending", None)
		recording.pop("last_error", None)
		if stale_review:
			recording.pop("review", None)
		save_manifest(paths, manifest)
		index.by_persistent_id[persistent_id] = actual
	validated[persistent_id] = actual
	return actual


def _is_different_managed_album(recording: dict[str, Any], album_id: str) -> bool:
	managed = recording.get("managed_file") or {}
	managed_album_id = str(managed.get("spotify_album_id") or "")
	return managed.get("metadata_profile") == "album" and bool(managed_album_id) and managed_album_id != album_id


def _playlist_artwork_needs_update(recording: dict[str, Any], paths: ManagedPaths) -> bool:
	managed = recording.get("managed_file") or {}
	active = recording.get("active_reference") or {}
	cover_url = str(recording.get("source_metadata", {}).get("cover_url") or "")
	return bool(
		active.get("kind") == "managed_file"
		and cover_url
		and managed.get("tool_owned")
		and managed.get("metadata_profile") in (None, "playlist")
		and managed.get("artwork_source_url") != cover_url
		and _managed_exists(recording, paths)
		and managed_duration_is_valid(recording, paths)
	)


def _set_album_release_conflict(
	recording: dict[str, Any],
	*,
	candidates: list[dict[str, Any]] | None = None,
) -> None:
	recording["review"] = {
		"kind": "album_release_conflict",
		"message": "This importer-owned recording is already assigned to a different real album. One Music item cannot carry two album identities, so the tool will not silently move it or create a duplicate.",
		"candidates": candidates or [],
	}


def _preferred_album_candidate(track: dict[str, Any], index: MusicIndex) -> dict[str, Any] | None:
	candidates = [
		candidate
		for candidate in index.candidates(track)
		if candidate.get("persistent_id") and _album_matches(track, candidate)
	]
	match = match_music_track(track, candidates)
	return match.get("candidate") if match.get("status") == "reused" else None


def build_preview(
	playlist: dict[str, Any],
	music_tracks: list[dict[str, Any]],
	manifest: dict[str, Any],
	paths: ManagedPaths,
) -> Preview:
	planned = clone_manifest(manifest)
	index = MusicIndex(music_tracks)
	rows: list[dict[str, Any]] = []
	items: list[dict[str, Any]] = []
	counts: dict[str, int] = {}
	for source_position, track in enumerate(playlist["tracks"], start=1):
		position = int(track.get("position") or source_position)
		key, recording = upsert_recording(planned, track)
		status = "search_required"
		detail = "not found in Music; YouTube search required"
		match_kind = None
		active = recording.get("active_reference") or {}
		music = recording.get("music") or {}
		persistent_id = str(music.get("persistent_id") or active.get("persistent_id") or "")
		if persistent_id and persistent_id in index.stale_by_persistent_id:
			stale_entry = index.stale_by_persistent_id[persistent_id]
			marker_only_stale = stale_entry.get("stale_reason") == "the importer ownership marker is missing or changed"
			if _trusted_managed_location(recording, paths) or (marker_only_stale and not recording.get("artwork_sync_pending")):
				# The exact tool-owned source path is a second durable ownership proof.
				# For playlist-only reuse, an exact ID and recording identity are also
				# sufficient because final apply will not edit or delete the Music item.
				status = "reused_music"
				detail = "saved Music match will be revalidated by exact ID before playlist-only reuse"
				if (recording.get("review") or {}).get("kind") == "music_cache_stale":
					recording.pop("review", None)
					recording.pop("last_error", None)
			else:
				status = "review_music"
				detail = "saved Music cache entry is stale; run Rebuild Music Library Cache"
		elif persistent_id and persistent_id not in index.by_persistent_id:
			status = "review_music"
			detail = "saved Music ID is missing from the cache; run Rebuild Music Library Cache"
		elif _playlist_artwork_needs_update(recording, paths):
			status = "artwork_update"
			detail = "repair the importer-owned MP3 with this track's individual Spotify album artwork"
		elif active.get("kind") == "existing_music" and persistent_id in index.by_persistent_id:
			candidate = index.by_persistent_id[persistent_id]
			status = "reused_music"
			match_kind = "importer_owned" if _is_importer_owned_music(recording, candidate) else "library"
			detail = f"existing {'importer-owned ' if match_kind == 'importer_owned' else ''}Music match {persistent_id}"
		elif persistent_id in index.by_persistent_id and _is_importer_owned_music(recording, index.by_persistent_id[persistent_id]):
			status = "reused_music"
			match_kind = "importer_owned"
			detail = f"existing importer-owned Music match {persistent_id}"
		elif _managed_exists(recording, paths) and managed_duration_is_valid(recording, paths):
			status = "managed_existing"
			detail = "reuse canonical managed MP3"
		elif active.get("kind") == "managed_file":
			status = "ready_to_download"
			detail = "registered managed MP3 failed duration validation; rebuild required"
		else:
			match = index.match(track)
			if match["status"] == "reused":
				candidate = match["candidate"]
				recording["music"] = {
					"source": "existing_library",
					"persistent_id": candidate.get("persistent_id"),
					"database_id": candidate.get("database_id"),
					"location": candidate.get("location"),
					"matched_score": candidate.get("score"),
				}
				recording["active_reference"] = {
					"kind": "existing_music",
					"persistent_id": candidate.get("persistent_id"),
				}
				recording.pop("review", None)
				status = "reused_music"
				match_kind = "importer_owned" if _is_importer_owned_music(recording, candidate) else "library"
				detail = f"confident {'importer-owned ' if match_kind == 'importer_owned' else ''}Music match {candidate.get('persistent_id')}"
			elif match["status"] == "ambiguous":
				manual_youtube = recording.get("youtube") or {}
				if manual_youtube.get("selected_by") == "manual_url" and manual_youtube.get("override_music_ambiguity"):
					recording.pop("review", None)
					status = "ready_to_download"
					detail = "deliberate Raycast YouTube choice overrides previously reviewed Music ambiguity"
				else:
					recording["review"] = {
						"kind": "music_ambiguity",
						"message": "Multiple Music tracks are too close to choose safely. If fingerprinting is required, grant read access to the listed audio locations before doing it.",
						"candidates": match["candidates"],
					}
					status = "review_music"
					detail = "ambiguous Music candidates; no download will occur"
			else:
				if recording.get("youtube") and recording.get("review", {}).get("kind") not in ("music_ambiguity",):
					status = "ready_to_download"
					detail = "Music has no confident match; reviewed YouTube source already recorded"
					recording.pop("review", None)
				else:
					recording.pop("review", None)
		if status == "reused_music" and (recording.get("review") or {}).get("kind") == "music_cache_stale":
			recording.pop("review", None)
			recording.pop("last_error", None)
		counts[status] = counts.get(status, 0) + 1
		row = {
			"position": position,
			"recording_id": key,
			"spotify_id": track.get("sp_id"),
			"title": recording["source_metadata"].get("title") or "",
			"artists": track.get("artists") or "",
			"status": status,
			"detail": detail,
			"match_kind": match_kind,
		}
		rows.append(row)
		items.append({
			"position": position,
			"recording_id": key,
			"spotify_id": track.get("sp_id"),
			"status": status,
		})
	set_playlist(planned, playlist, items)
	return Preview(planned, str(playlist["id"]), counts, rows)


def build_album_preview(
	album: dict[str, Any],
	music_tracks: list[dict[str, Any]],
	manifest: dict[str, Any],
	paths: ManagedPaths,
) -> AlbumPreview:
	planned = clone_manifest(manifest)
	planned.setdefault("albums", {})
	index = MusicIndex(music_tracks)
	rows: list[dict[str, Any]] = []
	items: list[dict[str, Any]] = []
	counts: dict[str, int] = {}
	for source_position, track in enumerate(album["tracks"], start=1):
		position = int(track.get("position") or source_position)
		key, recording = upsert_recording(planned, track, source_type="album")
		status = "search_required"
		detail = "not found in Music; YouTube search required"
		match_kind = None
		active = recording.get("active_reference") or {}
		music = recording.get("music") or {}
		persistent_id = str(music.get("persistent_id") or active.get("persistent_id") or "")
		active_candidate = index.by_persistent_id.get(persistent_id)
		if active_candidate:
			recover_moved_managed_file(recording, active_candidate, paths)
		managed_ready = _managed_exists(recording, paths) and managed_duration_is_valid(recording, paths)
		managed = recording.get("managed_file") or {}
		preferred_album_candidate = _preferred_album_candidate(track, index)
		preferred_id = str((preferred_album_candidate or {}).get("persistent_id") or "")
		if persistent_id and persistent_id in index.stale_by_persistent_id:
			stale_candidate = index.stale_by_persistent_id[persistent_id]
			recovered_path = recover_moved_managed_file(recording, stale_candidate, paths)
			promotion_transition_ready = bool(
				recovered_path
				and managed.get("metadata_profile") == "album"
				and str(managed.get("spotify_album_id") or "") == str(album["id"])
				and str(music.get("source") or "") == "managed_import"
				and str(stale_candidate.get("stale_reason") or "") == "the Music album changed"
			)
			if promotion_transition_ready:
				recording.pop("review", None)
				recording.pop("last_error", None)
				recording.pop("last_error_source", None)
				status = "managed_existing"
				detail = "album promotion is ready for final exact Music validation"
			else:
				status = "review_music"
				detail = "saved Music cache entry is stale; run Rebuild Music Library Cache"
		elif persistent_id and persistent_id not in index.by_persistent_id:
			status = "review_music"
			detail = "saved Music ID is missing from the cache; run Rebuild Music Library Cache"
		elif (
			preferred_album_candidate
			and active_candidate
			and preferred_id != persistent_id
			and _is_importer_owned_music(recording, active_candidate)
			and managed_ready
		):
			recording["review"] = {
				"kind": "album_duplicate_conflict",
				"message": "A proper user-owned album track and an importer-owned Playlist Imports item both exist. The tool will not delete either item automatically; choose the canonical copy before continuing.",
				"candidates": [preferred_album_candidate, active_candidate],
			}
			status = "review_music"
			detail = "proper album track and importer-owned playlist copy both already exist"
		elif preferred_album_candidate:
			recording["music"] = {
				"source": "existing_album_library",
				"persistent_id": preferred_album_candidate.get("persistent_id"),
				"database_id": preferred_album_candidate.get("database_id"),
				"location": preferred_album_candidate.get("location"),
				"matched_score": preferred_album_candidate.get("score"),
			}
			recording["active_reference"] = {
				"kind": "existing_music",
				"persistent_id": preferred_album_candidate.get("persistent_id"),
			}
			if managed.get("tool_owned"):
				managed["retirement_candidate"] = True
			recording.pop("review", None)
			status = "reused_music"
			match_kind = "importer_owned" if _is_importer_owned_music(recording, preferred_album_candidate) else "library"
			detail = f"confident existing album track {preferred_album_candidate.get('persistent_id')}"
		elif active_candidate and _album_matches(track, active_candidate):
			status = "reused_music"
			match_kind = "importer_owned" if _is_importer_owned_music(recording, active_candidate) else "library"
			detail = f"reuse album track in Music ({persistent_id})"
		elif active_candidate and _is_importer_owned_music(recording, active_candidate) and managed_ready:
			if _is_different_managed_album(recording, str(album["id"])):
				_set_album_release_conflict(recording, candidates=[active_candidate])
				status = "review_music"
				detail = "recording is already canonical for a different real album"
			else:
				recording.pop("review", None)
				recording.pop("last_error", None)
				status = "upgrade_managed"
				detail = "upgrade the importer-owned Playlist Imports item to real album metadata"
		elif active_candidate:
			recording["review"] = {
				"kind": "album_conflict",
				"message": "The same recording exists in Music under different album metadata. It is not importer-owned, so the album workflow will neither edit it nor create a silent duplicate.",
				"candidates": [active_candidate],
			}
			status = "review_music"
			detail = "existing user-owned recording belongs to a different album; review required"
		elif managed_ready:
			if managed.get("metadata_profile") == "album" and managed.get("spotify_album_id") == str(album["id"]):
				status = "managed_existing"
				detail = "reuse canonical album-tagged managed MP3"
			elif _is_different_managed_album(recording, str(album["id"])):
				_set_album_release_conflict(recording)
				status = "review_music"
				detail = "recording is already canonical for a different real album"
			else:
				status = "upgrade_managed"
				detail = "upgrade canonical importer-owned MP3 from Playlist Imports to real album metadata"
		elif active.get("kind") == "managed_file":
			status = "ready_to_download"
			detail = "registered managed MP3 failed duration validation; rebuild with album metadata"
		else:
			match = index.match(track)
			if match["status"] == "reused":
				candidate = match["candidate"]
				if _album_matches(track, candidate):
					recording["music"] = {
						"source": "existing_album_library",
						"persistent_id": candidate.get("persistent_id"),
						"database_id": candidate.get("database_id"),
						"location": candidate.get("location"),
						"matched_score": candidate.get("score"),
					}
					recording["active_reference"] = {
						"kind": "existing_music",
						"persistent_id": candidate.get("persistent_id"),
					}
					recording.pop("review", None)
					status = "reused_music"
					match_kind = "importer_owned" if _is_importer_owned_music(recording, candidate) else "library"
					detail = f"confident existing album track {candidate.get('persistent_id')}"
				elif _is_importer_owned_music(recording, candidate) and managed_ready and _is_different_managed_album(recording, str(album["id"])):
					_set_album_release_conflict(recording, candidates=match["candidates"])
					status = "review_music"
					detail = "recording is already canonical for a different real album"
				elif _is_importer_owned_music(recording, candidate) and managed_ready:
					recording["music"] = {
						"source": "managed_import",
						"persistent_id": candidate.get("persistent_id"),
						"database_id": candidate.get("database_id"),
						"location": candidate.get("location"),
					}
					recording.pop("review", None)
					recording.pop("last_error", None)
					status = "upgrade_managed"
					detail = "upgrade the importer-owned Music item in place; no second library item"
				else:
					recording["review"] = {
						"kind": "album_conflict",
						"message": "A confident recording match exists under different user-owned album metadata. Choose which release should be canonical before importing this album.",
						"candidates": match["candidates"],
					}
					status = "review_music"
					detail = "same recording exists under different user-owned album metadata"
			elif match["status"] == "ambiguous":
				recording["review"] = {
					"kind": "music_ambiguity",
					"message": "Multiple Music tracks are too close to choose safely for this album.",
					"candidates": match["candidates"],
				}
				status = "review_music"
				detail = "ambiguous Music candidates; no download will occur"
			else:
				recording.pop("review", None)
		counts[status] = counts.get(status, 0) + 1
		rows.append({
			"position": position,
			"recording_id": key,
			"spotify_id": track.get("sp_id"),
			"title": recording["source_metadata"].get("title") or "",
			"artists": track.get("artists") or "",
			"track_no": int(track.get("track_no") or position),
			"disc_no": int(track.get("disc_no") or 1),
			"status": status,
			"detail": detail,
			"match_kind": match_kind,
		})
		items.append({
			"position": position,
			"recording_id": key,
			"spotify_id": track.get("sp_id"),
			"track_no": int(track.get("track_no") or position),
			"disc_no": int(track.get("disc_no") or 1),
			"status": status,
		})
	set_album(planned, album, items)
	return AlbumPreview(planned, str(album["id"]), counts, rows)


def build_album_metadata_preview(
	album: dict[str, Any],
	manifest: dict[str, Any],
) -> AlbumPreview:
	"""Build the Raycast album track list without touching Music or managed media."""
	planned = clone_manifest(manifest)
	planned.setdefault("albums", {})
	rows: list[dict[str, Any]] = []
	items: list[dict[str, Any]] = []
	counts: dict[str, int] = {}
	for source_position, track in enumerate(album["tracks"], start=1):
		position = int(track.get("position") or source_position)
		key, recording = upsert_recording(planned, track, source_type="album")
		review = recording.get("review") or {}
		active = recording.get("active_reference") or {}
		youtube = recording.get("youtube") or {}
		music = recording.get("music") or {}
		if review.get("kind") in ("youtube_missing", "youtube_ambiguity"):
			status = "matches_need_approval" if review.get("candidates") else "no_youtube_matches"
			detail = (
				"Saved YouTube candidates need your approval"
				if review.get("candidates")
				else "The previous search found no usable YouTube matches"
			)
		elif review:
			status = "needs_approval"
			detail = str(review.get("message") or "This track needs approval")
		elif recording.get("last_error"):
			status = "failed"
			detail = str(recording["last_error"])
		elif active.get("kind") == "existing_music":
			status = "reused_music"
			detail = "Saved Music match; it will be checked once at the final Music stage"
		elif active.get("kind") == "managed_file" and music.get("persistent_id"):
			managed = recording.get("managed_file") or {}
			if managed.get("metadata_profile") != "album" or str(managed.get("spotify_album_id") or "") != str(album["id"]):
				status = "upgrade_managed"
				detail = "Importer-owned Playlist Imports track is ready for in-place album promotion"
			else:
				status = "complete"
				detail = "Previously downloaded and added to Music"
		elif active.get("kind") == "managed_file":
			status = "downloaded"
			detail = "Previously downloaded; waiting to be added to Music"
		elif youtube.get("url"):
			status = "youtube_match_found"
			detail = "Saved YouTube match is ready to download"
		else:
			status = "not_started"
			detail = "YouTube matching starts after you queue the album"
		counts[status] = counts.get(status, 0) + 1
		rows.append({
			"position": position,
			"recording_id": key,
			"spotify_id": track.get("sp_id"),
			"title": recording["source_metadata"].get("title") or "",
			"artists": track.get("artists") or "",
			"track_no": int(track.get("track_no") or position),
			"disc_no": int(track.get("disc_no") or 1),
			"status": status,
			"detail": detail,
		})
		items.append({
			"position": position,
			"recording_id": key,
			"spotify_id": track.get("sp_id"),
			"track_no": int(track.get("track_no") or position),
			"disc_no": int(track.get("disc_no") or 1),
			"status": status,
		})
	set_album(planned, album, items)
	return AlbumPreview(planned, str(album["id"]), counts, rows)


def execute_import(
	preview: Preview,
	paths: ManagedPaths,
	*,
	on_output: Callable[[str], None] | None = None,
	on_progress: ProgressCallback | None = None,
	search: Callable[[dict[str, Any]], list[dict[str, Any]]] = search_candidates,
	download: Callable[..., dict[str, Any]] = download_recording,
	retag_artwork: Callable[..., dict[str, Any]] = retag_managed_recording_artwork,
) -> dict[str, Any]:
	manifest = preview.manifest
	playlist = manifest["playlists"][preview.playlist_id]
	if not playlist.get("complete"):
		raise ValueError(playlist.get("warning") or "Spotify did not expose the complete ordered playlist.")
	paths.create()
	save_manifest(paths, manifest)
	processed: set[str] = set()
	for item in sorted(playlist["items"], key=lambda value: int(value["position"])):
		key = item["recording_id"]
		if key in processed:
			continue
		processed.add(key)
		refresh_manual_youtube_overrides(paths, manifest)
		recording = manifest["recordings"][key]
		if item["status"] in ("reused_music", "managed_existing", "review_music"):
			_progress(
				on_progress,
				"needs_attention" if item["status"] == "review_music" else "reused",
				recording_id=key,
				position=int(item["position"]),
				title=recording["source_metadata"].get("title") or "",
				artists=recording["source_metadata"].get("artists") or "",
			)
			continue
		try:
			if item["status"] == "artwork_update":
				_progress(on_progress, "tagging", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
				recording["managed_file"] = retag_artwork(recording, paths, on_output=on_output)
				if (recording.get("music") or {}).get("persistent_id"):
					recording["artwork_sync_pending"] = True
				recording.pop("last_error", None)
				recording.pop("last_error_source", None)
				_progress(on_progress, "downloaded", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
				continue
			if not recording.get("youtube") or recording.get("review", {}).get("kind") == "youtube_ambiguity":
				_progress(on_progress, "searching_youtube", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
				if on_output:
					on_output(f"Searching YouTube: {recording['source_metadata']['artists']} - {recording['source_metadata']['title']}")
				candidates = search(recording["source_metadata"])
				refresh_manual_youtube_overrides(paths, manifest)
				if recording.get("youtube") and not recording.get("review"):
					choice = {"status": "matched", "candidate": recording["youtube"], "candidates": []}
				else:
					choice = choose_candidate(recording["source_metadata"], candidates)
				if choice["status"] != "matched":
					attention_phase = "matches_need_approval" if choice["candidates"] else "no_youtube_matches"
					recording["review"] = {
						"kind": "youtube_missing",
						"message": "No verified YouTube recording met the automatic identity and duration requirements. Choose a recording after review.",
						"candidates": choice["candidates"],
						"search_summary": choice.get("search_summary", {}),
					}
					recording["last_error"] = "No verified YouTube recording met the automatic identity and duration requirements."
					if on_output:
						on_output(f"Needs review: {recording['source_metadata']['title']} ({choice['status']})")
					_progress(on_progress, attention_phase, recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
					save_manifest(paths, manifest)
					if not recording.get("youtube") or recording.get("review"):
						continue
					choice = {"status": "matched", "candidate": recording["youtube"], "candidates": []}
				candidate = choice["candidate"]
				recording["youtube"] = (
					dict(candidate)
					if str(candidate.get("selected_by") or "").startswith("manual")
					else dict(candidate, selected_by="automatic_high_confidence")
				)
				recording.pop("review", None)
				_progress(on_progress, "youtube_match_found", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			else:
				_progress(on_progress, "youtube_match_found", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			if on_output:
				on_output(f"Downloading {recording['source_metadata']['artists']} - {recording['source_metadata']['title']}")
			_progress(on_progress, "downloading", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			recording["managed_file"] = download(recording, paths, on_output=on_output)
			recording["active_reference"] = {
				"kind": "managed_file",
				"relative_path": recording["managed_file"]["relative_path"],
			}
			recording.pop("last_error", None)
			recording.pop("last_error_source", None)
			_progress(on_progress, "downloaded", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
		except (MediaError, YouTubeError, OSError) as exc:
			recording["last_error"] = str(exc)
			recording["last_error_source"] = {"type": "playlist", "id": preview.playlist_id}
			if on_output:
				on_output(f"Resumable failure: {recording['source_metadata']['title']}: {exc}")
			_progress(on_progress, "failed", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "", error=str(exc))
		finally:
			save_manifest(paths, manifest)
	for item in playlist["items"]:
		recording = manifest["recordings"][item["recording_id"]]
		active = recording.get("active_reference") or {}
		music = recording.get("music") or {}
		if recording.get("last_error"):
			item["status"] = "failed"
		elif item.get("status") == "reused_music" and music.get("persistent_id"):
			# Album imports intentionally keep their managed-file reference so album
			# metadata remains canonical, even after Music has copied the source file.
			# A playlist preview already validated the persistent Music ID, so do not
			# turn that safe reuse into a download failure when the source MP3 is gone.
			item["status"] = "reused_music"
		elif active.get("kind") == "existing_music":
			item["status"] = "reused_music"
		elif active.get("kind") == "managed_file" and _managed_exists(recording, paths):
			item["status"] = "managed_ready"
		elif recording.get("review"):
			item["status"] = "review_required"
		else:
			item["status"] = "failed"
	m3u = write_playlist_m3u8(manifest, preview.playlist_id, paths)
	save_manifest(paths, manifest)
	return {"manifest": manifest, "playlist": playlist, "m3u8": str(m3u)}


def execute_album_import(
	preview: AlbumPreview,
	paths: ManagedPaths,
	*,
	on_output: Callable[[str], None] | None = None,
	on_progress: ProgressCallback | None = None,
	search: Callable[[dict[str, Any]], list[dict[str, Any]]] = search_candidates,
	download: Callable[..., dict[str, Any]] = download_recording,
	retag: Callable[..., dict[str, Any]] = retag_managed_recording_as_album,
) -> dict[str, Any]:
	manifest = preview.manifest
	album = manifest["albums"][preview.album_id]
	if not album.get("complete"):
		raise ValueError(album.get("warning") or "Spotify did not expose the complete ordered album.")
	paths.create()
	save_manifest(paths, manifest)
	processed: set[str] = set()
	for item in sorted(album["items"], key=lambda value: int(value["position"])):
		key = item["recording_id"]
		if key in processed:
			continue
		processed.add(key)
		refresh_manual_youtube_overrides(paths, manifest)
		recording = manifest["recordings"][key]
		if item["status"] in ("reused_music", "managed_existing", "review_music"):
			_progress(on_progress, "needs_attention" if item["status"] == "review_music" else "reused", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			continue
		try:
			if item["status"] == "upgrade_managed":
				_progress(on_progress, "tagging", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
				recording["managed_file"] = retag(recording, paths, on_output=on_output)
				recording["active_reference"] = {
					"kind": "managed_file",
					"relative_path": recording["managed_file"]["relative_path"],
				}
				recording.pop("last_error", None)
				recording.pop("last_error_source", None)
				_progress(on_progress, "downloaded", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
				continue
			if not recording.get("youtube") or recording.get("review", {}).get("kind") == "youtube_ambiguity":
				_progress(on_progress, "searching_youtube", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
				if on_output:
					on_output(f"Searching YouTube: {recording['source_metadata']['artists']} - {recording['source_metadata']['title']}")
				candidates = search(recording["source_metadata"])
				refresh_manual_youtube_overrides(paths, manifest)
				if recording.get("youtube") and not recording.get("review"):
					choice = {"status": "matched", "candidate": recording["youtube"], "candidates": []}
				else:
					choice = choose_candidate(recording["source_metadata"], candidates)
				if choice["status"] != "matched":
					attention_phase = "matches_need_approval" if choice["candidates"] else "no_youtube_matches"
					recording["review"] = {
						"kind": "youtube_missing",
						"message": "No verified YouTube recording met the automatic identity and duration requirements. Choose a verified recording in Import Activity & Problems.",
						"candidates": choice["candidates"],
						"search_summary": choice.get("search_summary", {}),
					}
					recording["last_error"] = "No verified YouTube recording met the automatic identity and duration requirements."
					if on_output:
						on_output(f"Needs review: {recording['source_metadata']['title']} ({choice['status']})")
					_progress(on_progress, attention_phase, recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
					save_manifest(paths, manifest)
					if not recording.get("youtube") or recording.get("review"):
						continue
					choice = {"status": "matched", "candidate": recording["youtube"], "candidates": []}
				candidate = choice["candidate"]
				recording["youtube"] = (
					dict(candidate)
					if str(candidate.get("selected_by") or "").startswith("manual")
					else dict(candidate, selected_by="automatic_high_confidence")
				)
				recording.pop("review", None)
				_progress(on_progress, "youtube_match_found", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			else:
				_progress(on_progress, "youtube_match_found", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			if on_output:
				on_output(f"Downloading album track: {recording['source_metadata']['artists']} - {recording['source_metadata']['title']}")
			_progress(on_progress, "downloading", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			recording["managed_file"] = download(recording, paths, on_output=on_output)
			recording["active_reference"] = {
				"kind": "managed_file",
				"relative_path": recording["managed_file"]["relative_path"],
			}
			recording.pop("last_error", None)
			recording.pop("last_error_source", None)
			_progress(on_progress, "downloaded", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
		except (MediaError, YouTubeError, OSError) as exc:
			recording["last_error"] = str(exc)
			recording["last_error_source"] = {"type": "album", "id": preview.album_id}
			if on_output:
				on_output(f"Resumable failure: {recording['source_metadata']['title']}: {exc}")
			_progress(on_progress, "failed", recording_id=key, position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "", error=str(exc))
		finally:
			save_manifest(paths, manifest)
	for item in album["items"]:
		recording = manifest["recordings"][item["recording_id"]]
		active = recording.get("active_reference") or {}
		managed = recording.get("managed_file") or {}
		if active.get("kind") == "existing_music":
			item["status"] = "reused_music"
		elif active.get("kind") == "managed_file" and _managed_exists(recording, paths) and managed.get("metadata_profile") == "album":
			item["status"] = "managed_ready"
		elif recording.get("review"):
			item["status"] = "review_required"
		else:
			item["status"] = "failed"
	save_manifest(paths, manifest)
	return {"manifest": manifest, "album": album}


def apply_album_to_music(
	manifest: dict[str, Any],
	album_id: str,
	paths: ManagedPaths,
	music_tracks: list[dict[str, Any]],
	*,
	on_progress: ProgressCallback | None = None,
	exact_lookup: ExactMusicLookup | None = None,
	cache_updater: CacheTrackUpdater | None = None,
	mark_stale: CacheStaleMarker | None = None,
) -> dict[str, Any]:
	album = manifest.get("albums", {})[album_id]
	index = MusicIndex(music_tracks)
	exact_lookup = exact_lookup or (lambda persistent_id: index.by_persistent_id.get(persistent_id))
	validated: dict[str, dict[str, Any]] = {}
	artwork_paths: dict[str, Path] = {}
	for item in sorted(album["items"], key=lambda value: int(value["position"])):
		recording = manifest["recordings"][item["recording_id"]]
		if (recording.get("review") or recording.get("last_error")) and not recording.get("cache_sync_pending"):
			raise MusicAutomationError(
				f"Album track {item['position']} is unresolved ({recording['source_metadata']['title']}). Resolve it before changing Music."
			)
		active = recording.get("active_reference") or {}
		music = recording.get("music") or {}
		persistent_id = str(music.get("persistent_id") or active.get("persistent_id") or "")
		if persistent_id:
			_progress(on_progress, "checking_music_ids", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			candidate = _validate_exact_reference(
				manifest,
				recording,
				persistent_id,
				index,
				paths,
				exact_lookup=exact_lookup,
				cache_updater=cache_updater,
				mark_stale=mark_stale,
				validated=validated,
			)
			if active.get("kind") == "existing_music" and not _album_matches({"album": (recording.get("album_metadata") or {}).get("album")}, candidate):
				_stale_music_reference(manifest, recording, persistent_id, "its album metadata no longer matches the planned album", paths, mark_stale)
		if active.get("kind") == "existing_music" and persistent_id:
			continue
		managed = recording.get("managed_file") or {}
		path = paths.root / str(managed.get("relative_path") or "")
		if active.get("kind") != "managed_file" or not path.is_file() or managed.get("metadata_profile") != "album":
			raise MusicAutomationError(
				f"Album track {item['position']} is not ready ({recording['source_metadata']['title']}). Run the album download again first."
			)
		artwork_paths[recording["recording_id"]] = extract_embedded_artwork(
			path,
			paths.staging / recording["recording_id"] / "music-album-artwork.jpg",
		)
	new_imports = 0
	recovered_imports = 0
	updated_tracks = 0
	reused_tracks = 0
	for item in sorted(album["items"], key=lambda value: int(value["position"])):
		recording = manifest["recordings"][item["recording_id"]]
		_progress(on_progress, "adding_to_music", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
		active = recording.get("active_reference") or {}
		music = recording.get("music") or {}
		persistent_id = str(music.get("persistent_id") or active.get("persistent_id") or "")
		candidate = validated.get(persistent_id)
		if active.get("kind") == "existing_music" and candidate:
			reused_tracks += 1
			_progress(on_progress, "complete", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			continue
		managed = recording.get("managed_file") or {}
		path = paths.root / str(managed.get("relative_path") or "")
		recorded_music = bool(music.get("persistent_id"))
		imported = candidate or index.by_recording_comment.get(recording["recording_id"])
		was_recovered = imported is not None and not recorded_music
		if was_recovered:
			recovered_id = str(imported.get("persistent_id") or "")
			actual = exact_lookup(recovered_id) if recovered_id else None
			valid, reason = validate_exact_track(imported, actual, require_importer_owned=True, recording_id=recording["recording_id"])
			if not valid:
				_stale_music_reference(manifest, recording, recovered_id, reason, paths, mark_stale)
			imported = actual
		if imported is None:
			if recording.get("music_import_pending"):
				raise MusicAutomationError(
					f"A previous Music import may have succeeded for {recording['source_metadata']['title']}. "
					"Run ‘Rebuild Music Library Cache’ before retrying; refusing to create a possible duplicate."
				)
			recording["music_import_pending"] = {
				"relative_path": managed["relative_path"],
				"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
			}
			save_manifest(paths, manifest)
			imported = import_managed_file(path)
			new_imports += 1
		elif was_recovered:
			recovered_imports += 1
		assert imported is not None
		persistent_id = str(imported.get("persistent_id") or "")
		if not persistent_id:
			raise MusicAutomationError("Music did not return a persistent ID for an imported album track.")
		# A newly imported album MP3 already contains the final album tags, comment,
		# and artwork. Asking Music to rewrite it immediately can fail with -50 while
		# Cloud Library still marks the track as "Need upload". Only an existing
		# importer-owned playlist track needs an in-place Music metadata refresh.
		if recorded_music:
			update_managed_music_track(persistent_id, recording, artwork_paths[recording["recording_id"]])
			updated_tracks += 1
		recording["music"] = {
			"source": "managed_album_upgrade" if recorded_music else "managed_album_import",
			"persistent_id": persistent_id,
			"database_id": imported.get("database_id"),
			"location": imported.get("location"),
		}
		recording["active_reference"] = {
			"kind": "managed_file",
			"relative_path": managed["relative_path"],
		}
		recording.pop("music_import_pending", None)
		recording["cache_sync_pending"] = {
			"persistent_id": persistent_id,
			"action": "album_metadata_update" if recorded_music else "album_import",
			"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
		}
		save_manifest(paths, manifest)
		cached_track = cache_track_for_recording(recording, imported, album_profile=True)
		if cache_updater:
			try:
				cache_updater(cached_track)
			except Exception as exc:
				recording["last_error"] = str(_cache_warning(exc))
				save_manifest(paths, manifest)
				raise _cache_warning(exc) from exc
		recording.pop("cache_sync_pending", None)
		recording.pop("last_error", None)
		index.by_persistent_id[persistent_id] = imported
		index.by_recording_comment[recording["recording_id"]] = cached_track
		save_manifest(paths, manifest)
		_progress(on_progress, "complete", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
	return {
		"track_count": len(album["items"]),
		"new_imports": new_imports,
		"recovered_imports": recovered_imports,
		"updated_tracks": updated_tracks,
		"reused_tracks": reused_tracks,
	}


def apply_to_music(
	manifest: dict[str, Any],
	playlist_id: str,
	paths: ManagedPaths,
	music_tracks: list[dict[str, Any]],
	*,
	on_progress: ProgressCallback | None = None,
	exact_lookup: ExactMusicLookup | None = None,
	cache_updater: CacheTrackUpdater | None = None,
	mark_stale: CacheStaleMarker | None = None,
) -> dict[str, Any]:
	playlist = manifest["playlists"][playlist_id]
	known_playlist_id = playlist.get("music_playlist_persistent_id")
	status, found_id = playlist_status(playlist["name"], known_playlist_id)
	if status == "COLLISION":
		raise MusicAutomationError(
			f"A Music playlist named '{playlist['name']}' already exists with persistent ID {found_id}; it is not recorded as importer-owned. Rename it or deliberately adopt it outside this tool."
		)
	if status == "MISSING":
		raise MusicAutomationError("The previously importer-owned Music playlist is missing. Refusing to replace another playlist by name.")
	index = MusicIndex(music_tracks)
	exact_lookup = exact_lookup or (lambda persistent_id: index.by_persistent_id.get(persistent_id))
	validated: dict[str, dict[str, Any]] = {}
	recovered: dict[str, dict[str, Any]] = {}
	for item in sorted(playlist["items"], key=lambda value: int(value["position"])):
		recording = manifest["recordings"][item["recording_id"]]
		active = recording.get("active_reference") or {}
		music = recording.get("music") or {}
		persistent_id = str(music.get("persistent_id") or active.get("persistent_id") or "")
		if persistent_id:
			_progress(on_progress, "checking_music_ids", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			_validate_exact_reference(
				manifest,
				recording,
				persistent_id,
				index,
				paths,
				exact_lookup=exact_lookup,
				cache_updater=cache_updater,
				mark_stale=mark_stale,
				validated=validated,
				require_importer_owned=bool(recording.get("artwork_sync_pending")),
			)
			continue
		if active.get("kind") != "managed_file":
			raise MusicAutomationError(
				f"Playlist position {item['position']} is unresolved ({recording['source_metadata']['title']}). Resolve it before applying Music changes."
			)
		candidate = index.by_recording_comment.get(recording["recording_id"])
		if candidate:
			recovered_id = str(candidate.get("persistent_id") or "")
			_progress(on_progress, "checking_music_ids", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			actual = exact_lookup(recovered_id) if recovered_id else None
			valid, reason = validate_exact_track(candidate, actual, require_importer_owned=True, recording_id=recording["recording_id"])
			if not valid:
				_stale_music_reference(manifest, recording, recovered_id, reason, paths, mark_stale)
			assert actual is not None
			recovered[recording["recording_id"]] = actual
		elif recording.get("music_import_pending"):
			message = (
				f"A previous Music import may have succeeded for {recording['source_metadata']['title']}. "
				"Run ‘Rebuild Music Library Cache’ before retrying; refusing to create a possible duplicate."
			)
			recording["review"] = {"kind": "music_cache_stale", "message": message, "candidates": []}
			recording["last_error"] = message
			save_manifest(paths, manifest)
			raise MusicAutomationError(message)
	ordered_ids: list[str] = []
	new_imports = 0
	for item in sorted(playlist["items"], key=lambda value: int(value["position"])):
		recording = manifest["recordings"][item["recording_id"]]
		_progress(on_progress, "adding_to_music", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
		active = recording.get("active_reference") or {}
		music = recording.get("music") or {}
		persistent_id = str(music.get("persistent_id") or active.get("persistent_id") or "")
		if persistent_id and persistent_id in validated:
			if recording.get("artwork_sync_pending"):
				managed = recording.get("managed_file") or {}
				path = paths.root / str(managed.get("relative_path") or "")
				artwork_path = extract_embedded_artwork(
					path,
					paths.staging / recording["recording_id"] / "music-playlist-artwork.jpg",
				)
				update_managed_music_artwork(persistent_id, recording["recording_id"], artwork_path)
				recording.pop("artwork_sync_pending", None)
				save_manifest(paths, manifest)
			ordered_ids.append(persistent_id)
			_progress(on_progress, "complete", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			continue
		if active.get("kind") != "managed_file":
			raise MusicAutomationError(
				f"Playlist position {item['position']} is unresolved ({recording['source_metadata']['title']}). Resolve it before applying Music changes."
		)
		already_imported = recovered.get(recording["recording_id"])
		if already_imported:
			imported = already_imported
		else:
			managed = recording.get("managed_file") or {}
			path = paths.root / str(managed.get("relative_path") or "")
			recording["music_import_pending"] = {
				"relative_path": managed.get("relative_path"),
				"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
			}
			save_manifest(paths, manifest)
			imported = import_managed_file(path)
			new_imports += 1
		recording["music"] = {
			"source": "managed_import",
			"persistent_id": imported.get("persistent_id"),
			"database_id": imported.get("database_id"),
			"location": imported.get("location"),
		}
		persistent_id = str(imported.get("persistent_id") or "")
		if not persistent_id:
			raise MusicAutomationError("Music did not return a persistent ID for an imported managed MP3.")
		recording.pop("music_import_pending", None)
		recording["cache_sync_pending"] = {
			"persistent_id": persistent_id,
			"action": "playlist_import",
			"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
		}
		save_manifest(paths, manifest)
		cached_track = cache_track_for_recording(recording, imported, album_profile=False)
		if cache_updater:
			try:
				cache_updater(cached_track)
			except Exception as exc:
				recording["last_error"] = str(_cache_warning(exc))
				save_manifest(paths, manifest)
				raise _cache_warning(exc) from exc
		recording.pop("cache_sync_pending", None)
		recording.pop("artwork_sync_pending", None)
		recording.pop("last_error", None)
		ordered_ids.append(persistent_id)
		validated[persistent_id] = cached_track
		index.by_persistent_id[persistent_id] = cached_track
		index.by_recording_comment[recording["recording_id"]] = cached_track
		save_manifest(paths, manifest)
		_progress(on_progress, "complete", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
	playlist_pid = sync_music_playlist(playlist["name"], known_playlist_id, ordered_ids)
	playlist["music_playlist_persistent_id"] = playlist_pid
	write_playlist_m3u8(manifest, playlist_id, paths)
	save_manifest(paths, manifest)
	return {"playlist_persistent_id": playlist_pid, "new_imports": new_imports, "track_count": len(ordered_ids)}


def promote_recording(
	manifest: dict[str, Any],
	recording_id: str,
	music_persistent_id: str,
	music_tracks: list[dict[str, Any]],
	paths: ManagedPaths,
) -> dict[str, Any]:
	if recording_id not in manifest["recordings"]:
		raise ValueError(f"Unknown recording ID: {recording_id}")
	index = MusicIndex(music_tracks)
	candidate = index.by_persistent_id.get(music_persistent_id)
	if not candidate:
		raise ValueError("The requested Music persistent ID is not present in the current Music library scan.")
	recording = manifest["recordings"][recording_id]
	match = index.match(recording["source_metadata"])
	matching_ids = {str(item.get("persistent_id") or "") for item in match.get("candidates", []) if float(item.get("score") or 0) >= 0.92}
	if music_persistent_id not in matching_ids:
		raise ValueError("The requested Music track is not a confident metadata/duration/version match; promotion was refused.")
	recording["music"] = {
		"source": "promoted_album_copy",
		"persistent_id": candidate.get("persistent_id"),
		"database_id": candidate.get("database_id"),
		"location": candidate.get("location"),
	}
	recording["active_reference"] = {"kind": "existing_music", "persistent_id": music_persistent_id}
	recording["promotion"]["superseded_by_music_persistent_id"] = music_persistent_id
	recording["promotion"]["tool_owned_loose_file_retirement_candidate"] = bool((recording.get("managed_file") or {}).get("tool_owned"))
	recording.pop("review", None)
	recording.pop("last_error", None)
	if recording.get("managed_file"):
		recording["managed_file"]["retirement_candidate"] = True
	for playlist_id in recording.get("playlist_memberships", {}):
		write_playlist_m3u8(manifest, playlist_id, paths)
	save_manifest(paths, manifest)
	return recording
