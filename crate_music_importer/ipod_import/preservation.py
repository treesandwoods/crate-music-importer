"""Strict, read-only Music preservation snapshots. Unknown fields block execution."""
from __future__ import annotations

import hashlib
import json
import plistlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from crate_music_importer.ipod_import.music import _osascript, scan_music_library_for_health
from crate_music_importer.ipod_import.manifest import file_sha256

# JXA serializes strings without the lossy separators used by the matching index.
_SNAPSHOT = r'''
function run(argv) {
 const m = Application('com.apple.Music');
 const allTracks = m.libraryPlaylists[0].tracks();
 const selected = argv.length ? new Set(JSON.parse(argv[0])) : null;
 const ids = selected ? m.libraryPlaylists[0].tracks.persistentID() : [];
 const tracks = selected ? allTracks.filter((t, index) => selected.has(ids[index])) : allTracks;
 const fields = ['persistentID','databaseID','name','artist','album','albumArtist','duration','comment',
 'trackNumber','trackCount','discNumber','discCount','compilation','rating','favorited','playedCount','skippedCount',
 'albumFavorited','albumDisliked','disliked','playedDate','skippedDate','dateAdded','genre','composer',
 'grouping','year','lyrics','sortName','sortArtist','sortAlbum','sortAlbumArtist','sortComposer','volumeAdjustment',
 'start','finish','enabled','bookmark','bookmarkable','shufflable','eq','bpm','work','movement','movementNumber','movementCount'];
 const properties = selected ? tracks.map(t => t.properties()) : m.libraryPlaylists[0].tracks.properties();
 const rows = properties.map(p => {
   const out = {};
   Object.keys(p).sort().forEach(f => { if (!["class","id","index","location","modificationDate"].includes(f)) out[f] = p[f]; });
   fields.forEach(f => { if (!(f in p) && !['playedDate','skippedDate'].includes(f)) throw Error('Missing preservation field: ' + f); out[f] = f in p ? p[f] : null; });
   return out;
 });
 const playlists = m.userPlaylists().map(p => {
   const v = p.properties();
   return {persistent_id:p.persistentID(), name:p.name(), smart:p.smart(),
     parent: v.parent ? v.parent.persistentID() : null,
     tracks:p.tracks.persistentID()};
 });
 return JSON.stringify({properties:rows, playlists:playlists});
}
'''
_ARTWORK = r'''
on run argv
 set outFolder to item 1 of argv
 set resultRows to ""
 if count of argv > 1 then
  set allTracks to {}
  repeat with i from 2 to count of argv
   set wantedID to item i of argv
   tell application "Music" to set matches to every track of library playlist 1 whose persistent ID is wantedID
   if count of matches is not 1 then error "Exact Music identity is ambiguous or absent."
   set end of allTracks to item 1 of matches
  end repeat
 else
  tell application "Music" to set allTracks to every track of library playlist 1
 end if
 repeat with t in allTracks
  tell application "Music"
   set pid to persistent ID of t
   set n to count of artworks of t
  end tell
  set resultRows to resultRows & pid & ":" & n & linefeed
  repeat with i from 1 to n
   tell application "Music" to set bytes to raw data of artwork i of t
   set dest to POSIX file (outFolder & "/" & pid & "-" & i)
   set h to open for access dest with write permission
   try
    write bytes to h
    close access h
   on error messageText number errorNumber
    close access h
    error messageText number errorNumber
   end try
  end repeat
 end repeat
 return resultRows
end run
'''



def settings_evidence() -> dict[str, str]:
	from crate_music_importer.ipod_import.reconciliation import MUSIC_LIBRARY_PACKAGE
	from urllib.parse import unquote, urlparse
	prefs = Path.home() / "Library/Preferences/com.apple.Music.plist"
	data = plistlib.loads(prefs.read_bytes())
	active = Path(unquote(urlparse(str(data.get("library-url") or "")).path))
	if active.resolve() != MUSIC_LIBRARY_PACKAGE.resolve():
		raise ValueError("Configured Music library package does not match Music's active library.")
	files = [MUSIC_LIBRARY_PACKAGE / "Library Preferences.musicdb", MUSIC_LIBRARY_PACKAGE / "Preferences.plist", prefs]
	return {str(path): file_sha256(path) for path in files}


def record_settings_observation(paths: Any, *, keep_organized: bool, copy_files: bool, evidence: str) -> None:
	"""Save an explicit observation, bound to exact preference bytes and library."""
	from crate_music_importer.ipod_import.catalog import atomic_json
	if keep_organized or copy_files or not evidence:
		raise ValueError("Both Music organization options must be observed off.")
	atomic_json(paths.state_dir / "organization-settings-observation.json", {
		"settings": {"keep_organized": False, "copy_files": False}, "evidence": evidence,
		"preference_fingerprints": settings_evidence(),
	})


def read_organization_settings() -> dict[str, Any]:
	from crate_music_importer.ipod_import.manifest import ManagedPaths
	unknown = {"keep_organized": None, "copy_files": None}
	path = ManagedPaths().state_dir / "organization-settings-observation.json"
	try:
		receipt = json.loads(path.read_text())
		if receipt.get("preference_fingerprints") != settings_evidence():
			return unknown
		return receipt["settings"]
	except (OSError, ValueError, KeyError):
		return unknown


def capture_baseline(persistent_ids: list[str] | None = None, previous: dict[str, Any] | None = None) -> dict[str, Any]:
	if persistent_ids is not None and (not persistent_ids or not previous or not previous.get("complete")):
		raise ValueError("A batch check requires exact IDs and a preceding full baseline.")
	before = scan_music_library_for_health()
	result = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", _SNAPSHOT, *([json.dumps(persistent_ids)] if persistent_ids is not None else [])], capture_output=True, text=True, timeout=1800)
	if result.returncode:
		raise ValueError(result.stderr.strip())
	snapshot = json.loads(result.stdout)
	with tempfile.TemporaryDirectory(prefix="crate-artwork-") as directory:
		rows = _osascript(_ARTWORK, [directory, *(persistent_ids or [])], timeout=1800)
		artworks = {track["persistent_id"]: track["artwork_sha256"] for track in (previous or {}).get("tracks", [])} if persistent_ids is not None else {}
		artwork_ids = set()
		for row in rows.splitlines():
			pid, count = row.split(":")
			artwork_ids.add(pid)
			artworks[pid] = [file_sha256(Path(directory) / f"{pid}-{index}") for index in range(1, int(count) + 1)]
	after = scan_music_library_for_health()
	if sorted(before, key=lambda t: t["persistent_id"]) != sorted(after, key=lambda t: t["persistent_id"]):
		raise ValueError("Music changed during the preservation scan. Run a new preview.")
	properties = {track["persistent_id"]: track["preservation"] for track in (previous or {}).get("tracks", [])} if persistent_ids is not None else {}
	properties.update({value["persistentID"]: value for value in snapshot["properties"]})
	if persistent_ids is not None and (set(persistent_ids) != artwork_ids or set(persistent_ids) != {value["persistentID"] for value in snapshot["properties"]}):
		raise ValueError("Batch preservation scan omitted a requested Music identity.")
	if len(properties) != len(before) or set(properties) != set(artworks):
		raise ValueError("Preservation fields or artwork do not cover every Music entry.")
	for track in before:
		track["preservation"] = properties[track["persistent_id"]]
		track["artwork_sha256"] = artworks[track["persistent_id"]]
	return {"tracks": before, "playlists": snapshot["playlists"], "settings": read_organization_settings(), "complete": True}


def digest(value: Any) -> str:
	return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def assert_preserved(baseline: dict[str, Any], actual: dict[str, Any], locations: dict[str, str]) -> None:
	if not baseline.get("complete") or not actual.get("complete"):
		raise ValueError("A complete preservation baseline is required.")
	if actual.get("settings") != {"keep_organized": False, "copy_files": False}:
		raise ValueError("Both Music organization settings must be verified off.")
	if baseline.get("playlists") != actual.get("playlists"):
		raise ValueError("Music playlist membership, order, or organization changed.")
	def index(snapshot: dict[str, Any]) -> dict[str, Any]:
		rows = snapshot["tracks"]
		result = {row["persistent_id"]: dict(row) for row in rows}
		if len(result) != len(rows):
			raise ValueError("Duplicate Music persistent IDs in preservation snapshot.")
		return result
	wanted, got = index(baseline), index(actual)
	if set(wanted) != set(got):
		raise ValueError("Music persistent IDs changed.")
	for pid, expected in wanted.items():
		expected["location"] = locations.get(pid, expected.get("location"))
		if expected != got[pid]:
			raise ValueError(f"Music preservation verification failed for {pid}.")


class PreservationReader:
	"""Full start/end snapshots with exact-track batch reads and global playlists."""
	def __init__(self) -> None:
		self.previous: dict[str, Any] | None = None

	def __call__(self) -> dict[str, Any]:
		self.previous = capture_baseline()
		return self.previous

	def for_tracks(self, persistent_ids: list[str]) -> dict[str, Any]:
		self.previous = capture_baseline(persistent_ids, self.previous)
		return self.previous
