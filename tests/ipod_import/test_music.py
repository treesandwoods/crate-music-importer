import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import.music import (
	MusicAutomationError,
	MusicIndex,
	_ADD_FILE_SCRIPT,
	_DELETE_MANAGED_TRACK_SCRIPT,
	_EDIT_PLAYLIST_MEMBERSHIP_SCRIPT,
	_LOOKUP_TRACK_BY_ID_SCRIPT,
	_PLAYLIST_MEMBERSHIP_SCRIPT,
	_SCAN_PLAYLIST_IMPORTS_SCRIPT,
	_SCAN_SCRIPT,
	_UPDATE_ALBUM_TRACK_SCRIPT,
	_UPDATE_MANAGED_ARTWORK_SCRIPT,
	_music_location_to_posix,
	_osascript,
	delete_managed_music_track,
	edit_music_playlist_membership,
	import_managed_file,
	lookup_music_track,
	playlist_membership,
	reconcile_recording_music,
	scan_playlist_imports,
	update_managed_music_artwork,
	verify_music_tracks,
)


def source_recording(binding: str | None = None) -> dict:
	return {
		"recording_id": "rec_song",
		"source_metadata": {"title": "Song", "artists": "Artist", "album": "Album", "duration_ms": 180000},
		"music_binding": {"persistent_id": binding} if binding else None,
	}


def music_track(persistent_id: str, album: str = "Album", comment: str = "") -> dict:
	return {"persistent_id": persistent_id, "title": "Song", "artist": "Artist", "album": album, "duration_s": 180, "comment": comment, "location": f"/Music/{persistent_id}.mp3"}


class MusicAutomationTests(unittest.TestCase):
	def test_scan_and_path_conversion(self):
		self.assertIn("with timeout of 600 seconds", _SCAN_SCRIPT)
		self.assertIn("location of every track of library playlist 1", _SCAN_SCRIPT)
		self.assertEqual(_music_location_to_posix("Macintosh HD:Users:example:Music:Song.mp3"), "/Users/example/Music/Song.mp3")

	def test_exact_lookup_uses_only_requested_id(self):
		separator = chr(31)
		row = separator.join(["Song", "Artist", "Album", "180", "PID", "1", "/Music/Song.mp3", "", "Artist", "1", "1", "1", "1", "false"])
		with patch("crate_music_importer.ipod_import.music._osascript", return_value=row + chr(30)) as run_script:
			self.assertEqual(lookup_music_track("PID")["persistent_id"], "PID")
		run_script.assert_called_once_with(_LOOKUP_TRACK_BY_ID_SCRIPT, ["PID"], timeout=60)

	def test_import_does_not_write_comments(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "track.mp3"
			path.write_bytes(b"mp3")
			with patch("crate_music_importer.ipod_import.music._osascript", return_value="PID\t2\t/Music/track.mp3") as run_script:
				self.assertEqual(import_managed_file(path)["persistent_id"], "PID")
			run_script.assert_called_once_with(_ADD_FILE_SCRIPT, [str(path)])
			self.assertNotIn("set comment", _ADD_FILE_SCRIPT)

	def test_optional_recording_marker_is_only_a_high_confidence_hint(self):
		recording = source_recording()
		result = reconcile_recording_music(recording, MusicIndex([music_track("PID", comment="recording_id=rec_song")]))
		self.assertEqual(result["status"], "resolved")
		self.assertEqual(recording["music_binding"], {"persistent_id": "PID"})
		recording = source_recording()
		candidate = music_track("WRONG", comment="recording_id=rec_song")
		candidate["title"] = "Different"
		self.assertEqual(reconcile_recording_music(recording, MusicIndex([candidate]))["status"], "missing")

	def test_stale_binding_repairs_to_unique_current_match(self):
		recording = source_recording("STALE")
		result = reconcile_recording_music(recording, MusicIndex([music_track("CURRENT")]))
		self.assertEqual((result["status"], result["binding_changed"]), ("resolved", True))
		self.assertEqual(recording["music_binding"], {"persistent_id": "CURRENT"})

	def test_saved_id_uses_accepted_local_duration_with_matching_audio_baseline(self):
		recording = source_recording("PID")
		recording["source_metadata"]["duration_ms"] = 318000
		recording["managed_file"] = {"audio_sha256": "accepted-audio"}
		recording["local_preferences"] = {"accepted_duration": {"duration_ms": 259000, "audio_sha256": "accepted-audio"}}
		candidate = music_track("PID") | {"duration_s": 259}
		self.assertEqual(reconcile_recording_music(recording, MusicIndex([candidate]))["status"], "resolved")
		recording["local_preferences"]["accepted_duration"]["audio_sha256"] = "different-audio"
		self.assertEqual(reconcile_recording_music(recording, MusicIndex([candidate]))["status"], "missing")
		self.assertEqual(reconcile_recording_music(recording, MusicIndex([candidate | {"title": "Different"}]))["status"], "missing")

	def test_saved_album_position_accepts_metadata_mismatch_from_album_preview(self):
		recording = source_recording("GETZ")
		recording["source_metadata"].update(title="Doralice", artists="Stan Getz, João Gilberto", original_album="Getz/Gilberto", duration_ms=166266)
		recording["album_metadata"] = {"album": "Getz/Gilberto", "track_no": 2, "disc_no": 1}
		candidate = music_track("GETZ", "Stan Getz") | {"title": "Doralice", "artist": "Getz/Gilberto", "album_artist": "Getz/Gilberto", "track_no": 2, "disc_no": 0, "duration_s": 166.296}
		self.assertEqual(reconcile_recording_music(recording, MusicIndex([candidate]), preferred_album="Getz/Gilberto")["status"], "resolved")
		self.assertEqual(reconcile_recording_music(recording, MusicIndex([candidate | {"track_no": 3}]))["status"], "missing")
		self.assertEqual(reconcile_recording_music(recording, MusicIndex([candidate | {"duration_s": 120}]))["status"], "missing")

	def test_saved_album_position_accepts_different_edition_duration(self):
		recording = source_recording("PID")
		recording["album_metadata"] = {"album": "Album", "track_no": 1, "disc_no": 1}
		candidate = music_track("PID") | {"track_no": 1, "duration_s": 150}
		self.assertEqual(reconcile_recording_music(recording, MusicIndex([candidate]))["status"], "resolved")
		self.assertEqual(reconcile_recording_music(recording, MusicIndex([candidate | {"artist": "Other"}]))["status"], "missing")

	def test_requested_album_copy_wins_over_other_copy(self):
		recording = source_recording("LOOSE")
		result = reconcile_recording_music(recording, MusicIndex([music_track("LOOSE", "Playlist Imports"), music_track("ALBUM")]), preferred_album="Album")
		self.assertEqual(result["status"], "resolved")
		self.assertEqual(recording["music_binding"], {"persistent_id": "ALBUM"})

	def test_ambiguous_matches_are_not_bound(self):
		recording = source_recording()
		result = reconcile_recording_music(recording, MusicIndex([music_track("ONE"), music_track("TWO")]))
		self.assertEqual(result["status"], "ambiguous")
		self.assertIsNone(recording["music_binding"])

	def test_file_mutations_require_exact_registered_path(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "track.mp3"
			path.write_bytes(b"mp3")
			artwork = Path(directory) / "art.jpg"
			artwork.write_bytes(b"jpg")
			with patch("crate_music_importer.ipod_import.music._osascript", return_value="DELETED") as run_script:
				self.assertTrue(delete_managed_music_track("PID", path))
			run_script.assert_called_once_with(_DELETE_MANAGED_TRACK_SCRIPT, ["PID", str(path)], timeout=60)
			with patch("crate_music_importer.ipod_import.music._osascript", return_value="OK") as run_script:
				update_managed_music_artwork("PID", path, artwork)
			run_script.assert_called_once_with(_UPDATE_MANAGED_ARTWORK_SCRIPT, ["PID", str(path), str(artwork)])
			self.assertIn("actualPath is not expectedPath", _UPDATE_ALBUM_TRACK_SCRIPT)

	def test_targeted_album_check_and_post_import_verification(self):
		separator = chr(31)
		row = separator.join(["Song", "Artist", "Playlist Imports", "180", "PID", "1", "/Music/Song.mp3", "", "Artist", "1", "1", "1", "1", "false"])
		with patch("crate_music_importer.ipod_import.music._osascript", return_value=row + chr(30)) as run_script:
			self.assertEqual(scan_playlist_imports(["PID", "PID"])[0]["persistent_id"], "PID")
		run_script.assert_called_once_with(_SCAN_PLAYLIST_IMPORTS_SCRIPT, ["PID"], timeout=300)
		with patch("crate_music_importer.ipod_import.music.time.sleep") as sleep, patch("crate_music_importer.ipod_import.music.scan_playlist_imports", return_value=[{"persistent_id": "WANTED"}]):
			self.assertEqual(verify_music_tracks(["WANTED"], settle_seconds=1.5), {"WANTED": {"persistent_id": "WANTED"}})
		sleep.assert_called_once_with(1.5)

	def test_playlist_membership_and_guarded_edit_preserve_duplicates(self):
		with patch("crate_music_importer.ipod_import.music._osascript", return_value="OK\tA\x1fB\x1fA"):
			self.assertEqual(playlist_membership("Saved", "PLAYLIST-PID"), ["A", "B", "A"])
		with patch("crate_music_importer.ipod_import.music._osascript", return_value="OK\tMANUAL\x1fA\x1fC"):
			self.assertEqual(edit_music_playlist_membership("Saved", "PLAYLIST-PID", ["MANUAL", "A", "B"], [2], ["C"]), ["MANUAL", "A", "C"])
		self.assertIn("Playlist membership changed before the guarded update", _EDIT_PLAYLIST_MEMBERSHIP_SCRIPT)
		self.assertIn("persistent ID of candidate", _PLAYLIST_MEMBERSHIP_SCRIPT)

	def test_automation_errors_remain_clear(self):
		result = subprocess.CompletedProcess([], 1, "", "execution error: Not authorized to send Apple events. (-1743)")
		with patch("crate_music_importer.ipod_import.music.subprocess.run", return_value=result):
			with self.assertRaisesRegex(MusicAutomationError, "Automation"):
				_osascript("return 1")


if __name__ == "__main__":
	unittest.main()
