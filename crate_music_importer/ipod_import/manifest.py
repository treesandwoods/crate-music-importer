"""Persistent, atomic state for recordings and Spotify playlist membership."""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from crate_music_importer.ipod_import.constants import MANAGED_ROOT, MANIFEST_VERSION
from crate_music_importer.ipod_import.identity import canonical_artist, clean_release_labels, normalize_recording_title, recording_id, recording_identity


_YOUTUBE_SELECTION_ERRORS = {
	"No YouTube result scored strictly above 0.87.",
	"No verified YouTube recording met the automatic identity and duration requirements.",
}

_SEEDED_PLAYLIST_URLS = {
	"7e9ZbYY2MshqF4APO1GdMm": "https://open.spotify.com/playlist/7e9ZbYY2MshqF4APO1GdMm",
	"6iV3fAswdxYDitegoVjo72": "https://open.spotify.com/playlist/6iV3fAswdxYDitegoVjo72",
	"1CZPh4vz1diXjmNw3b3HgK": "https://open.spotify.com/playlist/1CZPh4vz1diXjmNw3b3HgK",
}


def seed_known_playlist_urls(manifest: dict[str, Any]) -> int:
	"""Backfill the three explicitly supplied legacy playlist links."""
	updated = 0
	for playlist_id, url in _SEEDED_PLAYLIST_URLS.items():
		playlist = manifest.get("playlists", {}).get(playlist_id)
		if isinstance(playlist, dict) and not playlist.get("spotify_url"):
			playlist["spotify_url"] = url
			updated += 1
	return updated


def _now() -> str:
	return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_name(value: str, *, limit: int = 80) -> str:
	text = re.sub(r"[^\w .()\[\]-]+", " ", str(value or ""), flags=re.UNICODE)
	text = re.sub(r"\s+", " ", text).strip(" .") or "Untitled"
	return text[:limit].rstrip(" .")


@dataclass(frozen=True)
class ManagedPaths:
	root: Path = MANAGED_ROOT

	@property
	def state_dir(self) -> Path:
		return self.root / ".state"

	@property
	def manifest(self) -> Path:
		return self.state_dir / "manifest.json"

	@property
	def music_cache(self) -> Path:
		return self.state_dir / "music-library-cache.json"

	@property
	def staging(self) -> Path:
		return self.root / ".staging"

	@property
	def tracks(self) -> Path:
		return self.root / "tracks"

	@property
	def playlists(self) -> Path:
		return self.root / "playlists"

	def create(self) -> None:
		for directory in (self.state_dir, self.staging, self.tracks, self.playlists):
			directory.mkdir(parents=True, exist_ok=True)


def new_manifest(paths: ManagedPaths) -> dict[str, Any]:
	return {
		"version": MANIFEST_VERSION,
		"managed_root": str(paths.root),
		"created_at": _now(),
		"updated_at": _now(),
		"recordings": {},
		"playlists": {},
		"albums": {},
		"manual_youtube_overrides": {},
	}


def _load_manifest_unlocked(paths: ManagedPaths) -> dict[str, Any]:
	if not paths.manifest.exists():
		return new_manifest(paths)
	with paths.manifest.open("r", encoding="utf-8") as handle:
		data = json.load(handle)
	if data.get("version") != MANIFEST_VERSION:
		raise ValueError(f"Unsupported manifest version: {data.get('version')!r}")
	if Path(str(data.get("managed_root"))) != paths.root:
		raise ValueError("Manifest managed_root does not match the required output directory.")
	if not isinstance(data.get("recordings"), dict) or not isinstance(data.get("playlists"), dict):
		raise ValueError("Manifest is missing recordings or playlists.")
	data.setdefault("albums", {})
	if not isinstance(data["albums"], dict):
		raise ValueError("Manifest albums must be an object.")
	for recording in data["recordings"].values():
		if isinstance(recording, dict):
			recording.setdefault("album_metadata", None)
			recording.setdefault("album_memberships", {})
	seed_known_playlist_urls(data)
	return data


def load_manifest(paths: ManagedPaths) -> dict[str, Any]:
	return _load_manifest_unlocked(paths)


@contextlib.contextmanager
def _manifest_lock(paths: ManagedPaths):
	paths.state_dir.mkdir(parents=True, exist_ok=True)
	with (paths.state_dir / "manifest.lock").open("a+", encoding="utf-8") as handle:
		fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
		try:
			yield
		finally:
			fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _apply_manual_youtube_overrides(manifest: dict[str, Any], overrides: dict[str, Any]) -> None:
	recordings = manifest.get("recordings") or {}
	kept: dict[str, Any] = {}
	for recording_id, candidate in overrides.items():
		recording = recordings.get(recording_id)
		if not isinstance(recording, dict) or not isinstance(candidate, dict):
			continue
		chosen = copy.deepcopy(candidate)
		recording["youtube"] = chosen
		if str((recording.get("review") or {}).get("kind") or "") in ("youtube_missing", "youtube_ambiguity"):
			recording.pop("review", None)
		if recording.get("last_error") in _YOUTUBE_SELECTION_ERRORS:
			recording.pop("last_error", None)
			recording.pop("last_error_source", None)
		kept[recording_id] = chosen
	manifest["manual_youtube_overrides"] = kept


def refresh_manual_youtube_overrides(paths: ManagedPaths, manifest: dict[str, Any]) -> None:
	"""Merge choices saved by Raycast into a long-running import's in-memory plan."""
	with _manifest_lock(paths):
		latest = _load_manifest_unlocked(paths)
	_apply_manual_youtube_overrides(manifest, latest.get("manual_youtube_overrides") or {})


def _save_manifest_unlocked(paths: ManagedPaths, manifest: dict[str, Any]) -> None:
	paths.create()
	manifest["updated_at"] = _now()
	with tempfile.NamedTemporaryFile(
		"w",
		encoding="utf-8",
		dir=paths.state_dir,
		prefix="manifest-",
		suffix=".tmp",
		delete=False,
	) as handle:
		json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
		handle.write("\n")
		temporary = Path(handle.name)
	os.replace(temporary, paths.manifest)


def save_manifest(paths: ManagedPaths, manifest: dict[str, Any]) -> None:
	with _manifest_lock(paths):
		latest = _load_manifest_unlocked(paths)
		overrides = dict(latest.get("manual_youtube_overrides") or {})
		overrides.update(manifest.get("manual_youtube_overrides") or {})
		_apply_manual_youtube_overrides(manifest, overrides)
		_save_manifest_unlocked(paths, manifest)


def update_manifest(paths: ManagedPaths, update: Callable[[dict[str, Any]], Any]) -> Any:
	"""Apply one targeted mutation without replacing concurrent importer progress."""
	with _manifest_lock(paths):
		manifest = _load_manifest_unlocked(paths)
		result = update(manifest)
		_apply_manual_youtube_overrides(manifest, manifest.get("manual_youtube_overrides") or {})
		_save_manifest_unlocked(paths, manifest)
		return result


def clone_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
	return copy.deepcopy(manifest)


def _same_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
	if normalize_recording_title(left.get("title")) != normalize_recording_title(right.get("title")):
		return False
	if left.get("artists") != right.get("artists"):
		return False
	left_versions = [value for value in left.get("versions", []) if value != "remaster" and not str(value).startswith("remaster-year:")]
	right_versions = [value for value in right.get("versions", []) if value != "remaster" and not str(value).startswith("remaster-year:")]
	if left_versions != right_versions:
		return False
	left_duration = int(left.get("duration_bucket_ms") or 0)
	right_duration = int(right.get("duration_bucket_ms") or 0)
	return not left_duration or not right_duration or abs(left_duration - right_duration) <= 4000


def find_recording(manifest: dict[str, Any], track: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
	spotify_id = str(track.get("sp_id") or "")
	isrc = str(track.get("isrc") or "").upper()
	identity = recording_identity(track)
	for key, recording in manifest["recordings"].items():
		if spotify_id and spotify_id in recording.get("spotify_ids", []):
			return key, recording
		if isrc and isrc in recording.get("isrcs", []):
			return key, recording
	for key, recording in manifest["recordings"].items():
		if _same_identity(identity, recording.get("normalized_identity", {})):
			return key, recording
	return None


def upsert_recording(
	manifest: dict[str, Any],
	track: dict[str, Any],
	*,
	source_type: str = "playlist",
) -> tuple[str, dict[str, Any]]:
	raw_title = str(track.get("title") or "")
	raw_album = str(track.get("album") or "")
	clean_track = dict(track)
	clean_track["title"] = clean_release_labels(raw_title)
	clean_track["album"] = clean_release_labels(raw_album)
	clean_track["artists"] = canonical_artist(track.get("artists"))
	clean_track["album_artist"] = canonical_artist(track.get("album_artist"))
	found = find_recording(manifest, clean_track)
	key = found[0] if found else recording_id(clean_track)
	if found:
		recording = found[1]
	else:
		recording = {
			"recording_id": key,
			"normalized_identity": recording_identity(clean_track),
			"spotify_ids": [],
			"isrcs": [],
			"source_occurrences": [],
			"source_metadata": {},
			"album_metadata": None,
			"youtube": None,
			"managed_file": None,
			"music": None,
			"active_reference": None,
			"playlist_memberships": {},
			"album_memberships": {},
			"promotion": {
				"eligible": True,
				"superseded_by_music_persistent_id": None,
				"tool_owned_loose_file_retirement_candidate": False,
				"retired_at": None,
			},
			"created_at": _now(),
			"updated_at": _now(),
		}
		manifest["recordings"][key] = recording
	recording.setdefault("album_metadata", None)
	recording.setdefault("album_memberships", {})
	recording["normalized_identity"] = recording_identity(clean_track)
	spotify_id = str(track.get("sp_id") or "")
	if spotify_id and spotify_id not in recording["spotify_ids"]:
		recording["spotify_ids"].append(spotify_id)
	isrc = str(track.get("isrc") or "").upper()
	if isrc and isrc not in recording["isrcs"]:
		recording["isrcs"].append(isrc)
	if source_type == "album":
		recording["album_metadata"] = {
			"spotify_album_id": str(track.get("album_id") or ""),
			"album": clean_track.get("album") or "",
			"album_artist": clean_track.get("album_artist") or clean_track.get("artists") or "",
			"track_no": int(track.get("track_no") or track.get("position") or 0),
			"track_total": int(track.get("track_total") or 0),
			"disc_no": int(track.get("disc_no") or 1),
			"disc_total": int(track.get("disc_total") or 1),
			"release_year": int(track.get("release_year") or 0) or None,
			"cover_url": track.get("cover_url"),
			"is_compilation": normalize_recording_title(track.get("album_artist")) == "various artists",
		}
	prior_metadata = recording.get("source_metadata") or {}
	album_metadata = recording.get("album_metadata") or {}
	preferences = recording.get("local_preferences") or {}
	recording["source_metadata"] = {
		"title": preferences.get("title") or clean_track.get("title") or prior_metadata.get("title") or "",
		"artists": clean_track.get("artists") or canonical_artist(prior_metadata.get("artists")),
		"original_album": album_metadata.get("album") or clean_track.get("album") or prior_metadata.get("original_album") or "",
		"duration_ms": int(track.get("duration_ms") or prior_metadata.get("duration_ms") or 0),
		"cover_url": album_metadata.get("cover_url") or track.get("cover_url") or prior_metadata.get("cover_url"),
	}
	occurrence = {
		"source_type": source_type,
		"spotify_id": spotify_id or None,
		"isrc": isrc or None,
		"title": clean_track.get("title") or "",
		"artists": clean_track.get("artists") or "",
		"original_album": clean_track.get("album") or "",
		"duration_ms": int(track.get("duration_ms") or 0),
		"cover_url": track.get("cover_url"),
	}
	if raw_title and raw_title != clean_track["title"]:
		occurrence["spotify_title"] = raw_title
	if raw_album and raw_album != clean_track["album"]:
		occurrence["spotify_album"] = raw_album
	occurrences = recording.setdefault("source_occurrences", [])
	if occurrence not in occurrences:
		occurrences.append(occurrence)
	recording["updated_at"] = _now()
	return key, recording


def set_playlist(
	manifest: dict[str, Any],
	playlist: dict[str, Any],
	items: list[dict[str, Any]],
) -> dict[str, Any]:
	playlist_id = str(playlist["id"])
	prior = manifest["playlists"].get(playlist_id, {})
	m3u8_relative_path = f"playlists/{safe_name(playlist.get('name') or 'Spotify Playlist')}--{playlist_id}.m3u8"
	entry = {
		"spotify_playlist_id": playlist_id,
		"spotify_url": playlist.get("url") or "",
		"name": playlist.get("name") or "Spotify Playlist",
		"total_count": playlist.get("total_count"),
		"complete": bool(playlist.get("complete", True)),
		"warning": playlist.get("warning"),
		"items": items,
		"m3u8_relative_path": m3u8_relative_path,
		"m3u8_tool_owned": bool(prior.get("m3u8_tool_owned", False) and prior.get("m3u8_relative_path") == m3u8_relative_path),
		"music_playlist_persistent_id": prior.get("music_playlist_persistent_id"),
		"created_at": prior.get("created_at") or _now(),
		"updated_at": _now(),
	}
	manifest["playlists"][playlist_id] = entry
	for recording in manifest["recordings"].values():
		recording.get("playlist_memberships", {}).pop(playlist_id, None)
	positions: dict[str, list[int]] = {}
	for item in items:
		positions.setdefault(item["recording_id"], []).append(int(item["position"]))
	for key, recording_positions in positions.items():
		manifest["recordings"][key]["playlist_memberships"][playlist_id] = recording_positions
	return entry


def set_album(
	manifest: dict[str, Any],
	album: dict[str, Any],
	items: list[dict[str, Any]],
) -> dict[str, Any]:
	album_id = str(album["id"])
	albums = manifest.setdefault("albums", {})
	prior = albums.get(album_id, {})
	entry = {
		"spotify_album_id": album_id,
		"spotify_url": album.get("url") or "",
		"name": clean_release_labels(album.get("name")) or "Spotify Album",
		"album_artist": album.get("album_artist") or "",
		"release_year": album.get("release_year"),
		"total_count": album.get("total_count"),
		"complete": bool(album.get("complete", True)),
		"warning": album.get("warning"),
		"items": items,
		"created_at": prior.get("created_at") or _now(),
		"updated_at": _now(),
	}
	albums[album_id] = entry
	for recording in manifest["recordings"].values():
		recording.setdefault("album_memberships", {}).pop(album_id, None)
	for item in items:
		manifest["recordings"][item["recording_id"]].setdefault("album_memberships", {})[album_id] = {
			"position": int(item["position"]),
			"track_no": int(item.get("track_no") or item["position"]),
			"disc_no": int(item.get("disc_no") or 1),
		}
	return entry


def managed_relative_path(recording: dict[str, Any]) -> str:
	key = recording["recording_id"]
	meta = recording["source_metadata"]
	label = safe_name(f"{meta.get('artists', '')} - {meta.get('title', '')}", limit=100)
	return f"tracks/{key[4:6]}/{key}--{label}.mp3"


def file_sha256(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as handle:
		for chunk in iter(lambda: handle.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()


def playlist_m3u8(manifest: dict[str, Any], playlist_id: str, paths: ManagedPaths) -> str:
	playlist = manifest["playlists"][playlist_id]
	lines = ["#EXTM3U", f"#CRATE-MUSIC-IMPORTER:{playlist_id}", f"#PLAYLIST:{playlist['name']}"]
	for item in sorted(playlist["items"], key=lambda value: int(value["position"])):
		recording = manifest["recordings"][item["recording_id"]]
		active = recording.get("active_reference") or {}
		if active.get("kind") == "unavailable_historical_reference":
			lines.append(f"#UNAVAILABLE-HISTORICAL-RECORDING:{item['recording_id']}")
			continue
		meta = recording["source_metadata"]
		duration = int(round(int(meta.get("duration_ms") or 0) / 1000.0))
		lines.append(f"#EXTINF:{duration},{meta.get('artists', '')} - {meta.get('title', '')}")
		music = recording.get("music") or {}
		managed = recording.get("managed_file") or {}
		if active.get("kind") == "existing_music":
			location = music.get("location")
		else:
			location = str(paths.root / managed["relative_path"]) if managed.get("relative_path") else None
		if location:
			lines.append(str(location))
		else:
			persistent_id = music.get("persistent_id") or "UNRESOLVED"
			lines.append(f"#MUSIC-PERSISTENT-ID:{persistent_id}")
	return "\n".join(lines) + "\n"


def write_playlist_m3u8(manifest: dict[str, Any], playlist_id: str, paths: ManagedPaths) -> Path:
	playlist = manifest["playlists"][playlist_id]
	target = paths.root / playlist["m3u8_relative_path"]
	target.parent.mkdir(parents=True, exist_ok=True)
	if target.exists() and not playlist.get("m3u8_tool_owned"):
		try:
			header = target.read_text(encoding="utf-8", errors="replace").splitlines()[:3]
			recognized = any(
				line.startswith("#") and line.endswith(f"-IMPORTER:{playlist_id}")
				for line in header
			)
		except OSError:
			recognized = False
		if not recognized:
			raise FileExistsError(f"Refusing to overwrite an unregistered playlist file: {target}")
	content = playlist_m3u8(manifest, playlist_id, paths)
	with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, prefix="playlist-", suffix=".tmp", delete=False) as handle:
		handle.write(content)
		temporary = Path(handle.name)
	os.replace(temporary, target)
	playlist["m3u8_tool_owned"] = True
	return target
