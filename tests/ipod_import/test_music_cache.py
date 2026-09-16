import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crate_music_importer.ipod_import.manifest import ManagedPaths
from crate_music_importer.ipod_import.music_cache import (
	MusicCacheError,
	build_full_cache,
	load_music_cache,
	music_cache_tracks,
	refresh_music_cache,
	upsert_music_cache_track,
	validate_exact_track,
)


def track(persistent_id: str = "PID", *, title: str = "Song") -> dict:
	return {
		"persistent_id": persistent_id,
		"database_id": "1",
		"title": title,
		"artist": "Artist",
		"album": "Album",
		"duration_s": 180.0,
		"location": f"/Music/{title}.m4a",
		"comment": "",
	}


class MusicCacheTests(unittest.TestCase):
	def test_cache_contains_only_snapshot_metadata(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			cache = refresh_music_cache(paths, [track()])
			self.assertEqual(set(cache), {"schema_version", "managed_root", "scanned_at", "tracks"})
			self.assertEqual([value["persistent_id"] for value in music_cache_tracks(cache)], ["PID"])

	def test_missing_cache_rebuilds_from_music(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			cache = load_music_cache(paths, rebuild=lambda: [track("LIVE")])
			self.assertEqual(set(cache["tracks"]), {"LIVE"})
			self.assertTrue(paths.music_cache.is_file())

	def test_corrupt_cache_rebuilds_from_music(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			paths.state_dir.mkdir(parents=True)
			paths.music_cache.write_text("not json", encoding="utf-8")
			cache = load_music_cache(paths, rebuild=lambda: [track("LIVE")])
			self.assertEqual(set(cache["tracks"]), {"LIVE"})

	def test_expired_cache_rebuilds_without_durable_stale_state(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			cache = build_full_cache(paths, [track("OLD")])
			cache["scanned_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
			paths.state_dir.mkdir(parents=True)
			paths.music_cache.write_text(json.dumps(cache), encoding="utf-8")
			loaded = load_music_cache(paths, rebuild=lambda: [track("NEW")])
			self.assertEqual(set(loaded["tracks"]), {"NEW"})

	def test_refresh_replaces_removed_and_changed_tracks(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			refresh_music_cache(paths, [track("ONE"), track("TWO")])
			refresh_music_cache(paths, [track("ONE", title="Changed")])
			cache = load_music_cache(paths)
			self.assertEqual(set(cache["tracks"]), {"ONE"})
			self.assertEqual(cache["tracks"]["ONE"]["title"], "Changed")

	def test_incremental_update_replaces_same_id(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			refresh_music_cache(paths, [track()])
			upsert_music_cache_track(paths, track(title="Retagged"))
			self.assertEqual(load_music_cache(paths)["tracks"]["PID"]["title"], "Retagged")

	def test_duplicate_ids_preserve_previous_cache(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			refresh_music_cache(paths, [track(title="Original")])
			before = paths.music_cache.read_bytes()
			with self.assertRaisesRegex(MusicCacheError, "duplicate persistent ID"):
				refresh_music_cache(paths, [track(title="One"), track(title="Two")])
			self.assertEqual(paths.music_cache.read_bytes(), before)

	def test_exact_validation_ignores_comment_and_provenance(self):
		expected = track(title="Song (2005 Remaster)")
		actual = track(title="Song")
		actual["comment"] = ""
		self.assertEqual(validate_exact_track(expected, actual), (True, ""))
		self.assertFalse(validate_exact_track(expected, track(title="Different Song"))[0])


if __name__ == "__main__":
	unittest.main()
