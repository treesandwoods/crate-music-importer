import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from crate_music_importer.ipod_import.catalog import canonical_relative_path, library_id, plan_paths, register_tracks
from crate_music_importer.ipod_import.manifest import ManagedPaths, new_manifest, save_manifest, load_manifest, file_sha256
from crate_music_importer.ipod_import.music_cache import rebuild_music_cache, load_music_cache
from crate_music_importer.ipod_import.preservation import assert_preserved
from crate_music_importer.ipod_import.unification import build_preview, migrate, backup_package


def inspect(path):
	digest = file_sha256(path)
	return {"sha256": digest, "audio_sha256": digest, "decoded_sha256": digest, "size_bytes": path.stat().st_size, "audio_verified": True}


class UnificationTests(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.home = Path(self.tmp.name).resolve()
		self.paths = ManagedPaths(self.home / "managed")
		self.paths.root.mkdir()
		self.source = self.paths.root / "old.mp3"
		self.source.write_bytes(b"synthetic audio")
		self.track = {"persistent_id": "A", "database_id": "12", "location": str(self.source), "title": "Song", "album": "Album", "album_artist": "Artist", "track_no": 1,
			"preservation": {"rating": 80, "favorited": True, "playedCount": 17, "skippedCount": 4}, "artwork_sha256": ["cover"]}
		self.baseline = {"complete": True, "tracks": [self.track], "playlists": [{"persistent_id": "P", "tracks": ["A", "A"]}], "settings": {"keep_organized": False, "copy_files": False}}
		self.current = copy.deepcopy(self.baseline)
		self.manifest = new_manifest(self.paths)
		save_manifest(self.paths, self.manifest)
		self.manifest = load_manifest(self.paths)
		self.package = self.home / "test.musiclibrary"
		self.package.mkdir()
		(self.package / "Library.musicdb").write_bytes(b"library")
		package_patch = patch("crate_music_importer.ipod_import.reconciliation.MUSIC_LIBRARY_PACKAGE", self.package)
		package_patch.start()
		self.addCleanup(package_patch.stop)
		self.env = patch.dict(os.environ, {"CRATE_APPLICATION_STATE": str(self.home / "app")})
		self.env.start()
		self.addCleanup(self.env.stop)
		health_patch = patch("crate_music_importer.ipod_import.health._build_health_report", return_value={"status": "healthy", "summary": {"critical": 0}})
		health_patch.start()
		self.addCleanup(health_patch.stop)
		self.home_patch = patch("pathlib.Path.home", return_value=self.home)
		self.home_patch.start()
		self.addCleanup(self.home_patch.stop)

	def preview(self):
		return build_preview(self.paths, self.manifest, copy.deepcopy(self.baseline), inspect=inspect)

	def relink(self, pid, path):
		self.assertEqual(pid, "A")
		self.current["tracks"][0]["location"] = str(path)
		return {"persistent_id": pid, "location": str(path)}

	def execute(self, preview, **kwargs):
		return migrate(self.paths, preview, snapshot=lambda: copy.deepcopy(self.current), relink=self.relink, music_package=self.package, inspect=inspect, **kwargs)

	def test_preview_is_read_only_and_uses_music_metadata(self):
		before = self.paths.manifest.read_bytes()
		preview = self.preview()
		self.assertTrue(preview["ready"])
		self.assertTrue(preview["tracks"][0]["target"].endswith("Music/Artist/Album/01 — Song.mp3"))
		self.assertEqual(self.paths.manifest.read_bytes(), before)
		self.assertTrue(self.source.exists())
		self.assertFalse((self.paths.root / "Music").exists())

	def test_explicit_deferred_orphan_is_preserved_during_migration(self):
		orphan = self.paths.root / "deferred.mp3"
		orphan.write_bytes(b"leave this recording untouched")
		preview = build_preview(self.paths, self.manifest, self.baseline, inspect=inspect, deferred_orphans=(str(orphan),))
		self.assertTrue(preview["ready"])
		self.execute(preview)
		self.assertEqual(orphan.read_bytes(), b"leave this recording untouched")
		self.assertTrue(Path(preview["tracks"][0]["target"]).exists())

	def test_batch_relink_interruption_resumes_from_actual_music_location(self):
		preview = self.preview()
		def interrupted(targets):
			for pid, path in targets.items():
				self.relink(pid, path)
			raise RuntimeError("interrupted batch")
		with self.assertRaisesRegex(RuntimeError, "interrupted batch"):
			self.execute(preview, relink_batch=interrupted)
		self.assertTrue(self.source.exists())
		result = self.execute(preview, relink_batch=lambda targets: self.fail("Already relinked items must not repeat"))
		self.assertEqual(result["state"], "verified_awaiting_user_acceptance")

	def test_catalog_identity_does_not_change_with_metadata(self):
		register_tracks(self.manifest, [self.track], complete=True)
		changed = {**self.track, "title": "New title"}
		register_tracks(self.manifest, [changed], complete=True)
		self.assertEqual(list(self.manifest["catalog"]["entries"]), [library_id("A")])
		self.assertFalse(self.manifest["catalog"]["entries"][library_id("A")]["authority"]["rewrite"])

	def test_cache_can_be_deleted_and_rebuilt_from_catalog(self):
		rebuild_music_cache(self.paths, self.manifest, scan=lambda: [self.track])
		self.paths.music_cache.unlink()
		self.assertEqual(load_music_cache(self.paths)["tracks"]["A"]["title"], "Song")

	def test_collision_unicode_case_and_multidisc(self):
		tracks = [{**self.track, "persistent_id": "A", "title": "Été", "disc_no": 2, "disc_total": 2}, {**self.track, "persistent_id": "B", "title": "E\u0301TE\u0301", "disc_no": 2, "disc_total": 2}]
		paths, collisions = plan_paths(tracks, set())
		self.assertEqual(len(collisions), 1)
		self.assertIn("2-01 —", paths["A"])
		self.assertEqual(plan_paths(list(reversed(tracks)), set()), (paths, collisions))
		self.assertEqual(canonical_relative_path({"title": "Hello"}), "Music/Unknown Artist/Unknown Album/Hello.mp3")

	def test_stale_file_stops_before_backup_or_relink(self):
		preview = self.preview()
		self.source.write_bytes(b"changed")
		with self.assertRaisesRegex(ValueError, "integrity changed"):
			self.execute(preview)
		self.assertEqual(self.current, self.baseline)
		self.assertFalse((self.paths.root / "Music").exists())

	def test_playlist_order_change_stops(self):
		preview = self.preview()
		self.current["playlists"][0]["tracks"] = ["A"]
		with self.assertRaisesRegex(ValueError, "playlist"):
			self.execute(preview)
		self.assertTrue(self.source.exists())

	def test_copy_relink_preservation_catalog_then_trash(self):
		preview = self.preview()
		journal = self.execute(preview)
		self.assertEqual(journal["state"], "verified_awaiting_user_acceptance")
		self.assertFalse(self.source.exists())
		self.assertEqual(Path(journal["tracks"]["A"]["trash"]).read_bytes(), b"synthetic audio")
		entry = load_manifest(self.paths)["catalog"]["entries"][library_id("A")]
		self.assertEqual(entry["location"], preview["tracks"][0]["target"])
		self.assertEqual(self.current["playlists"], self.baseline["playlists"])

	def test_interruption_after_relink_resumes_without_new_music_item(self):
		preview = self.preview()
		def crash(pid, path):
			self.relink(pid, path)
			raise RuntimeError("interrupted after Music accepted relink")
		with self.assertRaises(RuntimeError):
			migrate(self.paths, preview, snapshot=lambda: copy.deepcopy(self.current), relink=crash, music_package=self.package, inspect=inspect)
		self.assertTrue(self.source.exists())
		journal = self.execute(preview)
		self.assertEqual(journal["state"], "verified_awaiting_user_acceptance")
		self.assertEqual(len(self.current["tracks"]), 1)

	def test_failed_preservation_keeps_original(self):
		preview = self.preview()
		def bad_relink(pid, path):
			self.relink(pid, path)
			self.current["tracks"][0]["preservation"]["rating"] = 0
		with self.assertRaisesRegex(ValueError, "preservation"):
			migrate(self.paths, preview, snapshot=lambda: copy.deepcopy(self.current), relink=bad_relink, music_package=self.package, inspect=inspect)
		self.assertTrue(self.source.exists())

	def test_rollback_restores_original_location_and_catalog(self):
		preview = self.preview()
		self.execute(preview)
		journal = self.execute(preview, rollback=True)
		self.assertEqual(journal["state"], "rolled_back")
		self.assertEqual(self.current, self.baseline)
		self.assertEqual(self.source.read_bytes(), b"synthetic audio")

	def test_orphan_and_unknown_settings_block(self):
		(self.paths.root / "orphan.mp3").write_bytes(b"other")
		self.baseline["settings"]["copy_files"] = None
		preview = self.preview()
		self.assertFalse(preview["ready"])
		self.assertEqual(len(preview["orphans"]), 1)
		with self.assertRaisesRegex(ValueError, "prerequisites"):
			self.execute(preview)

	def test_backup_is_verified_and_never_overwritten(self):
		target = self.home / "backup.musiclibrary"
		backup_package(self.package, target)
		with self.assertRaises(ValueError):
			backup_package(self.package, target)
		self.assertEqual((target / "Library.musicdb").read_bytes(), b"library")

	def test_generated_playlist_preserves_comments_and_repeated_positions(self):
		from crate_music_importer.ipod_import.manifest import set_playlist, upsert_recording
		_, recording = upsert_recording(self.manifest, {"title": "Song", "artists": "Artist", "duration_ms": 180000})
		recording["music"] = {"persistent_id": "A", "location": str(self.source)}
		recording["managed_file"] = {"relative_path": "old.mp3", "sha256": file_sha256(self.source), "audio_sha256": file_sha256(self.source), "tool_owned": True}
		playlist = set_playlist(self.manifest, {"id": "P", "name": "My playlist"}, [{"recording_id": recording["recording_id"], "position": 1}, {"recording_id": recording["recording_id"], "position": 2}])
		playlist["m3u8_tool_owned"] = True
		path = self.paths.root / playlist["m3u8_relative_path"]
		path.parent.mkdir(exist_ok=True)
		original = f"#EXTM3U\n#Preserve this comment\n{self.source}\n{self.source}\n"
		path.write_text(original)
		save_manifest(self.paths, self.manifest)
		self.manifest = load_manifest(self.paths)
		preview = self.preview()
		self.execute(preview)
		self.assertEqual(path.read_text(), original.replace(str(self.source), preview["tracks"][0]["target"]))
		updated = load_manifest(self.paths)["recordings"][recording["recording_id"]]
		self.assertEqual(updated["music"]["persistent_id"], "A")
		self.assertEqual(updated["library_id"], library_id("A"))
		from crate_music_importer.ipod_import.manifest import managed_relative_path
		updated["album_metadata"] = {"album": "Upgraded Album", "album_artist": "Artist", "track_no": 2}
		self.assertEqual(managed_relative_path(updated), str(Path(preview["tracks"][0]["target"]).relative_to(self.paths.root)))

	def test_settings_receipt_is_invalidated_by_changed_preferences(self):
		from crate_music_importer.ipod_import.preservation import read_organization_settings, record_settings_observation
		with patch("crate_music_importer.ipod_import.preservation.settings_evidence", return_value={"prefs": "before"}), patch("crate_music_importer.ipod_import.manifest.ManagedPaths", return_value=self.paths):
			record_settings_observation(self.paths, keep_organized=False, copy_files=False, evidence="explicit observation")
			self.assertEqual(read_organization_settings()["copy_files"], False)
		with patch("crate_music_importer.ipod_import.preservation.settings_evidence", return_value={"prefs": "after"}), patch("crate_music_importer.ipod_import.manifest.ManagedPaths", return_value=self.paths):
			self.assertIsNone(read_organization_settings()["copy_files"])

	def test_backup_acceptance_never_empties_audio_trash(self):
		from crate_music_importer.ipod_import.unification import release_backup
		preview = self.preview()
		journal = self.execute(preview)
		audio_trash = Path(journal["tracks"]["A"]["trash"])
		result = release_backup(self.paths, preview["preview_id"])
		self.assertEqual(result["state"], "accepted")
		self.assertTrue(audio_trash.is_file())
		self.assertTrue(Path(result["backup_trash"]["manifest-backup.json"]).is_file())
		with self.assertRaisesRegex(ValueError, "Restore"):
			self.execute(preview, rollback=True)

	def test_catalog_rebuild_does_not_clobber_concurrent_manifest_change(self):
		from crate_music_importer.ipod_import.manifest import update_manifest
		def scan():
			update_manifest(self.paths, lambda value: value.update(concurrent="keep this"))
			return [self.track]
		with self.assertRaisesRegex(Exception, "changed during"):
			rebuild_music_cache(self.paths, self.manifest, scan=scan)
		self.assertEqual(load_manifest(self.paths)["concurrent"], "keep this")

	def test_finished_staging_mp3_is_never_cleanup(self):
		from crate_music_importer.ipod_import.working_files import review_working_files
		self.paths.staging.mkdir(exist_ok=True)
		(self.paths.staging / "finished.mp3").write_bytes(b"finished")
		row = review_working_files(self.paths, self.manifest)["files"][0]
		self.assertEqual(row["category"], "finished_audio")
		self.assertFalse(row["cleanup_eligible"])

	def test_cancellation_cannot_mutate_during_organization(self):
		from crate_music_importer.ipod_import.dependency_lock import dependency_lock, DependenciesBusyError
		from crate_music_importer.ipod_import.jobs import cancel_incomplete
		with dependency_lock(exclusive=True):
			with self.assertRaises(DependenciesBusyError):
				cancel_incomplete("irrelevant", root=self.paths.root)


if __name__ == "__main__":
	unittest.main()
