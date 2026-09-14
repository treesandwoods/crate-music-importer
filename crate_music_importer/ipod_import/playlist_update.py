"""Guarded full-order updates for previously imported Spotify playlists."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from crate_music_importer.ipod_import.manifest import ManagedPaths, clone_manifest, save_manifest, write_playlist_m3u8
from crate_music_importer.ipod_import.media import audio_sha256, extract_embedded_artwork
from crate_music_importer.ipod_import.music import (
	MusicAutomationError,
	MusicIndex,
	delete_importer_owned_music_track,
	edit_music_playlist_membership,
	import_managed_file,
	lookup_importer_owned_music_track,
	playlist_membership,
	playlist_status,
	update_managed_music_artwork,
)
from crate_music_importer.ipod_import.music_cache import cache_track_for_recording, validate_exact_track


ProgressCallback = Callable[[dict[str, Any]], None]
ExactMusicLookup = Callable[[str], dict[str, Any] | None]
CacheTrackUpdater = Callable[[dict[str, Any]], None]
CacheTrackRemover = Callable[[set[str]], int]


def _now() -> str:
	return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _snapshot(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
	return [
		{
			"spotify_id": str(item.get("spotify_id") or ""),
			"recording_id": str(item.get("recording_id") or ""),
			"spotify_position": int(item.get("spotify_position") or item.get("position") or position),
			"saved_position": int(item.get("position") or position),
		}
		for position, item in enumerate(items, start=1)
	]


def _occurrence_counts(snapshot: list[dict[str, Any]]) -> dict[str, int]:
	counts: dict[str, int] = defaultdict(int)
	for occurrence in snapshot:
		spotify_id = str(occurrence.get("spotify_id") or "")
		if spotify_id:
			counts[spotify_id] += 1
	return dict(sorted(counts.items()))


def saved_playlists(manifest: dict[str, Any]) -> list[dict[str, Any]]:
	values = []
	for playlist_id, playlist in manifest.get("playlists", {}).items():
		if not isinstance(playlist, dict):
			continue
		values.append({
			"id": str(playlist_id),
			"name": str(playlist.get("name") or "Spotify Playlist"),
			"track_count": len(playlist.get("items") or []),
			"spotify_url": str(playlist.get("spotify_url") or ""),
			"cover_url": str(playlist.get("cover_url") or "") or None,
			"music_playlist_persistent_id": str(playlist.get("music_playlist_persistent_id") or "") or None,
		})
	return sorted(values, key=lambda value: (value["name"].casefold(), value["id"]))


def backfill_playlist_covers(
	manifest: dict[str, Any],
	cover_fetcher: Callable[[str], str | None],
	*,
	max_workers: int = 8,
) -> int:
	"""Best-effort cache of shared playlist thumbnails for legacy saved imports."""
	missing = [
		(str(playlist_id), str(playlist.get("spotify_url") or ""))
		for playlist_id, playlist in manifest.get("playlists", {}).items()
		if isinstance(playlist, dict) and not playlist.get("cover_url") and playlist.get("spotify_url")
	]
	if not missing:
		return 0
	updated = 0
	with ThreadPoolExecutor(max_workers=min(max_workers, len(missing))) as executor:
		futures = {executor.submit(cover_fetcher, url): playlist_id for playlist_id, url in missing}
		for future in as_completed(futures):
			try:
				cover_url = str(future.result() or "")
			except Exception:
				continue
			if not cover_url.startswith(("https://", "http://")):
				continue
			manifest["playlists"][futures[future]]["cover_url"] = cover_url
			updated += 1
	return updated


def _baseline(playlist: dict[str, Any], manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, bool]:
	saved = playlist.get("spotify_occurrence_snapshot")
	if isinstance(saved, list):
		values = [dict(item) for item in saved if isinstance(item, dict)]
		backfilled = False
	else:
		values = _snapshot([dict(item) for item in playlist.get("items") or [] if isinstance(item, dict)])
		backfilled = True
	for item in values:
		if item.get("spotify_id"):
			continue
		recording = manifest.get("recordings", {}).get(str(item.get("recording_id") or "")) or {}
		known_ids = {
			str(occurrence.get("spotify_id") or "")
			for occurrence in recording.get("source_occurrences") or []
			if isinstance(occurrence, dict) and occurrence.get("spotify_id")
		}
		if len(known_ids) == 1:
			item["spotify_id"] = known_ids.pop()
			backfilled = True
	return values, all(str(item.get("spotify_id") or "") for item in values), backfilled


def _recording_title(manifest: dict[str, Any], recording_id: str) -> tuple[str, str]:
	recording = manifest.get("recordings", {}).get(recording_id) or {}
	metadata = recording.get("source_metadata") or {}
	return str(metadata.get("title") or "Unknown track"), str(metadata.get("artists") or "")


def _delete_kind(manifest: dict[str, Any], playlist_id: str, recording_id: str, surviving_ids: set[str]) -> str:
	recording = manifest.get("recordings", {}).get(recording_id) or {}
	other_playlists = set((recording.get("playlist_memberships") or {}).keys()) - {playlist_id}
	managed = recording.get("managed_file") or {}
	active = recording.get("active_reference") or {}
	sole_playlist_import = (
		recording_id not in surviving_ids
		and not other_playlists
		and not (recording.get("album_memberships") or {})
		and not recording.get("album_metadata")
		and bool(managed.get("tool_owned"))
		and bool(managed.get("audio_sha256"))
		and active.get("kind") == "managed_file"
	)
	return "delete" if sole_playlist_import else "unlink"


def _recording_persistent_id(manifest: dict[str, Any], recording_id: str) -> str:
	recording = manifest.get("recordings", {}).get(recording_id) or {}
	active = recording.get("active_reference") or {}
	return str((recording.get("music") or {}).get("persistent_id") or active.get("persistent_id") or "")


def _spotify_reorders(manifest: dict[str, Any], snapshot: list[dict[str, Any]], addition_positions: set[int]) -> list[dict[str, Any]]:
	rows = []
	for occurrence in snapshot:
		from_position = int(occurrence.get("saved_position") or 0)
		to_position = int(occurrence.get("spotify_position") or 0)
		if not occurrence.get("recording_id") or to_position in addition_positions or from_position == to_position:
			continue
		title, artists = _recording_title(manifest, str(occurrence["recording_id"]))
		rows.append({
			"spotify_id": str(occurrence.get("spotify_id") or ""),
			"recording_id": str(occurrence["recording_id"]),
			"title": title,
			"artists": artists,
			"from_position": from_position,
			"to_position": to_position,
		})
	return rows


def _music_sync_changes(
	membership: list[str],
	expected_snapshot: list[dict[str, Any]],
	manifest: dict[str, Any],
	music_tracks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	"""Describe manual Music drift from the saved imported occurrence sequence."""
	positions: dict[str, deque[int]] = defaultdict(deque)
	for position, persistent_id in enumerate(membership, start=1):
		positions[persistent_id].append(position)
	rows: list[dict[str, Any]] = []
	for occurrence in sorted(expected_snapshot, key=lambda row: int(row.get("saved_position") or 0)):
		to_position = int(occurrence.get("saved_position") or 0)
		recording_id = str(occurrence.get("recording_id") or "")
		persistent_id = _recording_persistent_id(manifest, recording_id)
		if not persistent_id:
			continue
		from_position = positions[persistent_id].popleft() if positions[persistent_id] else None
		title, artists = _recording_title(manifest, recording_id)
		if from_position is None:
			rows.append({
				"kind": "restore",
				"persistent_id": persistent_id,
				"recording_id": recording_id,
				"title": title,
				"artists": artists,
				"from_position": None,
				"to_position": to_position,
			})
		elif from_position != to_position:
			rows.append({
				"kind": "reorder",
				"persistent_id": persistent_id,
				"recording_id": recording_id,
				"title": title,
				"artists": artists,
				"from_position": from_position,
				"to_position": to_position,
			})
	track_by_id = {str(track.get("persistent_id") or ""): track for track in music_tracks}
	for persistent_id, remaining_positions in positions.items():
		for from_position in remaining_positions:
			track = track_by_id.get(persistent_id) or {}
			rows.append({
				"kind": "remove",
				"persistent_id": persistent_id,
				"recording_id": "",
				"title": str(track.get("title") or f"Music track {persistent_id}"),
				"artists": str(track.get("artist") or ""),
				"from_position": from_position,
				"to_position": None,
			})
	return sorted(rows, key=lambda row: (int(row.get("to_position") or 10**9), int(row.get("from_position") or 10**9), str(row.get("persistent_id") or "")))


@dataclass
class PlaylistUpdatePreview:
	manifest: dict[str, Any]
	playlist_id: str
	source: dict[str, Any]
	additions: list[dict[str, Any]]
	removals: list[dict[str, Any]]
	reorders: list[dict[str, Any]]
	music_changes: list[dict[str, Any]]
	addition_items: list[dict[str, Any]]
	removal_positions: list[int]
	spotify_snapshot: list[dict[str, Any]]
	music_membership: list[str]
	baseline_backfilled: bool
	removals_deferred: bool
	state: str
	warning: str | None

	def confirmation_token(self) -> str:
		payload = {
			"playlist_id": self.playlist_id,
			"snapshot": self.spotify_snapshot,
			"music_membership": self.music_membership,
			"additions": [{"spotify_id": row.get("spotify_id"), "position": row.get("position")} for row in self.additions],
			"removals": [{"spotify_id": row.get("spotify_id"), "saved_position": row.get("saved_position"), "action": row.get("action")} for row in self.removals],
			"reorders": [{"spotify_id": row.get("spotify_id"), "from_position": row.get("from_position"), "to_position": row.get("to_position")} for row in self.reorders],
			"music_changes": [{"kind": row.get("kind"), "persistent_id": row.get("persistent_id"), "from_position": row.get("from_position"), "to_position": row.get("to_position")} for row in self.music_changes],
		}
		return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

	def to_dict(self) -> dict[str, Any]:
		return {
			"source": self.source,
			"playlist_id": self.playlist_id,
			"additions": self.additions,
			"removals": self.removals,
			"reorders": self.reorders,
			"music_changes": self.music_changes,
			"up_to_date": self.state == "up_to_date",
			"incomplete_data": self.state == "incomplete_data",
			"blocked": self.state not in {"ready", "up_to_date"},
			"state": self.state,
			"warning": self.warning,
			"baseline_backfilled": self.baseline_backfilled,
			"removals_deferred": self.removals_deferred,
			"confirmation_token": self.confirmation_token(),
		}


def _blocked_preview(
	manifest: dict[str, Any],
	playlist_id: str,
	source: dict[str, Any],
	state: str,
	warning: str,
	*,
	baseline_backfilled: bool = False,
) -> PlaylistUpdatePreview:
	return PlaylistUpdatePreview(
		manifest=clone_manifest(manifest),
		playlist_id=playlist_id,
		source=source,
		additions=[],
		removals=[],
		reorders=[],
		music_changes=[],
		addition_items=[],
		removal_positions=[],
		spotify_snapshot=[],
		music_membership=[],
		baseline_backfilled=baseline_backfilled,
		removals_deferred=False,
		state=state,
		warning=warning,
	)


def build_playlist_update_preview(
	current: dict[str, Any],
	music_tracks: list[dict[str, Any]],
	manifest: dict[str, Any],
	paths: ManagedPaths,
	*,
	status_checker: Callable[[str, str | None], tuple[str, str | None]] = playlist_status,
	membership_reader: Callable[[str, str], list[str]] = playlist_membership,
) -> PlaylistUpdatePreview:
	from crate_music_importer.ipod_import.pipeline import build_preview

	playlist_id = str(current.get("id") or "")
	if playlist_id not in manifest.get("playlists", {}):
		raise ValueError("This Spotify playlist has not been imported by Crate Music Importer.")
	saved = manifest["playlists"][playlist_id]
	source = {
		"type": "playlist",
		"id": playlist_id,
		"name": str(saved.get("name") or current.get("name") or "Spotify Playlist"),
		"url": str(current.get("url") or saved.get("spotify_url") or ""),
		"cover_url": str(current.get("cover_url") or saved.get("cover_url") or "") or None,
		"saved_total": len(saved.get("items") or []),
		"current_total": int(current.get("total_count") or 0),
	}
	reported = current.get("total_count")
	tracks = [dict(track) for track in current.get("tracks") or []]
	if not current.get("complete", True) or reported is None or int(reported) != len(tracks):
		warning = str(current.get("warning") or f"Spotify reported {reported} tracks but only {len(tracks)} were fetched. Update blocked.")
		return _blocked_preview(manifest, playlist_id, source, "incomplete_data", warning)
	known_pid = str(saved.get("music_playlist_persistent_id") or "")
	if not known_pid:
		return _blocked_preview(manifest, playlist_id, source, "missing_music_playlist", "The saved Music playlist persistent ID is missing. Update refused.")
	status, found_id = status_checker(str(saved.get("name") or "Spotify Playlist"), known_pid)
	if status == "MISSING":
		return _blocked_preview(manifest, playlist_id, source, "missing_music_playlist", "The previously imported Music playlist is missing. Update refused.")
	if status == "COLLISION":
		return _blocked_preview(manifest, playlist_id, source, "playlist_collision", f"A different Music playlist now uses this name ({found_id or 'unknown ID'}). Update refused.")

	baseline, baseline_complete, backfilled = _baseline(saved, manifest)
	if not baseline_complete:
		warning = "The saved Spotify occurrence baseline is incomplete, so an exact full-playlist sync cannot be proven safe. Reimport the playlist before updating it."
		return _blocked_preview(manifest, playlist_id, source, "incomplete_baseline", warning, baseline_backfilled=backfilled)
	music_membership = membership_reader(str(saved.get("name") or "Spotify Playlist"), known_pid)
	remaining: dict[str, deque[int]] = defaultdict(deque)
	for index, occurrence in enumerate(baseline):
		spotify_id = str(occurrence.get("spotify_id") or "")
		if spotify_id:
			remaining[spotify_id].append(index)
	consumed: set[int] = set()
	addition_tracks: list[tuple[int, dict[str, Any]]] = []
	current_links: list[dict[str, Any]] = []
	for spotify_position, track in enumerate(tracks, start=1):
		spotify_id = str(track.get("sp_id") or "")
		if spotify_id and remaining[spotify_id]:
			baseline_index = remaining[spotify_id].popleft()
			consumed.add(baseline_index)
			current_links.append({**baseline[baseline_index], "spotify_position": spotify_position})
		else:
			addition_tracks.append((spotify_position, track))
			current_links.append({"spotify_id": spotify_id, "spotify_position": spotify_position, "recording_id": ""})

	planned = clone_manifest(manifest)
	addition_rows: list[dict[str, Any]] = []
	addition_items: list[dict[str, Any]] = []
	if addition_tracks:
		partial = dict(current)
		partial["tracks"] = [track for _, track in addition_tracks]
		partial["total_count"] = len(addition_tracks)
		partial["complete"] = True
		standard = build_preview(partial, music_tracks, planned, paths)
		planned = standard.manifest
		generated = list(planned["playlists"][playlist_id]["items"])
		next_position = max((int(item.get("position") or 0) for item in saved.get("items") or []), default=0) + 1
		for offset, (row, item, (spotify_position, _track)) in enumerate(zip(standard.rows, generated, addition_tracks)):
			row = dict(row, position=spotify_position)
			item = dict(item, position=next_position + offset, spotify_position=spotify_position)
			addition_rows.append(row)
			addition_items.append(item)
			current_links[spotify_position - 1]["recording_id"] = item["recording_id"]
			current_links[spotify_position - 1]["saved_position"] = item["position"]
	# build_preview replaces the playlist and its memberships; restore the saved state until apply succeeds.
	planned["playlists"][playlist_id] = copy.deepcopy(saved)
	for recording_id, recording in planned.get("recordings", {}).items():
		prior = manifest.get("recordings", {}).get(recording_id) or {}
		memberships = recording.setdefault("playlist_memberships", {})
		if playlist_id in (prior.get("playlist_memberships") or {}):
			memberships[playlist_id] = copy.deepcopy(prior["playlist_memberships"][playlist_id])
		else:
			memberships.pop(playlist_id, None)

	removal_indices = [index for index in range(len(baseline)) if index not in consumed]
	removal_positions = [int(baseline[index].get("saved_position") or 0) for index in removal_indices]
	removal_position_set = set(removal_positions)
	surviving_items = [item for item in saved.get("items") or [] if int(item.get("position") or 0) not in removal_position_set]
	surviving_ids = {str(item.get("recording_id") or "") for item in [*surviving_items, *addition_items]}
	removals = []
	for index in removal_indices:
		occurrence = baseline[index]
		recording_id = str(occurrence.get("recording_id") or "")
		title, artists = _recording_title(manifest, recording_id)
		removals.append({
			"saved_position": int(occurrence.get("saved_position") or 0),
			"spotify_id": str(occurrence.get("spotify_id") or ""),
			"recording_id": recording_id,
			"title": title,
			"artists": artists,
			"action": _delete_kind(manifest, playlist_id, recording_id, surviving_ids),
		})
	addition_positions = {int(item.get("spotify_position") or 0) for item in addition_items}
	reorders = _spotify_reorders(manifest, current_links, addition_positions)
	target_ids = [_recording_persistent_id(planned, str(row.get("recording_id") or "")) for row in current_links]
	music_changes = [] if all(target_ids) and music_membership == target_ids else _music_sync_changes(music_membership, baseline, manifest, music_tracks)
	state = "up_to_date" if not addition_rows and not removals and not reorders and not music_changes else "ready"
	return PlaylistUpdatePreview(planned, playlist_id, source, addition_rows, removals, reorders, music_changes, addition_items, removal_positions, current_links, music_membership, backfilled, False, state, None)


def save_pending_update(preview: PlaylistUpdatePreview, paths: ManagedPaths) -> dict[str, Any]:
	playlist = preview.manifest["playlists"][preview.playlist_id]
	playlist["pending_update"] = {
		"created_at": _now(),
		"spotify_url": preview.source["url"],
		"cover_url": preview.source.get("cover_url"),
		"spotify_snapshot": preview.spotify_snapshot,
		"music_baseline": preview.music_membership,
		"addition_items": preview.addition_items,
		"removals": preview.removals,
		"reorders": preview.reorders,
		"music_changes": preview.music_changes,
		"removal_positions": preview.removal_positions,
		"removals_deferred": preview.removals_deferred,
		"music_checkpoint": None,
		"confirmation_token": preview.confirmation_token(),
	}
	playlist["latest_observed_spotify_snapshot"] = preview.spotify_snapshot
	playlist["latest_observed_spotify_counts"] = _occurrence_counts(preview.spotify_snapshot)
	for item in preview.addition_items:
		recording = preview.manifest["recordings"][item["recording_id"]]
		recording.setdefault("pending_playlist_memberships", {})[preview.playlist_id] = [int(item["position"])]
	save_manifest(paths, preview.manifest)
	return playlist["pending_update"]


def _updated_playlist_items(
	original_items: list[dict[str, Any]],
	addition_items: list[dict[str, Any]],
	spotify_snapshot: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
	"""Rebuild saved items in exact Spotify occurrence order, including duplicates."""
	original_by_position = {int(item.get("position") or 0): dict(item) for item in original_items}
	addition_by_spotify_position = {int(item.get("spotify_position") or 0): dict(item) for item in addition_items}
	ordered: list[dict[str, Any]] = []
	normalized_snapshot = []
	for position, row in enumerate(sorted(spotify_snapshot, key=lambda value: int(value.get("spotify_position") or 0)), start=1):
		value = dict(row)
		spotify_position = int(value.get("spotify_position") or 0)
		item = addition_by_spotify_position.get(spotify_position) or original_by_position.get(int(value.get("saved_position") or 0))
		if not item:
			raise MusicAutomationError("A Spotify occurrence no longer maps to a saved playlist item. Update stopped for attention.")
		item = dict(item)
		item["position"] = position
		item["spotify_position"] = position
		item["spotify_id"] = str(value.get("spotify_id") or item.get("spotify_id") or "")
		ordered.append(item)
		value["spotify_position"] = position
		value["saved_position"] = position
		normalized_snapshot.append(value)
	return ordered, normalized_snapshot


def _safe_delete_file(paths: ManagedPaths, relative_path: str) -> None:
	if not relative_path:
		return
	target = (paths.root / relative_path).resolve()
	root = paths.root.resolve()
	if target == root or root not in target.parents:
		raise MusicAutomationError("Refusing to delete a managed file outside the configured managed directory.")
	try:
		target.unlink()
	except FileNotFoundError:
		pass


def _validate_permanent_deletions(
	manifest: dict[str, Any],
	removals: list[dict[str, Any]],
	paths: ManagedPaths,
	*,
	audio_hasher: Callable[[Path], str],
	owned_lookup: Callable[[str], dict[str, Any] | None],
) -> None:
	for removal in removals:
		if removal.get("action") != "delete":
			continue
		recording_id = str(removal.get("recording_id") or "")
		recording = manifest.get("recordings", {}).get(recording_id) or {}
		managed = recording.get("managed_file") or {}
		if not managed.get("tool_owned") or not managed.get("audio_sha256"):
			raise MusicAutomationError("Permanent deletion is missing importer ownership or encoded-audio proof. Stopped before changing Music.")
		path = (paths.root / str(managed.get("relative_path") or "")).resolve()
		if not path.is_file() or audio_hasher(path) != str(managed.get("audio_sha256") or ""):
			raise MusicAutomationError("A permanently deleted candidate's encoded audio no longer matches its saved proof. Stopped before changing Music.")
		persistent_id = str((recording.get("music") or {}).get("persistent_id") or "")
		owned = owned_lookup(recording_id)
		if not persistent_id or not owned or str(owned.get("persistent_id") or "") != persistent_id:
			raise MusicAutomationError("A permanently deleted candidate no longer has the exact importer-owned Music item. Stopped before changing Music.")


def _ensure_addition_ids(
	manifest: dict[str, Any],
	items: list[dict[str, Any]],
	paths: ManagedPaths,
	music_tracks: list[dict[str, Any]],
	*,
	exact_lookup: ExactMusicLookup,
	cache_updater: CacheTrackUpdater,
	on_progress: ProgressCallback | None,
) -> tuple[list[str], int]:
	from crate_music_importer.ipod_import.pipeline import _managed_exists

	index = MusicIndex(music_tracks)
	ids: list[str] = []
	new_imports = 0
	resolved: dict[str, str] = {}
	for item in items:
		recording = manifest["recordings"][item["recording_id"]]
		key = recording["recording_id"]
		if key in resolved:
			ids.append(resolved[key])
			continue
		active = recording.get("active_reference") or {}
		music = recording.get("music") or {}
		persistent_id = str(music.get("persistent_id") or active.get("persistent_id") or "")
		if persistent_id:
			actual = exact_lookup(persistent_id)
			expected = index.by_persistent_id.get(persistent_id) or {
				"persistent_id": persistent_id,
				"title": recording.get("source_metadata", {}).get("title"),
				"artist": recording.get("source_metadata", {}).get("artists"),
				"duration_s": int(recording.get("source_metadata", {}).get("duration_ms") or 0) / 1000.0,
				"comment": (actual or {}).get("comment", ""),
			}
			valid, reason = validate_exact_track(expected, actual, require_importer_owned=False, recording_id=key)
			if not valid:
				raise MusicAutomationError(f"Saved Music ID {persistent_id} is no longer safe to reuse: {reason}")
		else:
			if active.get("kind") != "managed_file" or not _managed_exists(recording, paths):
				raise MusicAutomationError(f"New playlist occurrence is unresolved: {recording.get('source_metadata', {}).get('title') or key}")
			managed = recording.get("managed_file") or {}
			path = paths.root / str(managed.get("relative_path") or "")
			recording["music_import_pending"] = {"relative_path": managed.get("relative_path"), "started_at": _now()}
			save_manifest(paths, manifest)
			imported = import_managed_file(path, key)
			persistent_id = str(imported.get("persistent_id") or "")
			if not persistent_id:
				raise MusicAutomationError("Music did not return a persistent ID for an imported managed MP3.")
			recording["music"] = {"source": "managed_import", **imported}
			recording.pop("music_import_pending", None)
			cached = cache_track_for_recording(recording, imported, album_profile=False)
			cache_updater(cached)
			new_imports += 1
		if recording.get("artwork_sync_pending"):
			managed = recording.get("managed_file") or {}
			artwork = extract_embedded_artwork(paths.root / str(managed.get("relative_path") or ""), paths.staging / key / "music-playlist-artwork.jpg")
			update_managed_music_artwork(persistent_id, key, artwork)
			recording.pop("artwork_sync_pending", None)
		recording.pop("last_error", None)
		resolved[key] = persistent_id
		ids.append(persistent_id)
		if on_progress:
			on_progress({"phase": "complete", "recording_id": key, "position": int(item.get("spotify_position") or item.get("position") or 0), "title": recording.get("source_metadata", {}).get("title") or "", "artists": recording.get("source_metadata", {}).get("artists") or ""})
		save_manifest(paths, manifest)
	return ids, new_imports


def apply_playlist_update(
	manifest: dict[str, Any],
	playlist_id: str,
	paths: ManagedPaths,
	music_tracks: list[dict[str, Any]],
	*,
	exact_lookup: ExactMusicLookup,
	cache_updater: CacheTrackUpdater,
	cache_remover: CacheTrackRemover,
	on_progress: ProgressCallback | None = None,
	membership_reader: Callable[[str, str], list[str]] = playlist_membership,
	membership_editor: Callable[[str, str, list[str], list[int], list[str]], list[str]] = edit_music_playlist_membership,
	music_deleter: Callable[[str, str], bool] = delete_importer_owned_music_track,
	status_checker: Callable[[str, str | None], tuple[str, str | None]] = playlist_status,
	audio_hasher: Callable[[Path], str] = audio_sha256,
	owned_lookup: Callable[[str], dict[str, Any] | None] = lookup_importer_owned_music_track,
) -> dict[str, Any]:
	playlist = manifest.get("playlists", {}).get(playlist_id)
	if not isinstance(playlist, dict):
		raise ValueError("The saved playlist no longer exists.")
	pending = playlist.get("pending_update")
	if not isinstance(pending, dict):
		raise ValueError("No confirmed playlist update is pending.")
	known_pid = str(playlist.get("music_playlist_persistent_id") or "")
	if not known_pid:
		raise MusicAutomationError("The saved Music playlist persistent ID is missing. Update refused.")
	status, found_id = status_checker(str(playlist.get("name") or "Spotify Playlist"), known_pid)
	if status == "MISSING":
		raise MusicAutomationError("The previously imported Music playlist is missing. Update refused.")
	if status == "COLLISION":
		raise MusicAutomationError(f"A different Music playlist now uses this name ({found_id or 'unknown ID'}). Update refused.")
	checkpoint = pending.get("music_checkpoint")
	if not isinstance(checkpoint, dict) or not checkpoint.get("applied_at"):
		_validate_permanent_deletions(manifest, list(pending.get("removals") or []), paths, audio_hasher=audio_hasher, owned_lookup=owned_lookup)
	addition_items = [dict(item) for item in pending.get("addition_items") or []]
	current = membership_reader(str(playlist.get("name") or "Spotify Playlist"), known_pid)
	if not isinstance(checkpoint, dict):
		preview_baseline = list(pending.get("music_baseline") or [])
		if current != preview_baseline:
			raise MusicAutomationError("The Music playlist changed after its update preview. Refresh the preview and confirm the exact Spotify sync again.")
	_append_ids, new_imports = _ensure_addition_ids(manifest, addition_items, paths, music_tracks, exact_lookup=exact_lookup, cache_updater=cache_updater, on_progress=on_progress)
	if not isinstance(checkpoint, dict):
		final = [
			_recording_persistent_id(manifest, str(occurrence.get("recording_id") or ""))
			for occurrence in sorted(pending.get("spotify_snapshot") or [], key=lambda row: int(row.get("spotify_position") or 0))
		]
		if any(not value for value in final):
			raise MusicAutomationError("A Spotify playlist occurrence has no Music persistent ID. Update stopped for attention.")
		prefix_length = 0
		while prefix_length < min(len(current), len(final)) and current[prefix_length] == final[prefix_length]:
			prefix_length += 1
		survivors = current[:prefix_length]
		remove_indexes = list(range(prefix_length, len(current)))
		planned_append = final[prefix_length:]
		checkpoint = {
			"baseline": current,
			"survivors": survivors,
			"append_ids": planned_append,
			"remove_indexes": remove_indexes,
			"final": final,
			"created_at": _now(),
		}
		pending["music_checkpoint"] = checkpoint
		save_manifest(paths, manifest)
	baseline = list(checkpoint.get("baseline") or [])
	survivors = list(checkpoint.get("survivors") or [])
	planned_append = list(checkpoint.get("append_ids") or [])
	final = list(checkpoint.get("final") or [*survivors, *planned_append])
	if current != final:
		if current == baseline:
			current = membership_editor(str(playlist.get("name") or "Spotify Playlist"), known_pid, current, list(checkpoint.get("remove_indexes") or []), planned_append)
		elif current == survivors:
			current = membership_editor(str(playlist.get("name") or "Spotify Playlist"), known_pid, current, [], planned_append)
		elif current[:len(survivors)] == survivors and current[len(survivors):] == planned_append[:len(current) - len(survivors)]:
			current = membership_editor(str(playlist.get("name") or "Spotify Playlist"), known_pid, current, [], planned_append[len(current) - len(survivors):])
		else:
			raise MusicAutomationError("Music playlist membership changed unexpectedly during this update. Stopped for attention without guessing.")
	if current != final:
		raise MusicAutomationError("Music returned an unexpected membership sequence after the playlist update.")
	pending["music_checkpoint"]["applied_at"] = _now()
	save_manifest(paths, manifest)

	remove_saved = set(int(value) for value in pending.get("removal_positions") or [])
	original_items = sorted(playlist.get("items") or [], key=lambda item: int(item.get("position") or 0))
	playlist["items"], spotify_snapshot = _updated_playlist_items(
		original_items,
		addition_items,
		list(pending.get("spotify_snapshot") or []),
	)
	for recording in manifest.get("recordings", {}).values():
		(recording.get("playlist_memberships") or {}).pop(playlist_id, None)
	positions: dict[str, list[int]] = defaultdict(list)
	for item in playlist["items"]:
		positions[str(item["recording_id"])].append(int(item["position"]))
	for recording_id, values in positions.items():
		manifest["recordings"][recording_id].setdefault("playlist_memberships", {})[playlist_id] = values
	save_manifest(paths, manifest)

	deleted = 0
	for removal in pending.get("removals") or []:
		if removal.get("action") != "delete":
			continue
		recording_id = str(removal.get("recording_id") or "")
		recording = manifest.get("recordings", {}).get(recording_id)
		if not isinstance(recording, dict):
			continue
		if recording.get("album_memberships") or recording.get("playlist_memberships"):
			raise MusicAutomationError("A track gained another imported reference after preview. Permanent deletion stopped for attention.")
		managed = recording.get("managed_file") or {}
		if not managed.get("tool_owned"):
			raise MusicAutomationError("Permanent deletion lost its importer-ownership proof. Stopped for attention.")
		persistent_id = str((recording.get("music") or {}).get("persistent_id") or "")
		if persistent_id:
			music_deleter(persistent_id, recording_id)
			cache_remover({persistent_id})
		_safe_delete_file(paths, str(managed.get("relative_path") or ""))
		shutil.rmtree(paths.staging / recording_id, ignore_errors=True)
		manifest["recordings"].pop(recording_id, None)
		deleted += 1
		save_manifest(paths, manifest)

	playlist["spotify_occurrence_snapshot"] = spotify_snapshot
	playlist["spotify_occurrence_counts"] = _occurrence_counts(playlist["spotify_occurrence_snapshot"])
	playlist["latest_observed_spotify_snapshot"] = list(spotify_snapshot)
	playlist["latest_observed_spotify_counts"] = dict(playlist["spotify_occurrence_counts"])
	playlist["spotify_url"] = str(pending.get("spotify_url") or playlist.get("spotify_url") or "")
	playlist["cover_url"] = pending.get("cover_url") or playlist.get("cover_url")
	playlist["total_count"] = len(playlist["spotify_occurrence_snapshot"])
	playlist["complete"] = True
	playlist["last_successful_update_at"] = _now()
	for recording in manifest.get("recordings", {}).values():
		(recording.get("pending_playlist_memberships") or {}).pop(playlist_id, None)
	playlist.pop("pending_update", None)
	write_playlist_m3u8(manifest, playlist_id, paths)
	save_manifest(paths, manifest)
	return {
		"track_count": len(playlist["items"]),
		"additions": len(addition_items),
		"removals": len(remove_saved),
		"reorders": len(pending.get("reorders") or []),
		"music_repairs": len(pending.get("music_changes") or []),
		"deleted": deleted,
		"new_imports": new_imports,
	}
