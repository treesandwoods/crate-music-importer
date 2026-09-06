import tempfile
import unittest
from pathlib import Path

from crate_music_importer.ipod_import.manifest import ManagedPaths, new_manifest, upsert_recording
from crate_music_importer.ipod_import.music_cache import (
	MusicCacheError,
	MusicCacheUnavailableError,
	build_full_cache,
	load_music_cache,
	mark_music_cache_entry_stale,
	music_cache_tracks,
	new_partial_cache,
	rebuild_music_cache,
	save_music_cache,
	upsert_music_cache_track,
	validate_exact_track,
	cache_track_for_recording,
)


def track(persistent_id: str = "PID", *, title: str = "Song") -> dict:
	return {
		"persistent_id": persistent_id,
		"database_id": "1",
		"title": title,
		"artist": "Artist",
		"album": "Album",
		"album_artist": "Artist",
		"duration_s": 180.0,
		"location": f"/Music/{title}.m4a",
		"comment": "",
		"track_no": 1,
		"track_total": 1,
		"disc_no": 1,
		"disc_total": 1,
		"compilation": False,
	}


class MusicCacheTests(unittest.TestCase):
	def test_missing_cache_has_deliberate_rebuild_instruction(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			with self.assertRaisesRegex(MusicCacheUnavailableError, "Rebuild Music Library Cache"):
				load_music_cache(paths)
			self.assertFalse(paths.root.exists())

	def test_rebuild_is_atomic_and_uses_only_supplied_read_only_scan(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			calls = []
			result = rebuild_music_cache(paths, manifest, scan=lambda: calls.append("scan") or [track()])
			self.assertEqual(calls, ["scan"])
			self.assertEqual(result["cached_tracks"], 1)
			self.assertTrue(load_music_cache(paths)["initial_scan_completed"])
			self.assertFalse(list(paths.state_dir.glob("*.tmp")))

	def test_failed_rebuild_preserves_previous_cache(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			rebuild_music_cache(paths, manifest, scan=lambda: [track(title="Original")])
			before = paths.music_cache.read_bytes()
			with self.assertRaisesRegex(RuntimeError, "scan failed"):
				rebuild_music_cache(paths, manifest, scan=lambda: (_ for _ in ()).throw(RuntimeError("scan failed")))
			self.assertEqual(paths.music_cache.read_bytes(), before)

	def test_duplicate_persistent_id_aborts_without_replacing_cache(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			rebuild_music_cache(paths, manifest, scan=lambda: [track(title="Original")])
			before = paths.music_cache.read_bytes()
			with self.assertRaisesRegex(MusicCacheError, "duplicate persistent ID"):
				rebuild_music_cache(paths, manifest, scan=lambda: [track(title="One"), track(title="Two")])
			self.assertEqual(paths.music_cache.read_bytes(), before)

	def test_incremental_updates_replace_same_persistent_id_without_duplicates(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			rebuild_music_cache(paths, manifest, scan=lambda: [track()])
			upsert_music_cache_track(paths, track(title="Retagged"))
			upsert_music_cache_track(paths, track(title="Retagged Again"))
			cache = load_music_cache(paths)
			self.assertEqual(cache["total_cached_tracks"], 1)
			self.assertEqual(cache["tracks"]["PID"]["title"], "Retagged Again")

	def test_manual_rebuild_refreshes_changed_tracks_and_removes_deleted_tracks(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			rebuild_music_cache(paths, manifest, scan=lambda: [track("ONE"), track("TWO")])
			rebuild_music_cache(paths, manifest, scan=lambda: [track("ONE", title="Changed")])
			cache = load_music_cache(paths)
			self.assertEqual(set(cache["tracks"]), {"ONE"})
			self.assertEqual(cache["tracks"]["ONE"]["title"], "Changed")

	def test_full_rebuild_retains_missing_manifest_metadata_only_as_stale_migration_data(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			_, recording = upsert_recording(manifest, {"title": "Saved Song", "artists": "Artist", "duration_ms": 180000})
			recording["music"] = {"persistent_id": "OLD-PID", "database_id": "9", "location": "/Old/Song.m4a"}
			cache = build_full_cache(paths, manifest, [track("CURRENT")])
			self.assertEqual(set(cache["tracks"]), {"CURRENT"})
			migrated = cache["migration"]["entries_requiring_validation"]["OLD-PID"]
			self.assertEqual(migrated["title"], "Saved Song")
			self.assertTrue(migrated["validation_required"])
			self.assertTrue(migrated["stale"])

	def test_manifest_migration_is_partial_and_requires_validation(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			_, recording = upsert_recording(manifest, {"title": "Known", "artists": "Artist", "duration_ms": 180000})
			recording["music"] = {"persistent_id": "SAVED", "database_id": "9"}
			cache = new_partial_cache(paths, manifest)
			save_music_cache(paths, cache)
			self.assertTrue(cache["tracks"]["SAVED"]["validation_required"])
			with self.assertRaisesRegex(MusicCacheUnavailableError, "incomplete"):
				load_music_cache(paths)
			self.assertFalse(load_music_cache(paths, require_complete=False)["initial_scan_completed"])

	def test_stale_entries_are_retained_but_not_silently_reused(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			cache = build_full_cache(paths, new_manifest(paths), [track()])
			save_music_cache(paths, cache)
			mark_music_cache_entry_stale(paths, "PID", "title changed")
			loaded = load_music_cache(paths)
			self.assertTrue(loaded["tracks"]["PID"]["stale"])
			self.assertTrue(music_cache_tracks(loaded)[0]["stale"])

	def test_exact_validation_accepts_remaster_label_but_rejects_identity_change(self):
		cached = track(title="Song (2005 Remaster)")
		actual = track(title="Song")
		self.assertEqual(validate_exact_track(cached, actual), (True, ""))
		changed = track(title="Different Song")
		self.assertEqual(validate_exact_track(cached, changed)[0], False)

	def test_exact_validation_accepts_tool_owned_location_when_music_drops_comment(self):
		cached = track()
		actual = track()
		actual["location"] = "/Managed/tracks/song.mp3"
		self.assertEqual(
			validate_exact_track(
				cached,
				actual,
				require_importer_owned=True,
				recording_id="rec_song",
				importer_owned_location="/Managed/tracks/song.mp3",
			),
			(True, ""),
		)
		self.assertEqual(
			validate_exact_track(
				cached,
				actual,
				require_importer_owned=True,
				recording_id="rec_song",
				importer_owned_location="/Managed/tracks/different.mp3",
			)[0],
			False,
		)

	def test_importer_cache_uses_the_managed_file_duration(self):
		recording = {
			"recording_id": "rec_song",
			"source_metadata": {"title": "Song", "artists": "Artist", "duration_ms": 209106},
			"managed_file": {"duration_ms": 214645},
		}
		cached = cache_track_for_recording(recording, {"persistent_id": "PID"}, album_profile=False)
		self.assertEqual(cached["duration_s"], 214.645)


if __name__ == "__main__":
	unittest.main()
