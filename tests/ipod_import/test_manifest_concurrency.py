import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import import manifest as state


class ManifestConcurrencyTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.paths = state.ManagedPaths(Path(self.temp.name))
		manifest = state.new_manifest(self.paths)
		self.key, _ = state.upsert_recording(manifest, {"title": "Song", "artists": "Artist", "sp_id": "track"})
		manifest["playlists"]["playlist"] = {"spotify_url": "old", "cover_url": "old-cover", "items": []}
		state.save_manifest(self.paths, manifest)

	def test_checkpoint_preserves_concurrent_choices_bindings_links_and_artwork(self):
		checkpoint = state.clone_manifest(state.load_manifest(self.paths))
		def choose(latest):
			latest["recordings"][self.key]["music_binding"] = {"persistent_id": "CHOSEN"}
			latest["recordings"][self.key]["review"] = {"kind": "music_ambiguity", "message": "New choice"}
			latest["playlists"]["playlist"].update(spotify_url="new", cover_url="new-cover")
			latest["albums"]["concurrent"] = {"items": []}
		state.update_manifest(self.paths, choose)
		checkpoint["recordings"][self.key]["managed_file"] = {"relative_path": "Music/song.mp3"}
		state.save_manifest(self.paths, checkpoint)
		latest = state.load_manifest(self.paths)
		self.assertEqual(latest["recordings"][self.key]["music_binding"], {"persistent_id": "CHOSEN"})
		self.assertEqual(latest["recordings"][self.key]["review"]["message"], "New choice")
		self.assertEqual(latest["playlists"]["playlist"]["spotify_url"], "new")
		self.assertEqual(latest["playlists"]["playlist"]["cover_url"], "new-cover")
		self.assertIn("concurrent", latest["albums"])
		self.assertEqual(latest["recordings"][self.key]["managed_file"], {"relative_path": "Music/song.mp3"})

	def test_barrier_synchronized_independent_writers_do_not_lose_fields(self):
		barrier = threading.Barrier(2)
		def writer(field):
			manifest = state.load_manifest(self.paths)
			manifest["recordings"][self.key][field] = {"value": field}
			barrier.wait(timeout=2)
			state.save_manifest(self.paths, manifest)
		with ThreadPoolExecutor(max_workers=2) as pool:
			list(pool.map(writer, ["download", "artwork"]))
		latest = state.load_manifest(self.paths)["recordings"][self.key]
		self.assertEqual(latest["download"], {"value": "download"})
		self.assertEqual(latest["artwork"], {"value": "artwork"})

	def test_stale_checkpoint_cannot_resurrect_deleted_recording(self):
		checkpoint = state.load_manifest(self.paths)
		state.update_manifest(self.paths, lambda latest: latest["recordings"].pop(self.key))
		checkpoint["recordings"][self.key]["last_error"] = "old failure"
		state.save_manifest(self.paths, checkpoint)
		self.assertNotIn(self.key, state.load_manifest(self.paths)["recordings"])

	def test_noop_transactions_do_not_rewrite_state(self):
		with patch.object(state, "_save_manifest_unlocked", wraps=state._save_manifest_unlocked) as write:
			state.update_manifest(self.paths, lambda latest: None)
			state.save_manifest(self.paths, state.load_manifest(self.paths))
		self.assertEqual(write.call_count, 0)

	def test_failed_atomic_write_preserves_state_and_cleans_temporary_file(self):
		before = self.paths.manifest.read_bytes()
		manifest = state.load_manifest(self.paths)
		manifest["recordings"][self.key]["last_error"] = "new"
		with patch.object(state.os, "replace", side_effect=OSError("disk full")):
			with self.assertRaises(OSError):
				state.save_manifest(self.paths, manifest)
		self.assertEqual(self.paths.manifest.read_bytes(), before)
		self.assertEqual(list(self.paths.state_dir.glob("manifest-*.tmp")), [])
		self.assertIsInstance(json.loads(before), dict)

	def test_invalid_state_is_actionable_and_cannot_be_migrated_or_replaced(self):
		invalid_root = dict(state.load_manifest(self.paths), version=1, managed_root="different-root")
		for raw in ("broken", "[]", json.dumps({"version": 2, "recordings": []}), json.dumps(invalid_root)):
			with self.subTest(raw=raw):
				self.paths.manifest.write_text(raw)
				with self.assertRaises(ValueError):
					state.load_manifest(self.paths)
				self.assertEqual(self.paths.manifest.read_text(), raw)

	def test_malformed_nested_source_is_rejected_without_rewriting_it(self):
		value = json.loads(self.paths.manifest.read_text())
		value["playlists"]["playlist"]["items"] = ["invalid occurrence"]
		raw = json.dumps(value)
		self.paths.manifest.write_text(raw)
		with self.assertRaisesRegex(ValueError, "Library Health"):
			state.load_manifest(self.paths)
		self.assertEqual(self.paths.manifest.read_text(), raw)
