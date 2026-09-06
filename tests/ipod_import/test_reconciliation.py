import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest, new_manifest, save_manifest
from crate_music_importer.ipod_import.reconciliation import (
	ReconciliationError,
	assert_audit_current,
	build_library_audit,
	migrate_library,
	reconcile_missing_manifest_references,
	verify_library_migration,
)


class ReconciliationTests(unittest.TestCase):
	def _track(self, persistent_id: str, path: Path, *, title: str = "Song", artist: str = "Artist") -> dict:
		return {
			"persistent_id": persistent_id,
			"database_id": persistent_id,
			"location": str(path),
			"title": title,
			"artist": artist,
			"album": "Album",
			"album_artist": artist,
			"duration_s": 180,
			"comment": "",
		}

	def test_audit_classifies_exact_and_semantic_duplicates_without_writing_media(self):
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			yt = base / "YT Albums"
			root = base / "mp3 Music"
			yt.mkdir()
			root.mkdir()
			one = yt / "one.mp3"
			two = root / "two.mp3"
			three = root / "three.mp3"
			one.write_bytes(b"same audio")
			two.write_bytes(b"same audio")
			three.write_bytes(b"different encode")
			paths = ManagedPaths(root)
			with patch("crate_music_importer.ipod_import.reconciliation.YT_ALBUMS_ROOT", yt):
				audit = build_library_audit(paths, new_manifest(paths), [
					self._track("ONE", one), self._track("TWO", two), self._track("THREE", three),
				])
			self.assertEqual(audit["source_groups"], {"managed_root": 2, "yt_albums": 1})
			self.assertEqual(len(audit["exact_duplicates"]), 1)
			self.assertEqual(len(audit["semantic_candidates"]), 1)
			self.assertEqual(one.read_bytes(), b"same audio")

	def test_migration_copies_relinks_verifies_then_retires_legacy_source(self):
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			yt = base / "YT Albums"
			root = base / "mp3 Music"
			trash = base / "Trash"
			yt.mkdir()
			root.mkdir()
			trash.mkdir()
			source = yt / "legacy.mp3"
			managed = root / "tracks" / "aa" / "managed.mp3"
			managed.parent.mkdir(parents=True)
			source.write_bytes(b"legacy audio")
			managed.write_bytes(b"managed audio")
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			with patch("crate_music_importer.ipod_import.reconciliation.YT_ALBUMS_ROOT", yt), \
				patch("crate_music_importer.ipod_import.reconciliation.MUSIC_LIBRARY_PACKAGE", base / "Library.musiclibrary"), \
				patch("crate_music_importer.ipod_import.reconciliation.PREVIOUS_LIBRARIES_ROOT", base / "Previous Libraries"):
				(base / "Library.musiclibrary").mkdir()
				audit = build_library_audit(paths, manifest, [self._track("LEGACY", source), self._track("MANAGED", managed)])
				relinked: dict[str, Path] = {}
				def relink(persistent_id: str, target: Path) -> dict:
					relinked[persistent_id] = target
					return {"persistent_id": persistent_id, "location": str(target)}
				def move_to_trash(value: Path) -> Path:
					target = trash / value.name
					os.replace(value, target)
					return target
				journal = migrate_library(paths, audit, [self._track("LEGACY", source), self._track("MANAGED", managed)], relink=relink, trash=move_to_trash)
				legacy_target = relinked["LEGACY"]
				self.assertTrue(legacy_target.is_file())
				self.assertFalse(source.exists())
				self.assertTrue((trash / "legacy.mp3").is_file())
				verify_library_migration(paths, audit, journal, [self._track("LEGACY", legacy_target), self._track("MANAGED", managed)])

	def test_stale_audit_refuses_before_any_copy(self):
		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			yt = base / "YT Albums"
			root = base / "mp3 Music"
			yt.mkdir()
			root.mkdir()
			source = yt / "legacy.mp3"
			source.write_bytes(b"first")
			paths = ManagedPaths(root)
			with patch("crate_music_importer.ipod_import.reconciliation.YT_ALBUMS_ROOT", yt):
				audit = build_library_audit(paths, new_manifest(paths), [self._track("PID", source)])
				source.write_bytes(b"changed")
				with self.assertRaisesRegex(ReconciliationError, "changed since this audit"):
					assert_audit_current(audit, [self._track("PID", source)])

	def test_missing_manifest_file_is_retired_without_touching_audio_or_music(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "mp3 Music"
			paths = ManagedPaths(root)
			paths.create()
			manifest = new_manifest(paths)
			recording_id = "rec_missing"
			manifest["recordings"][recording_id] = {
				"recording_id": recording_id,
				"source_metadata": {"artists": "Artist", "title": "Missing", "duration_ms": 1000},
				"managed_file": {"relative_path": "tracks/aa/missing.mp3", "tool_owned": True},
				"music": {"persistent_id": "GONE", "location": None},
				"active_reference": {"kind": "managed_file", "relative_path": "tracks/aa/missing.mp3"},
			}
			manifest["playlists"]["playlist"] = {
				"name": "Fixture", "items": [{"position": 1, "recording_id": recording_id}],
				"m3u8_relative_path": "playlists/fixture.m3u8", "m3u8_tool_owned": False,
			}
			save_manifest(paths, manifest)
			result = reconcile_missing_manifest_references(paths, [])
			updated = load_manifest(paths)["recordings"][recording_id]
			self.assertEqual(result, {"resolved_to_music": 0, "marked_unavailable": 1, "rewritten_playlists": 1})
			self.assertIsNone(updated["managed_file"])
			self.assertEqual(updated["active_reference"]["kind"], "unavailable_historical_reference")
			playlist = (paths.root / "playlists/fixture.m3u8").read_text(encoding="utf-8")
			self.assertIn("#UNAVAILABLE-HISTORICAL-RECORDING:rec_missing", playlist)
			self.assertNotIn("tracks/aa/missing.mp3", playlist)


if __name__ == "__main__":
	unittest.main()
