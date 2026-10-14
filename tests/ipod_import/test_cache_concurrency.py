import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import import music_cache as cache
from crate_music_importer.ipod_import.manifest import ManagedPaths


def track(pid):
	return {"persistent_id": pid, "title": pid, "artist": "Artist", "duration_s": 180}


class CacheConcurrencyTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.paths = ManagedPaths(Path(self.temp.name))

	def test_incremental_changes_keep_full_scan_freshness(self):
		original = cache.build_full_cache(self.paths, [track("OLD")])
		original["scanned_at"] = (datetime.now(timezone.utc) - timedelta(hours=23)).isoformat()
		cache.save_music_cache(self.paths, original)
		cache.upsert_music_cache_track(self.paths, track("NEW"))
		self.assertEqual(cache.read_music_cache(self.paths)["scanned_at"], original["scanned_at"])
		cache.remove_music_cache_tracks(self.paths, {"OLD"})
		self.assertEqual(cache.read_music_cache(self.paths)["scanned_at"], original["scanned_at"])

	def test_concurrent_updates_keep_both_tracks(self):
		cache.refresh_music_cache(self.paths, [])
		first_writing = threading.Event()
		release_first = threading.Event()
		second_finished = threading.Event()
		save = cache.save_music_cache
		def controlled_save(paths, value):
			if "FIRST" in value["tracks"] and "SECOND" not in value["tracks"]:
				first_writing.set()
				self.assertTrue(release_first.wait(2))
			return save(paths, value)
		def second():
			cache.upsert_music_cache_track(self.paths, track("SECOND"))
			second_finished.set()
		with patch.object(cache, "save_music_cache", side_effect=controlled_save):
			with ThreadPoolExecutor(max_workers=2) as pool:
				first = pool.submit(cache.upsert_music_cache_track, self.paths, track("FIRST"))
				self.assertTrue(first_writing.wait(2))
				other = pool.submit(second)
				second_finished.wait(0.1)
				release_first.set()
				first.result(timeout=2)
				other.result(timeout=2)
		self.assertEqual(set(cache.read_music_cache(self.paths)["tracks"]), {"FIRST", "SECOND"})

	def test_concurrent_missing_cache_rebuilds_scan_once(self):
		started = threading.Event()
		release = threading.Event()
		calls = []
		second_failed_read = threading.Event()
		read = cache.read_music_cache
		def controlled_read(paths):
			try:
				return read(paths)
			except cache.MusicCacheUnavailableError:
				if threading.current_thread().name.endswith("_1"):
					second_failed_read.set()
				raise
		def rebuild():
			calls.append(1)
			started.set()
			self.assertTrue(release.wait(2))
			return [track("LIVE")]
		with patch.object(cache, "read_music_cache", side_effect=controlled_read), ThreadPoolExecutor(max_workers=2) as pool:
			first = pool.submit(cache.load_music_cache, self.paths, rebuild=rebuild)
			self.assertTrue(started.wait(2))
			second = pool.submit(cache.load_music_cache, self.paths, rebuild=rebuild)
			self.assertTrue(second_failed_read.wait(2))
			release.set()
			self.assertEqual(first.result(timeout=2), second.result(timeout=2))
		self.assertEqual(len(calls), 1)

	def test_unchanged_incremental_updates_do_not_write(self):
		cache.refresh_music_cache(self.paths, [track("OLD")])
		with patch.object(cache, "save_music_cache", wraps=cache.save_music_cache) as save:
			cache.upsert_music_cache_track(self.paths, track("OLD"))
			self.assertEqual(cache.remove_music_cache_tracks(self.paths, {"ABSENT"}), 0)
		self.assertEqual(save.call_count, 0)

	def test_failed_rebuild_keeps_expired_cache_and_recovery_instruction(self):
		value = cache.build_full_cache(self.paths, [track("OLD")])
		value["scanned_at"] = "2000-01-01T00:00:00+00:00"
		self.paths.state_dir.mkdir(parents=True, exist_ok=True)
		self.paths.music_cache.write_text(json.dumps(value))
		before = self.paths.music_cache.read_bytes()
		with self.assertRaisesRegex(cache.MusicCacheUnavailableError, "Library Health"):
			cache.load_music_cache(self.paths, rebuild=lambda: (_ for _ in ()).throw(RuntimeError("denied")))
		self.assertEqual(self.paths.music_cache.read_bytes(), before)

	def test_session_reuses_snapshot_but_preserves_concurrent_external_changes(self):
		cache.refresh_music_cache(self.paths, [track("OLD")])
		with patch.object(cache.json, "load", wraps=cache.json.load) as reads:
			session = cache.MusicCacheSession(self.paths)
			for _ in range(5):
				session.upsert(track("OLD"))
			self.assertEqual(reads.call_count, 1)
		cache.upsert_music_cache_track(self.paths, track("OTHER"))
		session.upsert(track("NEW"))
		self.assertEqual(set(cache.read_music_cache(self.paths)["tracks"]), {"OLD", "OTHER", "NEW"})

	def test_session_retries_failed_write_at_the_next_durable_boundary(self):
		cache.refresh_music_cache(self.paths, [])
		session = cache.MusicCacheSession(self.paths)
		with patch.object(cache.os, "replace", side_effect=OSError("disk full")):
			with self.assertRaises(OSError):
				session.upsert(track("NEW"))
		self.assertEqual(cache.read_music_cache(self.paths)["tracks"], {})
		session.upsert(track("NEW"))
		self.assertEqual(set(cache.read_music_cache(self.paths)["tracks"]), {"NEW"})

	def test_rebuild_and_incremental_update_share_the_scan_boundary(self):
		cache.refresh_music_cache(self.paths, [])
		started = threading.Event()
		release = threading.Event()
		def scan():
			started.set()
			self.assertTrue(release.wait(2))
			return [track("SCAN")]
		with ThreadPoolExecutor(max_workers=2) as pool:
			full = pool.submit(cache.rebuild_music_cache, self.paths, scan)
			self.assertTrue(started.wait(2))
			update = pool.submit(cache.upsert_music_cache_track, self.paths, track("UPDATE"))
			release.set()
			full.result(timeout=2)
			update.result(timeout=2)
		self.assertEqual(set(cache.read_music_cache(self.paths)["tracks"]), {"SCAN", "UPDATE"})
