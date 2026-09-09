import tempfile
import unittest
from pathlib import Path

from crate_music_importer.ipod_import.manifest import (
	ManagedPaths,
	new_manifest,
	playlist_m3u8,
	set_album,
	set_playlist,
	upsert_recording,
	write_playlist_m3u8,
)


class ManifestTests(unittest.TestCase):
	def test_user_title_survives_source_refresh(self):
		with tempfile.TemporaryDirectory() as directory:
			manifest = new_manifest(ManagedPaths(Path(directory)))
			track = {"title": "Song", "artists": "Artist", "duration_ms": 180000, "sp_id": "source-id"}
			key, recording = upsert_recording(manifest, track)
			recording["local_preferences"] = {"title": "My chosen title"}
			new_key, updated = upsert_recording(manifest, track)
			self.assertEqual(new_key, key)
			self.assertEqual(updated["source_metadata"]["title"], "My chosen title")
			self.assertEqual(updated["source_occurrences"][0]["title"], "Song")

	def test_display_cleanup_for_album_and_playlist_imports(self):
		for source_type in ("album", "playlist"):
			with self.subTest(source_type=source_type), tempfile.TemporaryDirectory() as directory:
				manifest = new_manifest(ManagedPaths(Path(directory)))
				track = {"title": "Song - 2009 Mix", "artists": "Yusuf / Cat Stevens", "album_artist": "Yusuf / Cat Stevens", "album": "Album", "duration_ms": 180000}
				key, recording = upsert_recording(manifest, track, source_type=source_type)
				self.assertEqual(recording["source_metadata"]["title"], "Song")
				self.assertEqual(recording["source_metadata"]["artists"], "Cat Stevens")
				if source_type == "album":
					self.assertEqual(recording["album_metadata"]["album_artist"], "Cat Stevens")
				other_key, _ = upsert_recording(manifest, dict(track, artists="Cat Stevens", title="Song"), source_type=source_type)
				self.assertEqual(key, other_key)

	def test_album_metadata_wins_without_creating_a_second_recording(self):
		with tempfile.TemporaryDirectory() as directory:
			manifest = new_manifest(ManagedPaths(Path(directory)))
			playlist_track = {
				"title": "Genesis",
				"artists": "Grimes",
				"album": "Visions",
				"duration_ms": 255000,
				"sp_id": "playlist-track-id",
				"isrc": "CA21O1100042",
			}
			album_track = dict(
				playlist_track,
				sp_id="album-track-id",
				album_id="spotify-album-id",
				album_artist="Grimes",
				track_no=2,
				track_total=13,
				disc_no=1,
				disc_total=1,
				release_year=2012,
			)
			playlist_key, _ = upsert_recording(manifest, playlist_track)
			album_key, recording = upsert_recording(manifest, album_track, source_type="album")
			self.assertEqual(playlist_key, album_key)
			self.assertEqual(len(manifest["recordings"]), 1)
			self.assertEqual(recording["album_metadata"]["album"], "Visions")
			self.assertEqual(recording["album_metadata"]["track_no"], 2)
			self.assertEqual(recording["source_metadata"]["original_album"], "Visions")

			set_album(manifest, {
				"id": "spotify-album-id",
				"name": "Visions",
				"album_artist": "Grimes",
				"url": "url",
				"total_count": 1,
				"complete": True,
			}, [{
				"position": 1,
				"recording_id": album_key,
				"spotify_id": "album-track-id",
				"track_no": 2,
				"disc_no": 1,
				"status": "managed_ready",
			}])
			self.assertIn("spotify-album-id", manifest["albums"])
			self.assertEqual(recording["album_memberships"]["spotify-album-id"]["track_no"], 2)

	def test_isrc_merges_different_spotify_ids_into_one_recording(self):
		with tempfile.TemporaryDirectory() as directory:
			manifest = new_manifest(ManagedPaths(Path(directory)))
			first = {"title": "Song", "artists": "Artist", "duration_ms": 180000, "sp_id": "spotify-one", "isrc": "USABC1234567"}
			second = {"title": "Song", "artists": "Artist", "duration_ms": 180100, "sp_id": "spotify-two", "isrc": "USABC1234567"}
			first_key, _ = upsert_recording(manifest, first)
			second_key, recording = upsert_recording(manifest, second)
			self.assertEqual(first_key, second_key)
			self.assertEqual(recording["spotify_ids"], ["spotify-one", "spotify-two"])
			self.assertEqual(len(recording["source_occurrences"]), 2)
			self.assertEqual(len(manifest["recordings"]), 1)

	def test_remaster_and_plain_titles_share_one_recording_without_isrc(self):
		with tempfile.TemporaryDirectory() as directory:
			manifest = new_manifest(ManagedPaths(Path(directory)))
			plain = {"title": "The Rain Song", "artists": "Led Zeppelin", "duration_ms": 461000, "sp_id": "plain"}
			remaster = {"title": "The Rain Song - 2012 Remaster", "artists": "Led Zeppelin", "duration_ms": 461000, "sp_id": "remaster"}
			plain_key, _ = upsert_recording(manifest, plain)
			remaster_key, recording = upsert_recording(manifest, remaster)
			self.assertEqual(plain_key, remaster_key)
			self.assertEqual(recording["spotify_ids"], ["plain", "remaster"])
			self.assertEqual(recording["source_metadata"]["title"], "The Rain Song")
			self.assertEqual(recording["source_occurrences"][1]["spotify_title"], "The Rain Song - 2012 Remaster")

	def test_album_and_track_metadata_drop_release_labels_but_keep_source_text_for_audit(self):
		with tempfile.TemporaryDirectory() as directory:
			manifest = new_manifest(ManagedPaths(Path(directory)))
			key, recording = upsert_recording(manifest, {
				"title": "Song (Live) - 2005 Remaster",
				"artists": "Artist",
				"album": "Album (Deluxe Edition)",
				"album_artist": "Artist",
				"album_id": "album-id",
				"duration_ms": 180000,
				"sp_id": "track-id",
			}, source_type="album")
			self.assertEqual(recording["source_metadata"]["title"], "Song (Live)")
			self.assertEqual(recording["source_metadata"]["original_album"], "Album")
			self.assertEqual(recording["album_metadata"]["album"], "Album")
			self.assertEqual(recording["source_occurrences"][0]["spotify_title"], "Song (Live) - 2005 Remaster")
			self.assertEqual(recording["source_occurrences"][0]["spotify_album"], "Album (Deluxe Edition)")

			album = set_album(manifest, {
				"id": "album-id",
				"name": "Album (Deluxe Edition)",
				"album_artist": "Artist",
				"url": "url",
				"total_count": 1,
				"complete": True,
			}, [{"position": 1, "recording_id": key, "spotify_id": "track-id", "status": "managed_ready"}])
			self.assertEqual(album["name"], "Album")

	def test_m3u_preserves_positions_and_mixes_existing_and_managed_paths(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest = new_manifest(paths)
			one_key, one = upsert_recording(manifest, {"title": "Existing", "artists": "Artist", "duration_ms": 100000, "sp_id": "one"})
			two_key, two = upsert_recording(manifest, {"title": "Managed", "artists": "Artist", "duration_ms": 120000, "sp_id": "two"})
			one["music"] = {"persistent_id": "PID1", "location": "/Music/Existing.m4a"}
			one["active_reference"] = {"kind": "existing_music", "persistent_id": "PID1"}
			two["managed_file"] = {"relative_path": "tracks/aa/managed.mp3", "tool_owned": True}
			two["active_reference"] = {"kind": "managed_file", "relative_path": "tracks/aa/managed.mp3"}
			set_playlist(manifest, {"id": "playlist", "name": "Order", "url": "url", "tracks": [], "complete": True, "total_count": 3}, [
				{"position": 1, "recording_id": two_key, "spotify_id": "two", "status": "managed_ready"},
				{"position": 2, "recording_id": one_key, "spotify_id": "one", "status": "reused_music"},
				{"position": 3, "recording_id": two_key, "spotify_id": "two", "status": "managed_ready"},
			])
			content = playlist_m3u8(manifest, "playlist", paths)
			self.assertLess(content.index("managed.mp3"), content.index("/Music/Existing.m4a"))
			self.assertEqual(content.count("managed.mp3"), 2)
			self.assertNotIn("track=", content)

	def test_first_m3u_write_refuses_an_unregistered_existing_file(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest = new_manifest(paths)
			key, _ = upsert_recording(manifest, {"title": "Song", "artists": "Artist", "duration_ms": 100000, "sp_id": "one"})
			playlist = set_playlist(manifest, {"id": "playlist", "name": "Order", "url": "url", "tracks": [], "complete": True, "total_count": 1}, [
				{"position": 1, "recording_id": key, "spotify_id": "one", "status": "unresolved"},
			])
			target = paths.root / playlist["m3u8_relative_path"]
			target.parent.mkdir(parents=True)
			target.write_text("user file", encoding="utf-8")
			with self.assertRaises(FileExistsError):
				write_playlist_m3u8(manifest, "playlist", paths)
			self.assertEqual(target.read_text(encoding="utf-8"), "user file")

	def test_first_m3u_write_upgrades_a_previous_importer_marker(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest = new_manifest(paths)
			key, _ = upsert_recording(manifest, {"title": "Song", "artists": "Artist", "duration_ms": 100000, "sp_id": "one"})
			playlist = set_playlist(manifest, {"id": "playlist", "name": "Order", "url": "url", "tracks": [], "complete": True, "total_count": 1}, [
				{"position": 1, "recording_id": key, "spotify_id": "one", "status": "unresolved"},
			])
			target = paths.root / playlist["m3u8_relative_path"]
			target.parent.mkdir(parents=True)
			target.write_text("#EXTM3U\n#PREVIOUS-IMPORTER:playlist\n#PLAYLIST:Order\n", encoding="utf-8")

			write_playlist_m3u8(manifest, "playlist", paths)

			self.assertIn("#CRATE-MUSIC-IMPORTER:playlist", target.read_text(encoding="utf-8"))

	def test_promoted_cloud_music_reference_does_not_fall_back_to_obsolete_managed_file(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {"title": "Song", "artists": "Artist", "duration_ms": 100000, "sp_id": "one"})
			recording["managed_file"] = {"relative_path": "tracks/old.mp3", "tool_owned": True, "retirement_candidate": True}
			recording["music"] = {"persistent_id": "PROMOTED-PID", "location": None}
			recording["active_reference"] = {"kind": "existing_music", "persistent_id": "PROMOTED-PID"}
			set_playlist(manifest, {"id": "playlist", "name": "Order", "url": "url", "tracks": [], "complete": True, "total_count": 1}, [
				{"position": 1, "recording_id": key, "spotify_id": "one", "status": "reused_music"},
			])
			content = playlist_m3u8(manifest, "playlist", paths)
			self.assertIn("#MUSIC-PERSISTENT-ID:PROMOTED-PID", content)
			self.assertNotIn("tracks/old.mp3", content)


if __name__ == "__main__":
	unittest.main()
