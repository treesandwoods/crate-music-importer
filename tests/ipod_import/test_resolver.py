import tempfile
import unittest
import hashlib
from unittest.mock import patch
from pathlib import Path

from crate_music_importer.ipod_import.manifest import (
	ManagedPaths,
	load_manifest,
	new_manifest,
	save_manifest,
	set_album,
	set_playlist,
	upsert_recording,
)
from crate_music_importer.ipod_import.resolver import resolve_music, resolve_youtube, resolver_snapshot, search_youtube
from crate_music_importer.ipod_import.music_cache import build_full_cache, save_music_cache


def source_track(**updates):
	track = {
		"title": "Song",
		"artists": "Artist",
		"album": "Album",
		"album_artist": "Artist",
		"album_id": "album-id",
		"duration_ms": 180000,
		"sp_id": "track-id",
		"position": 1,
		"track_no": 1,
		"disc_no": 1,
	}
	track.update(updates)
	return track


def add_album(manifest, key):
	set_album(manifest, {
		"id": "album-id",
		"name": "Album",
		"album_artist": "Artist",
		"url": "https://open.spotify.com/album/album-id",
		"tracks": [],
		"complete": True,
		"total_count": 1,
	}, [{"position": 1, "track_no": 1, "disc_no": 1, "recording_id": key, "spotify_id": "track-id", "status": "review_required"}])


def add_playlist(manifest, key):
	set_playlist(manifest, {
		"id": "playlist-id",
		"name": "Playlist",
		"url": "https://open.spotify.com/playlist/playlist-id",
		"tracks": [],
		"complete": True,
		"total_count": 1,
	}, [{"position": 1, "recording_id": key, "spotify_id": "track-id", "status": "review_required"}])


class ResolverTests(unittest.TestCase):
	def test_manual_choice_with_stale_automatch_error_is_ready(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track(), source_type="album")
			recording["youtube"] = {
				"url": "https://www.youtube.com/watch?v=chosen",
				"video_id": "chosen",
				"selected_by": "manual_url",
			}
			recording["last_error"] = "No YouTube result scored strictly above 0.87."
			add_album(manifest, key)
			save_manifest(paths, manifest)

			snapshot = resolver_snapshot(paths)
			self.assertEqual(snapshot["problems"], [])
			self.assertEqual([(source["type"], source["state"]) for source in snapshot["sources"]], [("album", "ready")])
			self.assertEqual(snapshot["readySources"][0]["id"], "album-id")

	def test_youtube_choice_clears_review_and_failure(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track())
			recording["review"] = {"kind": "youtube_missing", "message": "Choose", "candidates": []}
			recording["last_error"] = "No YouTube result scored strictly above 0.87."
			add_playlist(manifest, key)
			save_manifest(paths, manifest)

			result = resolve_youtube(key, "https://www.youtube.com/watch?v=chosen", paths=paths, inspect=lambda _url: {
				"video_id": "chosen",
				"url": "https://www.youtube.com/watch?v=chosen",
				"title": "Song",
				"uploader": "Artist - Topic",
				"duration_s": 180,
				"reasons": [],
			})
			self.assertEqual(result["candidate"]["id"], "chosen")
			saved = load_manifest(paths)["recordings"][key]
			self.assertNotIn("review", saved)
			self.assertNotIn("last_error", saved)
			self.assertEqual(saved["youtube"]["selected_by"], "manual_candidate")

	def test_stale_importer_save_cannot_erase_a_manual_youtube_choice(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track(), source_type="album")
			recording["review"] = {"kind": "youtube_missing", "message": "Choose", "candidates": []}
			add_album(manifest, key)
			save_manifest(paths, manifest)
			stale_importer_manifest = load_manifest(paths)

			resolve_youtube(key, "https://www.youtube.com/watch?v=chosen", paths=paths, inspect=lambda _url: {
				"video_id": "chosen",
				"url": "https://www.youtube.com/watch?v=chosen",
				"title": "Song",
				"uploader": "Artist - Topic",
				"duration_s": 180,
				"reasons": [],
			})
			stale_importer_manifest["recordings"][key]["review"] = {"kind": "youtube_missing", "message": "Stale", "candidates": []}
			save_manifest(paths, stale_importer_manifest)

			saved = load_manifest(paths)["recordings"][key]
			self.assertEqual(saved["youtube"]["video_id"], "chosen")
			self.assertNotIn("review", saved)

	def test_youtube_problem_without_saved_candidates_still_opens_manual_review(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track(), source_type="album")
			recording["review"] = {"kind": "youtube_missing", "message": "Choose", "candidates": []}
			recording["last_error"] = "No YouTube result scored strictly above 0.87."
			add_album(manifest, key)
			save_manifest(paths, manifest)

			snapshot = resolver_snapshot(paths)
			self.assertEqual(snapshot["problems"][0]["state"], "needs_choice")
			self.assertEqual(snapshot["sources"][0]["state"], "needs_choice")

	def test_search_uses_edited_query_and_returns_scored_shape(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, _ = upsert_recording(manifest, source_track())
			save_manifest(paths, manifest)
			calls = []

			def search(_metadata, *, query):
				calls.append(query)
				return [{
					"video_id": "new",
					"url": "https://www.youtube.com/watch?v=new",
					"title": "Song",
					"uploader": "Artist",
					"duration_s": 180,
					"score": 0.8,
					"reasons": ["manual search"],
				}]

			result = search_youtube(key, "Artist Song studio", paths=paths, search=search)
			self.assertEqual(calls, ["Artist Song studio"])
			self.assertEqual(result["candidates"][0]["id"], "new")

	def test_music_ambiguity_can_choose_only_a_displayed_live_candidate(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track())
			candidate = {
				"title": "Song", "artist": "Artist", "album": "Album", "duration_s": 180,
				"persistent_id": "PID-ONE", "database_id": "1", "location": "/Music/Song.m4a",
				"comment": "", "score": 1.0,
			}
			recording["review"] = {"kind": "music_ambiguity", "message": "Choose", "candidates": [candidate]}
			add_playlist(manifest, key)
			save_manifest(paths, manifest)

			resolve_music(key, "PID-ONE", paths=paths, scan=lambda: [candidate])
			saved = load_manifest(paths)["recordings"][key]
			self.assertEqual(saved["active_reference"], {"kind": "existing_music", "persistent_id": "PID-ONE"})
			self.assertNotIn("review", saved)

	def test_user_owned_album_conflict_blocks_the_whole_source(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track(), source_type="album")
			recording["review"] = {"kind": "album_conflict", "message": "Different album", "candidates": []}
			add_album(manifest, key)
			save_manifest(paths, manifest)

			snapshot = resolver_snapshot(paths)
			self.assertEqual(snapshot["problems"][0]["state"], "blocked")
			self.assertEqual(snapshot["sources"][0]["state"], "blocked")
			self.assertEqual(snapshot["readySources"], [])

	def test_importer_owned_playlist_copy_can_be_approved_for_album_promotion(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			moved = paths.root / "Music" / "Compilations" / "Playlist Imports" / "Song.mp3"
			moved.parent.mkdir(parents=True)
			moved.write_bytes(b"managed-audio")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track(), source_type="album")
			candidate = {
				"title": "Song", "artist": "Artist", "album": "Playlist Imports", "duration_s": 180,
				"persistent_id": "MANAGED-PID", "database_id": "1", "location": str(moved), "comment": "",
			}
			recording["managed_file"] = {
				"relative_path": "tracks/missing.mp3", "tool_owned": True, "metadata_profile": "playlist",
				"sha256": hashlib.sha256(b"managed-audio").hexdigest(),
			}
			recording["music"] = {"source": "managed_import", "persistent_id": "MANAGED-PID"}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": "tracks/missing.mp3"}
			recording["review"] = {"kind": "album_conflict", "message": "Different album", "candidates": [candidate]}
			add_album(manifest, key)
			save_manifest(paths, manifest)
			save_music_cache(paths, build_full_cache(paths, manifest, [candidate]))

			snapshot = resolver_snapshot(paths)
			problem = snapshot["problems"][0]
			self.assertEqual(problem["state"], "needs_choice")
			self.assertTrue(problem["candidates"][0]["promotionEligible"])

			live_candidate = dict(candidate, location=None)
			with patch("crate_music_importer.ipod_import.resolver.lookup_music_track", return_value=live_candidate):
				resolve_music(key, "MANAGED-PID", paths=paths)
			resolved_snapshot = resolver_snapshot(paths)
			self.assertEqual(resolved_snapshot["sources"][0]["state"], "ready")
			saved = load_manifest(paths)["recordings"][key]
			self.assertNotIn("review", saved)
			self.assertEqual(saved["music"]["source"], "managed_import")
			self.assertEqual(
				saved["active_reference"],
				{"kind": "managed_file", "relative_path": "Music/Compilations/Playlist Imports/Song.mp3"},
			)

	def test_album_only_conflict_does_not_block_a_shared_playlist(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track(), source_type="album")
			recording["review"] = {"kind": "album_conflict", "message": "Different album", "candidates": []}
			add_album(manifest, key)
			add_playlist(manifest, key)
			save_manifest(paths, manifest)

			snapshot = resolver_snapshot(paths)
			states = {(source["type"], source["id"]): source["state"] for source in snapshot["sources"]}
			self.assertEqual(states[("album", "album-id")], "blocked")
			self.assertEqual(states[("playlist", "playlist-id")], "ready")
			self.assertEqual([(source["type"], source["id"]) for source in snapshot["readySources"]], [("playlist", "playlist-id")])

	def test_duplicate_conflict_selects_proper_album_copy_without_deleting_managed_copy(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track(), source_type="album")
			recording["managed_file"] = {"relative_path": "tracks/managed.mp3", "tool_owned": True}
			recording["music"] = {"source": "managed_import", "persistent_id": "MANAGED-PID"}
			proper = {
				"title": "Song", "artist": "Artist", "album": "Album", "duration_s": 180,
				"persistent_id": "PROPER-PID", "database_id": "2", "location": "/Music/Album/Song.m4a",
				"comment": "", "score": 1.0,
			}
			managed = {
				"title": "Song", "artist": "Artist", "album": "Playlist Imports", "duration_s": 180,
				"persistent_id": "MANAGED-PID", "database_id": "1", "location": "/Managed/Song.mp3",
				"comment": f"Managed by Crate Music Importer; recording_id={key}", "score": 0.95,
			}
			recording["review"] = {"kind": "album_duplicate_conflict", "message": "Choose canonical", "candidates": [proper, managed]}
			add_album(manifest, key)
			save_manifest(paths, manifest)

			resolve_music(key, "PROPER-PID", paths=paths, scan=lambda: [proper, managed])
			saved = load_manifest(paths)["recordings"][key]
			self.assertEqual(saved["active_reference"]["persistent_id"], "PROPER-PID")
			self.assertTrue(saved["managed_file"]["retirement_candidate"])
			self.assertEqual(saved["managed_file"]["relative_path"], "tracks/managed.mp3")


if __name__ == "__main__":
	unittest.main()

class YouTubeReviewFilteringTests(unittest.TestCase):
	def test_legacy_candidates_are_filtered_without_mutating_saved_state(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track())
			recording["review"] = {"kind": "youtube_missing", "candidates": [
				{"video_id": "wrong", "title": "Other Song", "uploader": "Artist", "duration_s": 180, "score": 0.99},
				{"video_id": "right", "title": "Song", "uploader": "Artist", "duration_s": 190, "url": "https://www.youtube.com/watch?v=right"},
			]}
			add_playlist(manifest, key)
			save_manifest(paths, manifest)
			before = load_manifest(paths)
			problem = resolver_snapshot(paths)["problems"][0]
			self.assertEqual([c["id"] for c in problem["candidates"]], ["right"])
			self.assertFalse(problem["candidates"][0]["automaticEligible"])
			self.assertEqual(load_manifest(paths), before)

	def test_search_more_merges_relevant_saved_candidates_without_saving(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track())
			good = {"video_id": "saved", "title": "Artist - Song", "duration_s": 180}
			recording["review"] = {"kind": "youtube_missing", "candidates": [good]}
			save_manifest(paths, manifest)
			before = load_manifest(paths)
			def search(metadata, *, query, more):
				self.assertTrue(more)
				return [good, dict(good, video_id="new", duration_s=200), dict(good, video_id="bad", title="Unrelated")]
			result = search_youtube(key, paths=paths, more=True, search=search)
			self.assertEqual([c["id"] for c in result["candidates"]], ["saved", "new"])
			self.assertEqual(load_manifest(paths), before)
