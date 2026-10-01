"""Read-only line protocol for ordered Raycast album checks against one snapshot."""

from __future__ import annotations

import json
from typing import Any, TextIO

from crate_music_importer.ipod_import.manifest import ManagedPaths
from crate_music_importer.ipod_import.music_cache import music_cache_tracks, read_music_cache
from crate_music_importer.ipod_import.pipeline import AlbumLibraryIndex


def check_album(index: AlbumLibraryIndex, album: dict[str, Any]) -> dict[str, Any]:
	if not isinstance(album, dict) or not album.get("id") or not album.get("name"):
		raise ValueError("An album ID and name are required.")
	total = int(album.get("totalTracks") or 0)
	if total <= 0:
		raise ValueError("Spotify returned no album track count; library status is unknown.")
	if not index.has_album(album["name"]):
		return {"status": "none", "matched": 0, "total": total}
	if "tracks" not in album:
		return {"status": "needs_tracks"}
	tracks = album["tracks"]
	if not isinstance(tracks, list) or len(tracks) != total or any(
		not isinstance(track, dict) or not track.get("title") or not track.get("artists")
		for track in tracks
	):
		raise ValueError("Spotify returned incomplete album tracks; library status is unknown.")
	# The requested album is authoritative, even if a track payload contains other metadata.
	source = {"name": album["name"], "tracks": [{**track, "album": album["name"]} for track in tracks]}
	matched = sum(candidate is not None for candidate in index.matches(source))
	return {"status": "complete" if matched == total else "partial" if matched else "none", "matched": matched, "total": total}


def run_album_search_session(paths: ManagedPaths, source: TextIO, output: TextIO) -> None:
	cache = read_music_cache(paths)
	index = AlbumLibraryIndex(music_cache_tracks(cache))
	output.write(json.dumps({"ready": True, "scannedAt": cache["scanned_at"]}) + "\n")
	output.flush()
	for line in source:
		try:
			result = check_album(index, json.loads(line))
		except (ValueError, TypeError, KeyError) as exc:
			result = {"error": str(exc)}
		output.write(json.dumps(result, ensure_ascii=False) + "\n")
		output.flush()
