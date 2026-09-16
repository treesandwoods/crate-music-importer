import tempfile
import unittest
from pathlib import Path

from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest, new_manifest, save_manifest, set_album, set_playlist, upsert_recording
from crate_music_importer.ipod_import.resolver import resolve_music, resolve_youtube, resolver_snapshot


def source_track(**updates):
	track = {"title": "Song", "artists": "Artist", "album": "Album", "duration_ms": 180000, "sp_id": "track-id", "position": 1, "track_no": 1, "disc_no": 1}
	track.update(updates)
	return track


def music_track(persistent_id: str, album: str = "Album"):
	return {"persistent_id": persistent_id, "title": "Song", "artist": "Artist", "album": album, "duration_s": 180, "location": f"/Music/{persistent_id}.m4a", "comment": ""}


def add_playlist(manifest, key):
	set_playlist(manifest, {"id": "playlist-id", "name": "Playlist", "url": "https://open.spotify.com/playlist/playlist-id", "tracks": [], "complete": True, "total_count": 1}, [{"position": 1, "recording_id": key, "spotify_id": "track-id", "status": "review_required"}])


def add_album(manifest, key):
	set_album(manifest, {"id": "album-id", "name": "Album", "album_artist": "Artist", "url": "https://open.spotify.com/album/album-id", "tracks": [], "complete": True, "total_count": 1}, [{"position": 1, "track_no": 1, "disc_no": 1, "recording_id": key, "spotify_id": "track-id", "status": "review_required"}])


class ResolverTests(unittest.TestCase):
	def test_youtube_choice_clears_review_and_is_resumable(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track())
			recording["review"] = {"kind": "youtube_missing", "message": "Choose", "candidates": []}
			recording["last_error"] = "No YouTube result scored strictly above 0.87."
			add_playlist(manifest, key)
			save_manifest(paths, manifest)
			result = resolve_youtube(key, "https://www.youtube.com/watch?v=chosen", paths=paths, inspect=lambda _url: {"video_id": "chosen", "url": "https://www.youtube.com/watch?v=chosen", "title": "Song", "uploader": "Artist - Topic", "duration_s": 180, "reasons": []})
			self.assertEqual(result["candidate"]["id"], "chosen")
			saved = load_manifest(paths)["recordings"][key]
			self.assertNotIn("review", saved)
			self.assertNotIn("last_error", saved)

	def test_music_ambiguity_accepts_only_displayed_exact_candidate(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track())
			recording["review"] = {"kind": "music_ambiguity", "message": "Choose", "candidates": [music_track("ONE"), music_track("TWO")]}
			add_playlist(manifest, key)
			save_manifest(paths, manifest)
			with self.assertRaisesRegex(ValueError, "displayed"):
				resolve_music(key, "THREE", paths=paths, scan=lambda: [music_track("ONE"), music_track("TWO")])
			resolve_music(key, "ONE", paths=paths, scan=lambda: [music_track("ONE"), music_track("TWO")])
			self.assertEqual(load_manifest(paths)["recordings"][key]["music_binding"], {"persistent_id": "ONE"})

	def test_album_choice_requires_requested_album_identity(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track(), source_type="album")
			recording["review"] = {"kind": "album_identity_mismatch", "message": "Choose", "candidates": [music_track("LOOSE", "Playlist Imports"), music_track("ALBUM")]}
			add_album(manifest, key)
			save_manifest(paths, manifest)
			with self.assertRaisesRegex(ValueError, "album identity"):
				resolve_music(key, "LOOSE", paths=paths, scan=lambda: [music_track("LOOSE", "Playlist Imports"), music_track("ALBUM")])
			resolve_music(key, "ALBUM", paths=paths, scan=lambda: [music_track("LOOSE", "Playlist Imports"), music_track("ALBUM")])
			self.assertEqual(load_manifest(paths)["recordings"][key]["music_binding"], {"persistent_id": "ALBUM"})

	def test_snapshot_has_no_music_provenance_fields(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track())
			recording["review"] = {"kind": "music_ambiguity", "message": "Choose", "candidates": [music_track("ONE"), music_track("TWO")]}
			add_playlist(manifest, key)
			save_manifest(paths, manifest)
			candidate = resolver_snapshot(paths)["problems"][0]["candidates"][0]
			self.assertNotIn("importerOwned", candidate)
			self.assertNotIn("promotionEligible", candidate)

	def test_ready_source_is_complete_after_music_binding(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track())
			recording["music_binding"] = {"persistent_id": "PID"}
			add_playlist(manifest, key)
			save_manifest(paths, manifest)
			snapshot = resolver_snapshot(paths)
			self.assertEqual(snapshot["sources"][0]["state"], "complete")


if __name__ == "__main__":
	unittest.main()
