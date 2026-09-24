"""Resumable orchestration for preview, managed downloads, and Music apply."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from crate_music_importer.ipod_import.manifest import (
	ManagedPaths,
	clone_manifest,
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
	album_track_position_match,
	bind_recording_to_music,
	MusicAutomationError,
	MusicIndex,
	import_managed_file,
	music_binding_id,
	playlist_status,
	reconcile_recording_music,
	sync_music_playlist,
	update_managed_music_artwork,
	update_managed_music_track,
)
from crate_music_importer.ipod_import.music_cache import validate_exact_track
from crate_music_importer.ipod_import.youtube import YouTubeError, choose_candidate, search_candidates


ProgressCallback = Callable[[dict[str, Any]], None]
ExactMusicLookup = Callable[[str], dict[str, Any] | None]
CacheTrackUpdater = Callable[[dict[str, Any]], None]
CacheTrackRemover = Callable[[set[str]], int]
MusicTrackVerifier = Callable[[list[str]], dict[str, dict[str, Any]]]


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
	return bool(managed.get("managed_by_crate") and managed.get("relative_path") and (paths.root / managed["relative_path"]).is_file())


def _album_matches(track: dict[str, Any], candidate: dict[str, Any]) -> bool:
	return bool(
		normalize_text(clean_release_labels(track.get("album")))
		and normalize_text(clean_release_labels(track.get("album"))) == normalize_text(clean_release_labels(candidate.get("album")))
	)


def _mislabelled_album_matches(album: dict[str, Any], music_tracks: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
	"""Find a whole album whose album title was saved in Music's album-artist field."""
	requested = normalize_text(clean_release_labels(album.get("name")))
	source_tracks = album.get("tracks") or []
	if not requested or len(source_tracks) < 3:
		return {}
	groups: dict[str, list[dict[str, Any]]] = {}
	for candidate in music_tracks:
		actual_album = normalize_text(clean_release_labels(candidate.get("album")))
		if (
			candidate.get("persistent_id")
			and actual_album and actual_album != requested
			and normalize_text(clean_release_labels(candidate.get("album_artist"))) == requested
			and normalize_text(clean_release_labels(candidate.get("artist"))) == requested
		):
			groups.setdefault(actual_album, []).append(candidate)
	qualified: list[dict[int, dict[str, Any]]] = []
	for candidates in groups.values():
		if len(candidates) != len(source_tracks):
			continue
		by_position: dict[tuple[int, int], list[dict[str, Any]]] = {}
		for candidate in candidates:
			position = (int(candidate.get("disc_no") or 1), int(candidate.get("track_no") or 0))
			by_position.setdefault(position, []).append(candidate)
		matches: dict[int, dict[str, Any]] = {}
		strong_titles = 0
		for ordinal, track in enumerate(source_tracks):
			position = (int(track.get("disc_no") or 1), int(track.get("track_no") or ordinal + 1))
			at_position = by_position.get(position, [])
			if len(at_position) != 1:
				break
			candidate = at_position[0]
			matched, strong_title = album_track_position_match(
				{**track, "album": track.get("album") or album.get("name"), "track_no": position[1]},
				candidate,
				misplaced_album=True,
			)
			if not matched:
				break
			strong_titles += strong_title
			matches[ordinal] = candidate
		if len(matches) == len(source_tracks) and strong_titles >= max(3, len(source_tracks) * 4 // 5):
			qualified.append(matches)
	return qualified[0] if len(qualified) == 1 else {}


def _expected_track_from_manifest(recording: dict[str, Any], persistent_id: str) -> dict[str, Any]:
	metadata = recording.get("source_metadata") or {}
	return {
		"persistent_id": persistent_id,
		"title": metadata.get("title"),
		"artist": metadata.get("artists"),
		"duration_s": int(metadata.get("duration_ms") or 0) / 1000.0,
	}


def _validate_exact_reference(
	manifest: dict[str, Any],
	recording: dict[str, Any],
	persistent_id: str,
	index: MusicIndex,
	paths: ManagedPaths,
	*,
	exact_lookup: ExactMusicLookup,
	cache_updater: CacheTrackUpdater | None,
	validated: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
	if persistent_id in validated:
		return validated[persistent_id]
	cached = index.by_persistent_id.get(persistent_id)
	actual = exact_lookup(persistent_id)
	expected = cached or _expected_track_from_manifest(recording, persistent_id)
	valid, _reason = validate_exact_track(expected, actual)
	if not valid:
		fallback = MusicIndex([track for track in index.tracks if str(track.get("persistent_id") or "") != persistent_id])
		result = reconcile_recording_music(recording, fallback)
		if result["status"] != "resolved":
			recording["music_binding"] = None
			return None
		candidate_id = music_binding_id(recording)
		actual = exact_lookup(candidate_id)
		valid, _reason = validate_exact_track(_expected_track_from_manifest(recording, candidate_id), actual)
		if not valid:
			recording["music_binding"] = None
			return None
		persistent_id = candidate_id
	assert actual is not None
	if cache_updater:
		try:
			cache_updater(actual)
		except Exception:
			pass
	recording.pop("last_error", None)
	recording.pop("review", None)
	index.by_persistent_id[persistent_id] = actual
	save_manifest(paths, manifest)
	validated[persistent_id] = actual
	return actual


def _is_different_managed_album(recording: dict[str, Any], album_id: str) -> bool:
	managed = recording.get("managed_file") or {}
	managed_album_id = str(managed.get("spotify_album_id") or "")
	return managed.get("metadata_profile") == "album" and bool(managed_album_id) and managed_album_id != album_id


def _playlist_artwork_needs_update(recording: dict[str, Any], paths: ManagedPaths) -> bool:
	managed = recording.get("managed_file") or {}
	cover_url = str(recording.get("source_metadata", {}).get("cover_url") or "")
	return bool(
		cover_url
		and managed.get("managed_by_crate")
		and managed.get("metadata_profile") in (None, "playlist")
		and managed.get("artwork_source_url") != cover_url
		and _managed_exists(recording, paths)
		and managed_duration_is_valid(recording, paths)
	)


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
		resolution = reconcile_recording_music(recording, index)
		if resolution["status"] == "resolved":
			status = "reused_music"
			detail = f"existing Music match {music_binding_id(recording)}"
			recording.pop("review", None)
			recording.pop("last_error", None)
		elif resolution["status"] == "ambiguous":
			recording["review"] = {
				"kind": "music_ambiguity",
				"message": "Multiple Music tracks are plausible matches. Choose the correct recording before importing.",
				"candidates": resolution["candidates"],
			}
			status = "review_music"
			detail = "ambiguous Music candidates; no download will occur"
		elif _playlist_artwork_needs_update(recording, paths):
			status = "artwork_update"
			detail = "repair the Crate-managed MP3 with this track's individual Spotify album artwork"
		elif _managed_exists(recording, paths) and managed_duration_is_valid(recording, paths):
			status = "managed_existing"
			detail = "reuse canonical managed MP3"
		elif recording.get("managed_file"):
			status = "ready_to_download"
			detail = "registered managed MP3 failed duration validation; rebuild required"
		else:
			status = "ready_to_download" if recording.get("youtube") else "search_required"
			detail = "Music has no confident match; reviewed source is ready" if recording.get("youtube") else "not found in Music; YouTube search required"
			recording.pop("review", None)
		counts[status] = counts.get(status, 0) + 1
		row = {
			"position": position,
			"recording_id": key,
			"spotify_id": track.get("sp_id"),
			"title": recording["source_metadata"].get("title") or "",
			"artists": track.get("artists") or "",
			"status": status,
			"detail": detail,
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
	mislabelled = _mislabelled_album_matches(album, music_tracks)
	rows: list[dict[str, Any]] = []
	items: list[dict[str, Any]] = []
	counts: dict[str, int] = {}
	for source_position, track in enumerate(album["tracks"], start=1):
		position = int(track.get("position") or source_position)
		key, recording = upsert_recording(planned, track, source_type="album")
		requested_album = str(track.get("album") or album.get("name") or "")
		resolution = reconcile_recording_music(recording, index, preferred_album=requested_album)
		if source_position - 1 in mislabelled and resolution["status"] in ("missing", "album_mismatch"):
			candidate = mislabelled[source_position - 1]
			changed = bind_recording_to_music(recording, candidate)
			resolution = {"status": "resolved", "candidate": candidate, "candidates": [candidate], "binding_changed": changed}
		managed_ready = _managed_exists(recording, paths) and managed_duration_is_valid(recording, paths)
		managed = recording.get("managed_file") or {}
		candidate = resolution.get("candidate")
		if resolution["status"] == "resolved":
			if candidate:
				recover_moved_managed_file(recording, candidate, paths)
			status = "reused_music"
			detail = f"reuse requested album track in Music ({music_binding_id(recording)})"
			recording.pop("review", None)
			recording.pop("last_error", None)
		elif resolution["status"] == "ambiguous":
			recording["review"] = {"kind": "music_ambiguity", "message": "Multiple Music tracks are plausible matches for this album.", "candidates": resolution["candidates"]}
			status = "review_music"
			detail = "ambiguous Music candidates; no download will occur"
		elif resolution["status"] == "album_mismatch" and managed_ready and managed.get("managed_by_crate"):
			if _is_different_managed_album(recording, str(album["id"])):
				recording["review"] = {"kind": "album_identity_conflict", "message": "This Crate-managed file already carries a different album identity and needs review.", "candidates": resolution["candidates"]}
				status = "review_music"
				detail = "managed file already carries a different album identity"
			else:
				recording.pop("review", None)
				recording.pop("last_error", None)
				status = "upgrade_managed"
				detail = "upgrade the Crate-managed loose file to real album metadata"
		elif resolution["status"] == "album_mismatch":
			recording["review"] = {"kind": "album_identity_mismatch", "message": "The recording exists in Music under different album metadata, and no unique requested-album copy was found. The existing file will not be modified.", "candidates": resolution["candidates"]}
			status = "review_music"
			detail = "existing recording belongs to different album metadata; review required"
		elif managed_ready:
			if managed.get("metadata_profile") == "album" and managed.get("spotify_album_id") == str(album["id"]):
				status = "managed_existing"
				detail = "reuse canonical album-tagged managed MP3"
			elif _is_different_managed_album(recording, str(album["id"])):
				recording["review"] = {"kind": "album_identity_conflict", "message": "This Crate-managed file already carries a different album identity and needs review.", "candidates": []}
				status = "review_music"
				detail = "managed file already carries a different album identity"
			else:
				status = "upgrade_managed"
				detail = "upgrade Crate-managed MP3 from Playlist Imports to real album metadata"
		elif recording.get("managed_file"):
			status = "ready_to_download"
			detail = "registered managed MP3 failed duration validation; rebuild with album metadata"
		else:
			status = "ready_to_download" if recording.get("youtube") else "search_required"
			detail = "Music has no confident match; reviewed source is ready" if recording.get("youtube") else "not found in Music; YouTube search required"
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


def build_album_library_preview(
	album: dict[str, Any],
	music_tracks: list[dict[str, Any]],
	manifest: dict[str, Any],
) -> AlbumPreview:
	"""Show current Music album membership and repair only unique recording bindings."""
	planned = clone_manifest(manifest)
	mislabelled = _mislabelled_album_matches(album, music_tracks)
	album_tracks: dict[str, list[dict[str, Any]]] = {}
	for candidate in music_tracks:
		album_name = normalize_text(clean_release_labels(candidate.get("album")))
		if album_name and candidate.get("persistent_id"):
			album_tracks.setdefault(album_name, []).append(candidate)
	rows: list[dict[str, Any]] = []
	items: list[dict[str, Any]] = []
	counts = {"in_library": 0, "not_in_library": 0}
	for source_position, track in enumerate(album["tracks"], start=1):
		position = int(track.get("position") or source_position)
		key, recording = upsert_recording(planned, track, source_type="album")
		requested_album = normalize_text(clean_release_labels(track.get("album") or album.get("name")))
		candidates = album_tracks.get(requested_album, [])
		match = match_music_track(track, candidates)
		if match["status"] == "missing" and source_position - 1 in mislabelled:
			match = {"status": "reused", "candidate": mislabelled[source_position - 1]}
		if match["status"] == "missing":
			# Album editions can use different track boundaries. Exact title, artist,
			# disc, and position still identify a Music album entry for search status.
			exact = [
				candidate for candidate in candidates
				if album_track_position_match({**track, "album": track.get("album") or album.get("name"), "track_no": track.get("track_no") or position}, candidate)[0]
			]
			if len(exact) == 1:
				match = {"status": "reused", "candidate": exact[0]}
		if match["status"] == "reused":
			bind_recording_to_music(recording, match["candidate"])
			status = "in_library"
		else:
			status = "not_in_library"
		counts[status] += 1
		rows.append({
			"position": position,
			"recording_id": key,
			"spotify_id": track.get("sp_id"),
			"title": recording["source_metadata"].get("title") or "",
			"artists": track.get("artists") or "",
			"track_no": int(track.get("track_no") or position),
			"disc_no": int(track.get("disc_no") or 1),
			"status": status,
			"detail": "In Library" if status == "in_library" else "Not in Library",
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
		youtube = recording.get("youtube") or {}
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
		elif music_binding_id(recording):
			status = "reused_music"
			detail = "Saved Music match; it will be checked once at the final Music stage"
		elif recording.get("managed_file"):
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
	items_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
	manifest = preview.manifest
	playlist = manifest["playlists"][preview.playlist_id]
	if not playlist.get("complete"):
		raise ValueError(playlist.get("warning") or "Spotify did not expose the complete ordered playlist.")
	paths.create()
	save_manifest(paths, manifest)
	work_items = items_override if items_override is not None else playlist["items"]
	processed: set[str] = set()
	for item in sorted(work_items, key=lambda value: int(value["position"])):
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
				if music_binding_id(recording):
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
	for item in work_items:
		recording = manifest["recordings"][item["recording_id"]]
		if recording.get("last_error"):
			item["status"] = "failed"
		elif music_binding_id(recording):
			# Album imports intentionally keep their managed-file reference so album
			# metadata remains canonical, even after Music has copied the source file.
			# A playlist preview already validated the persistent Music ID, so do not
			# turn that safe reuse into a download failure when the source MP3 is gone.
			item["status"] = "reused_music"
		elif _managed_exists(recording, paths):
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
						"message": "No verified YouTube recording met the automatic identity and duration requirements. Choose a verified recording in Review Activity.",
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
		managed = recording.get("managed_file") or {}
		if music_binding_id(recording):
			item["status"] = "reused_music"
		elif _managed_exists(recording, paths) and managed.get("metadata_profile") == "album":
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
	cache_remover: CacheTrackRemover | None = None,
	verify_music: MusicTrackVerifier | None = None,
) -> dict[str, Any]:
	album = manifest.get("albums", {})[album_id]
	index = MusicIndex(music_tracks)
	exact_lookup = exact_lookup or (lambda persistent_id: index.by_persistent_id.get(persistent_id))
	validated: dict[str, dict[str, Any]] = {}
	artwork_paths: dict[str, Path] = {}
	for item in sorted(album["items"], key=lambda value: int(value["position"])):
		recording = manifest["recordings"][item["recording_id"]]
		resolution = reconcile_recording_music(recording, index, preferred_album=str(album.get("name") or ""))
		if resolution["status"] == "ambiguous":
			recording["review"] = {"kind": "music_ambiguity", "message": "Multiple Music tracks are plausible matches for this album.", "candidates": resolution["candidates"]}
		if recording.get("review") or recording.get("last_error"):
			raise MusicAutomationError(
				f"Album track {item['position']} is unresolved ({recording['source_metadata']['title']}). Resolve it before changing Music."
			)
		persistent_id = music_binding_id(recording)
		if persistent_id:
			_progress(on_progress, "checking_music_ids", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			candidate = _validate_exact_reference(manifest, recording, persistent_id, index, paths, exact_lookup=exact_lookup, cache_updater=cache_updater, validated=validated)
			persistent_id = music_binding_id(recording)
			if candidate and _album_matches({"album": album.get("name")}, candidate):
				continue
		managed = recording.get("managed_file") or {}
		path = paths.root / str(managed.get("relative_path") or "")
		if not managed.get("managed_by_crate") or not path.is_file() or managed.get("metadata_profile") != "album":
			raise MusicAutomationError(
				f"Album track {item['position']} is not ready ({recording['source_metadata']['title']}). Run the album download again first."
			)
		artwork_paths[recording["recording_id"]] = extract_embedded_artwork(
			path,
			paths.staging / recording["recording_id"] / "music-album-artwork.jpg",
		)
	new_imports = 0
	updated_tracks = 0
	reused_tracks = 0
	stability_recoveries = 0
	new_recording_ids: list[str] = []

	def recover_missing_addition(recording_id: str, previous_id: str) -> str:
		nonlocal stability_recoveries
		recording = manifest["recordings"][recording_id]
		managed = recording.get("managed_file") or {}
		path = paths.root / str(managed.get("relative_path") or "")
		recording["music_import_pending"] = {"relative_path": managed.get("relative_path"), "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
		save_manifest(paths, manifest)
		replacement = import_managed_file(path)
		new_id = str(replacement.get("persistent_id") or "")
		if not new_id:
			raise MusicAutomationError("Crate Music Importer could not recover a Music track whose first addition disappeared.")
		bind_recording_to_music(recording, replacement)
		save_manifest(paths, manifest)
		if cache_updater:
			try:
				cache_updater(replacement)
			except Exception:
				pass
		if cache_remover and previous_id != new_id:
			try:
				cache_remover({previous_id})
			except Exception:
				pass
		recording.pop("music_import_pending", None)
		recording.pop("review", None)
		recording.pop("last_error", None)
		index.by_persistent_id[new_id] = replacement
		save_manifest(paths, manifest)
		stability_recoveries += 1
		return new_id

	def confirm_new_additions() -> None:
		if not verify_music or not new_recording_ids:
			return
		for _attempt in range(3):
			expected_ids = [music_binding_id(manifest["recordings"][recording_id]) for recording_id in new_recording_ids]
			verified = verify_music(expected_ids)
			missing = [(recording_id, persistent_id) for recording_id, persistent_id in zip(new_recording_ids, expected_ids) if persistent_id not in verified]
			if missing:
				for recording_id, previous_id in missing:
					recover_missing_addition(recording_id, previous_id)
				continue
			for recording_id, persistent_id in zip(new_recording_ids, expected_ids):
				recording = manifest["recordings"][recording_id]
				actual = verified[persistent_id]
				if all(field in actual for field in ("title", "artist", "album", "duration_s")):
					expected = _expected_track_from_manifest(recording, persistent_id)
					valid, reason = validate_exact_track(expected, actual)
					if not valid:
						raise MusicAutomationError(f"Music returned the wrong track after an album addition because {reason}.")
			return
		for recording_id in new_recording_ids:
			recording = manifest["recordings"][recording_id]
			recording["music_import_pending"] = {
				"relative_path": (recording.get("managed_file") or {}).get("relative_path"),
				"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
			}
			recording["last_error"] = "Crate Music Importer could not verify this track after Music processed the addition."
		save_manifest(paths, manifest)
		raise MusicAutomationError("Crate Music Importer could not stabilize every Music track, so the album was not marked complete.")

	for item in sorted(album["items"], key=lambda value: int(value["position"])):
		recording = manifest["recordings"][item["recording_id"]]
		_progress(on_progress, "adding_to_music", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
		persistent_id = music_binding_id(recording)
		candidate = validated.get(persistent_id)
		if candidate and _album_matches({"album": album.get("name")}, candidate):
			reused_tracks += 1
			_progress(on_progress, "complete", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			continue
		managed = recording.get("managed_file") or {}
		path = paths.root / str(managed.get("relative_path") or "")
		candidate_location = Path(str((candidate or {}).get("location") or "")).resolve() if (candidate or {}).get("location") else None
		updating_existing = bool(candidate is not None and managed.get("managed_by_crate") and candidate_location == path.resolve())
		imported = candidate if updating_existing else None
		if imported is None:
			if recording.get("music_import_pending"):
				raise MusicAutomationError(
					f"A previous Music import may have succeeded for {recording['source_metadata']['title']}. "
					"Refresh the Music index before retrying; refusing to create a possible duplicate."
				)
			recording["music_import_pending"] = {
				"relative_path": managed["relative_path"],
				"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
			}
			save_manifest(paths, manifest)
			imported = import_managed_file(path)
			new_imports += 1
			new_recording_ids.append(recording["recording_id"])
		assert imported is not None
		persistent_id = str(imported.get("persistent_id") or "")
		if not persistent_id:
			raise MusicAutomationError("Music did not return a persistent ID for an imported album track.")
		# A newly imported album MP3 already contains the final album tags
		# and artwork. Asking Music to rewrite it immediately can fail with -50 while
		# Cloud Library still marks the track as "Need upload". Only an existing
		# A previously bound Crate-managed loose file needs an in-place refresh.
		if updating_existing:
			update_managed_music_track(persistent_id, path, recording, artwork_paths[recording["recording_id"]])
			updated_tracks += 1
		bind_recording_to_music(recording, imported)
		recording.pop("music_import_pending", None)
		save_manifest(paths, manifest)
		if cache_updater:
			try:
				cache_updater(imported)
			except Exception:
				pass
		recording.pop("last_error", None)
		index.by_persistent_id[persistent_id] = imported
		save_manifest(paths, manifest)
		if recording["recording_id"] in new_recording_ids:
			# Do not burst another file into Music while any earlier addition is
			# still provisional. Recheck the complete new set after each add so a
			# disappearing entry is recovered before the next Music mutation.
			confirm_new_additions()
		_progress(on_progress, "complete", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
	confirm_new_additions()
	return {
		"track_count": len(album["items"]),
		"new_imports": new_imports,
		"recovered_imports": 0,
		"updated_tracks": updated_tracks,
		"reused_tracks": reused_tracks,
		"stability_recoveries": stability_recoveries,
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
) -> dict[str, Any]:
	playlist = manifest["playlists"][playlist_id]
	known_playlist_id = playlist.get("music_playlist_persistent_id")
	status, found_id = playlist_status(playlist["name"], known_playlist_id)
	if status == "COLLISION":
		raise MusicAutomationError(
			f"A Music playlist named '{playlist['name']}' already exists with persistent ID {found_id}; rename it or deliberately adopt it outside this tool."
		)
	if status == "MISSING":
		raise MusicAutomationError("The previously saved Music playlist is missing. Refusing to replace another playlist by name.")
	index = MusicIndex(music_tracks)
	exact_lookup = exact_lookup or (lambda persistent_id: index.by_persistent_id.get(persistent_id))
	validated: dict[str, dict[str, Any]] = {}
	for item in sorted(playlist["items"], key=lambda value: int(value["position"])):
		recording = manifest["recordings"][item["recording_id"]]
		resolution = reconcile_recording_music(recording, index)
		if resolution["status"] == "ambiguous":
			recording["review"] = {"kind": "music_ambiguity", "message": "Multiple Music tracks are plausible matches. Choose the correct recording before applying the playlist.", "candidates": resolution["candidates"]}
			save_manifest(paths, manifest)
			raise MusicAutomationError(f"Playlist position {item['position']} has ambiguous Music matches.")
		persistent_id = music_binding_id(recording)
		if persistent_id:
			_progress(on_progress, "checking_music_ids", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			actual = _validate_exact_reference(
				manifest,
				recording,
				persistent_id,
				index,
				paths,
				exact_lookup=exact_lookup,
				cache_updater=cache_updater,
				validated=validated,
			)
			if actual:
				continue
		if not _managed_exists(recording, paths):
			raise MusicAutomationError(
				f"Playlist position {item['position']} is unresolved ({recording['source_metadata']['title']}). Resolve it before applying Music changes."
			)
		if recording.get("music_import_pending"):
			message = (
				f"A previous Music import may have succeeded for {recording['source_metadata']['title']}. "
				"Refresh the Music index before retrying; refusing to create a possible duplicate."
			)
			recording["last_error"] = message
			save_manifest(paths, manifest)
			raise MusicAutomationError(message)
	ordered_ids: list[str] = []
	new_imports = 0
	for item in sorted(playlist["items"], key=lambda value: int(value["position"])):
		recording = manifest["recordings"][item["recording_id"]]
		_progress(on_progress, "adding_to_music", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
		persistent_id = music_binding_id(recording)
		if persistent_id and persistent_id in validated:
			if recording.get("artwork_sync_pending"):
				managed = recording.get("managed_file") or {}
				path = paths.root / str(managed.get("relative_path") or "")
				artwork_path = extract_embedded_artwork(
					path,
					paths.staging / recording["recording_id"] / "music-playlist-artwork.jpg",
				)
				actual_location = str(validated[persistent_id].get("location") or "")
				if not managed.get("managed_by_crate") or actual_location != str(path):
					raise MusicAutomationError("Artwork update refused because the Music track does not use the registered Crate-managed file.")
				update_managed_music_artwork(persistent_id, path, artwork_path)
				recording.pop("artwork_sync_pending", None)
				save_manifest(paths, manifest)
			ordered_ids.append(persistent_id)
			_progress(on_progress, "complete", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
			continue
		managed = recording.get("managed_file") or {}
		path = paths.root / str(managed.get("relative_path") or "")
		if not managed.get("managed_by_crate") or not path.is_file():
			raise MusicAutomationError("A missing recording can only be imported from its verified Crate-managed file.")
		recording["music_import_pending"] = {"relative_path": managed.get("relative_path"), "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
		save_manifest(paths, manifest)
		imported = import_managed_file(path)
		new_imports += 1
		bind_recording_to_music(recording, imported)
		persistent_id = str(imported.get("persistent_id") or "")
		if not persistent_id:
			raise MusicAutomationError("Music did not return a persistent ID for an imported managed MP3.")
		recording.pop("music_import_pending", None)
		save_manifest(paths, manifest)
		if cache_updater:
			try:
				cache_updater(imported)
			except Exception:
				pass
		recording.pop("artwork_sync_pending", None)
		recording.pop("last_error", None)
		ordered_ids.append(persistent_id)
		validated[persistent_id] = imported
		index.by_persistent_id[persistent_id] = imported
		save_manifest(paths, manifest)
		_progress(on_progress, "complete", recording_id=recording["recording_id"], position=int(item["position"]), title=recording["source_metadata"].get("title") or "", artists=recording["source_metadata"].get("artists") or "")
	playlist_pid = sync_music_playlist(playlist["name"], known_playlist_id, ordered_ids)
	playlist["music_playlist_persistent_id"] = playlist_pid
	playlist["spotify_occurrence_snapshot"] = [
		{
			"spotify_id": str(item.get("spotify_id") or ""),
			"recording_id": str(item.get("recording_id") or ""),
			"spotify_position": int(item.get("spotify_position") or item.get("position") or position),
			"saved_position": int(item.get("position") or position),
		}
		for position, item in enumerate(sorted(playlist["items"], key=lambda value: int(value["position"])), start=1)
	]
	playlist["spotify_occurrence_counts"] = {}
	for occurrence in playlist["spotify_occurrence_snapshot"]:
		spotify_id = occurrence["spotify_id"]
		if spotify_id:
			playlist["spotify_occurrence_counts"][spotify_id] = int(playlist["spotify_occurrence_counts"].get(spotify_id) or 0) + 1
	playlist["latest_observed_spotify_snapshot"] = list(playlist["spotify_occurrence_snapshot"])
	playlist["latest_observed_spotify_counts"] = dict(playlist["spotify_occurrence_counts"])
	playlist["last_successful_update_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
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
	bind_recording_to_music(recording, candidate)
	recording.pop("review", None)
	recording.pop("last_error", None)
	for playlist_id in recording.get("playlist_memberships", {}):
		write_playlist_m3u8(manifest, playlist_id, paths)
	save_manifest(paths, manifest)
	return recording
