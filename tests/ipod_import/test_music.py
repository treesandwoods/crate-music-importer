import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import.music import (
	MusicAutomationError,
	_ADD_FILE_SCRIPT,
	_DELETE_IMPORTER_OWNED_TRACK_SCRIPT,
	_EDIT_PLAYLIST_MEMBERSHIP_SCRIPT,
	_LOOKUP_TRACK_BY_RECORDING_ID_SCRIPT,
	_LOOKUP_TRACK_BY_ID_SCRIPT,
	_SCAN_PLAYLIST_IMPORTS_SCRIPT,
	_SCAN_SCRIPT,
	_PLAYLIST_MEMBERSHIP_SCRIPT,
	_UPDATE_ALBUM_TRACK_SCRIPT,
	_UPDATE_MANAGED_ARTWORK_SCRIPT,
	_osascript,
	_music_location_to_posix,
	delete_importer_owned_music_track,
	edit_music_playlist_membership,
	import_managed_file,
	lookup_importer_owned_music_track,
	lookup_music_track,
	playlist_membership,
	scan_playlist_imports,
	update_managed_music_artwork,
	verify_music_tracks,
)


class MusicAutomationTests(unittest.TestCase):
	def test_scan_allows_music_more_than_the_default_apple_event_timeout(self):
		self.assertIn("with timeout of 600 seconds", _SCAN_SCRIPT)
		self.assertIn("location of every track of library playlist 1", _SCAN_SCRIPT)
		self.assertNotIn("POSIX path of trackLocationAlias", _SCAN_SCRIPT)
		self.assertNotIn("POSIX path of (location of", _SCAN_SCRIPT)
		self.assertEqual(_music_location_to_posix("Macintosh HD:Users:example:Music:Song.mp3"), "/Users/example/Music/Song.mp3")
		self.assertEqual(_music_location_to_posix("Macintosh HD:Users:example:Music:Album/ Live:Song.mp3"), "/Users/example/Music/Album: Live/Song.mp3")

	def test_targeted_album_check_reads_only_playlist_imports_and_saved_ids(self):
		separator = chr(31)
		row = separator.join(["Song", "Artist", "Playlist Imports", "180", "PID", "1", "/Music/Song.mp3", "recording_id=x", "Artist", "1", "1", "1", "1", "false"])
		with patch("crate_music_importer.ipod_import.music._osascript", return_value=row + chr(30)) as run_script:
			tracks = scan_playlist_imports(["PID", "PID"])
		self.assertEqual([track["persistent_id"] for track in tracks], ["PID"])
		run_script.assert_called_once_with(_SCAN_PLAYLIST_IMPORTS_SCRIPT, ["PID"], timeout=300)
		self.assertIn('whose album is "Playlist Imports"', _SCAN_PLAYLIST_IMPORTS_SCRIPT)
		self.assertNotIn("every track of library playlist 1\n", _SCAN_PLAYLIST_IMPORTS_SCRIPT)

	def test_album_upgrade_sets_music_genre(self):
		self.assertIn('set genre of targetTrack to "Music"', _UPDATE_ALBUM_TRACK_SCRIPT)

	def test_playlist_artwork_update_requires_importer_ownership_marker(self):
		artwork = Path("/tmp/artwork.jpg")
		with patch("crate_music_importer.ipod_import.music._osascript", return_value="OK") as run_script:
			update_managed_music_artwork("PID", "recording-one", artwork)
		run_script.assert_called_once_with(
			_UPDATE_MANAGED_ARTWORK_SCRIPT,
			["PID", "recording_id=recording-one", str(artwork)],
		)
		self.assertIn("does not contain recordingToken", _UPDATE_MANAGED_ARTWORK_SCRIPT)

	def test_exact_lookup_uses_only_the_requested_persistent_id(self):
		separator = chr(31)
		row = separator.join(["Song", "Artist", "Album", "180", "PID", "1", "/Music/Song.mp3", "", "Artist", "1", "1", "1", "1", "false"])
		with patch("crate_music_importer.ipod_import.music._osascript", return_value=row + chr(30)) as run_script:
			result = lookup_music_track("PID")
		self.assertEqual(result["persistent_id"], "PID")
		run_script.assert_called_once_with(_LOOKUP_TRACK_BY_ID_SCRIPT, ["PID"], timeout=60)
		self.assertIn("whose persistent ID is requestedID", _LOOKUP_TRACK_BY_ID_SCRIPT)
		self.assertNotIn("every track of library playlist 1\n", _LOOKUP_TRACK_BY_ID_SCRIPT)

	def test_importer_owned_lookup_uses_the_recording_marker(self):
		separator = chr(31)
		row = separator.join(["Song", "Artist", "Album", "180", "PID", "1", "/Music/Song.mp3", "Managed; recording_id=rec_one"])
		with patch("crate_music_importer.ipod_import.music._osascript", return_value=row + chr(30)) as run_script:
			result = lookup_importer_owned_music_track("rec_one")
		self.assertEqual(result["persistent_id"], "PID")
		run_script.assert_called_once_with(_LOOKUP_TRACK_BY_RECORDING_ID_SCRIPT, ["rec_one"], timeout=60)
		self.assertIn("whose comment contains ownershipMarker", _LOOKUP_TRACK_BY_RECORDING_ID_SCRIPT)

	def test_import_sets_the_crate_music_importer_ownership_comment(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "track.mp3"
			path.write_bytes(b"mp3")
			with patch("crate_music_importer.ipod_import.music._osascript", return_value="PID\t2\t/Music/track.mp3") as run_script:
				result = import_managed_file(path, "rec_one")
		self.assertEqual(result["persistent_id"], "PID")
		run_script.assert_called_once_with(
			_ADD_FILE_SCRIPT,
			[str(path), "Managed by Crate Music Importer; recording_id=rec_one"],
		)
		self.assertIn("set comment of importedTrack to ownershipComment", _ADD_FILE_SCRIPT)

	def test_post_import_verification_returns_only_requested_ids(self):
		with patch("crate_music_importer.ipod_import.music.time.sleep") as sleep, \
			patch("crate_music_importer.ipod_import.music.scan_playlist_imports", return_value=[
				{"persistent_id": "WANTED"}, {"persistent_id": "PLAYLIST-EXTRA"},
			]):
			result = verify_music_tracks(["WANTED"], settle_seconds=1.5)
		self.assertEqual(result, {"WANTED": {"persistent_id": "WANTED"}})
		sleep.assert_called_once_with(1.5)

	def test_deletion_requires_an_exact_id_and_importer_marker(self):
		with patch("crate_music_importer.ipod_import.music._osascript", return_value="DELETED") as run_script:
			self.assertTrue(delete_importer_owned_music_track("PID", "rec_one"))
		run_script.assert_called_once_with(_DELETE_IMPORTER_OWNED_TRACK_SCRIPT, ["PID", "rec_one"], timeout=60)
		self.assertIn("whose persistent ID is requestedID", _DELETE_IMPORTER_OWNED_TRACK_SCRIPT)
		self.assertIn("does not contain ownershipMarker", _DELETE_IMPORTER_OWNED_TRACK_SCRIPT)

	def test_playlist_membership_reads_only_the_saved_playlist(self):
		with patch("crate_music_importer.ipod_import.music._osascript", return_value="OK\tA\x1fB\x1fA") as run_script:
			self.assertEqual(playlist_membership("Saved", "PLAYLIST-PID"), ["A", "B", "A"])
		run_script.assert_called_once_with(_PLAYLIST_MEMBERSHIP_SCRIPT, ["Saved", "PLAYLIST-PID"], timeout=120)
		self.assertIn("persistent ID of candidate", _PLAYLIST_MEMBERSHIP_SCRIPT)

	def test_guarded_playlist_edit_preserves_survivors_and_appends(self):
		with patch("crate_music_importer.ipod_import.music._osascript", return_value="OK\tMANUAL\x1fA\x1fC") as run_script:
			result = edit_music_playlist_membership("Saved", "PLAYLIST-PID", ["MANUAL", "A", "B"], [2], ["C"])
		self.assertEqual(result, ["MANUAL", "A", "C"])
		self.assertEqual(run_script.call_args.args[1], ["Saved", "PLAYLIST-PID", "MANUAL\x1fA\x1fB", "2", "C"])
		self.assertIn("Playlist membership changed before the guarded update", _EDIT_PLAYLIST_MEMBERSHIP_SCRIPT)
		self.assertLess(_EDIT_PLAYLIST_MEMBERSHIP_SCRIPT.index("set sourceTracks to {}"), _EDIT_PLAYLIST_MEMBERSHIP_SCRIPT.index("repeat with removalValue"))
		self.assertNotIn("delete every track of targetPlaylist", _EDIT_PLAYLIST_MEMBERSHIP_SCRIPT)

	def test_permission_error_names_automation_settings(self):
		result = subprocess.CompletedProcess([], 1, "", "execution error: Not authorized to send Apple events. (-1743)")
		with patch("crate_music_importer.ipod_import.music.subprocess.run", return_value=result):
			with self.assertRaisesRegex(MusicAutomationError, "Automation"):
				_osascript("return 1")

	def test_parameter_error_is_not_mislabeled_as_permission_failure(self):
		result = subprocess.CompletedProcess([], 1, "", "Music got an error: Parameter error. (-50)")
		with patch("crate_music_importer.ipod_import.music.subprocess.run", return_value=result):
			with self.assertRaisesRegex(MusicAutomationError, "not an Automation-permission error"):
				_osascript("return 1")

	def test_timeout_explains_that_music_may_be_busy(self):
		result = subprocess.CompletedProcess([], 1, "", "Music got an error: AppleEvent timed out. (-1712)")
		with patch("crate_music_importer.ipod_import.music.subprocess.run", return_value=result):
			with self.assertRaisesRegex(MusicAutomationError, "Music was busy"):
				_osascript("return 1")

if __name__ == "__main__":
	unittest.main()
