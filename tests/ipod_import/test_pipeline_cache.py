import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import.manifest import ManagedPaths, managed_relative_path, new_manifest, set_album, set_playlist, upsert_recording
from crate_music_importer.ipod_import.music_cache import (
	build_full_cache,
	load_music_cache,
	mark_music_cache_entry_stale,
	music_cache_tracks,
	remove_music_cache_tracks,
	save_music_cache,
	upsert_music_cache_track,
)
from crate_music_importer.ipod_import.pipeline import apply_album_to_music, apply_to_music


def cached_track(
	persistent_id: str,
	*,
	title: str,
	album: str = "Album",
	comment: str = "",
	location: str | None = None,
) -> dict:
	return {
		"persistent_id": persistent_id,
		"database_id": persistent_id.removeprefix("PID-") or "1",
		"title": title,
		"artist": "Artist",
		"album": album,
		"album_artist": "Artist" if album != "Playlist Imports" else "Various Artists",
		"duration_s": 180,
		"location": location or f"/Music/{title}.m4a",
		"comment": comment,
		"track_no": 1 if album != "Playlist Imports" else 0,
		"track_total": 1 if album != "Playlist Imports" else 0,
		"disc_no": 1 if album != "Playlist Imports" else 0,
		"disc_total": 1 if album != "Playlist Imports" else 0,
		"compilation": album == "Playlist Imports",
	}


class PipelineCacheTests(unittest.TestCase):
	def test_cache_write_failure_checkpoints_music_id_and_retry_does_not_import_again(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {"title": "Managed", "artists": "Artist", "duration_ms": 180000})
			relative = managed_relative_path(recording)
			managed_path = paths.root / relative
			managed_path.parent.mkdir(parents=True)
			managed_path.write_bytes(b"mp3")
			recording["managed_file"] = {"relative_path": relative, "tool_owned": True, "metadata_profile": "playlist"}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			set_playlist(manifest, {"id": "list", "name": "List", "url": "url", "complete": True}, [{"position": 1, "recording_id": key}])
			save_music_cache(paths, build_full_cache(paths, manifest, []))
			imported_value = {"persistent_id": "PID-NEW", "database_id": "3", "location": str(managed_path)}
			actual = cached_track(
				"PID-NEW",
				title="Managed",
				album="Playlist Imports",
				comment=f"Managed by Crate Music Importer; recording_id={key}",
				location=str(managed_path),
			)
			with patch("crate_music_importer.ipod_import.pipeline.playlist_status", return_value=("AVAILABLE", None)), \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file", return_value=imported_value) as imported, \
				patch("crate_music_importer.ipod_import.pipeline.sync_music_playlist", return_value="PLAYLIST") as synced:
				with self.assertRaisesRegex(Exception, "will not create a duplicate"):
					apply_to_music(
						manifest,
						"list",
						paths,
						[],
						exact_lookup=lambda persistent_id: actual,
						cache_updater=lambda value: (_ for _ in ()).throw(OSError("disk full")),
					)
				self.assertEqual(recording["music"]["persistent_id"], "PID-NEW")
				self.assertEqual(recording["cache_sync_pending"]["persistent_id"], "PID-NEW")
				self.assertEqual(imported.call_count, 1)
				synced.assert_not_called()

				apply_to_music(
					manifest,
					"list",
					paths,
					[],
					exact_lookup=lambda persistent_id: actual,
					cache_updater=lambda value: upsert_music_cache_track(paths, value),
				)
			self.assertEqual(imported.call_count, 1)
			self.assertNotIn("cache_sync_pending", recording)
			self.assertIn("PID-NEW", load_music_cache(paths)["tracks"])

	def test_playlist_apply_uses_only_relevant_exact_ids_and_caches_new_import(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			existing_key, existing = upsert_recording(manifest, {"title": "Existing", "artists": "Artist", "duration_ms": 180000})
			existing["music"] = {"source": "existing_library", "persistent_id": "PID-EXISTING", "database_id": "1", "location": "/Music/Existing.m4a"}
			existing["active_reference"] = {"kind": "existing_music", "persistent_id": "PID-EXISTING"}
			managed_key, managed = upsert_recording(manifest, {"title": "Managed", "artists": "Artist", "duration_ms": 180000})
			relative = managed_relative_path(managed)
			managed_path = paths.root / relative
			managed_path.parent.mkdir(parents=True)
			managed_path.write_bytes(b"mp3")
			managed["managed_file"] = {"relative_path": relative, "tool_owned": True, "metadata_profile": "playlist"}
			managed["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			set_playlist(manifest, {"id": "list", "name": "List", "url": "url", "complete": True}, [
				{"position": 1, "recording_id": existing_key},
				{"position": 2, "recording_id": managed_key},
				{"position": 3, "recording_id": managed_key},
			])
			library = [
				cached_track("PID-EXISTING", title="Existing"),
				cached_track("PID-UNRELATED", title="Unrelated"),
			]
			save_music_cache(paths, build_full_cache(paths, manifest, library))
			lookups = []

			def exact(persistent_id):
				lookups.append(persistent_id)
				return next((value for value in library if value["persistent_id"] == persistent_id), None)

			with patch("crate_music_importer.ipod_import.pipeline.playlist_status", return_value=("AVAILABLE", None)), \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file", return_value={"persistent_id": "PID-NEW", "database_id": "3", "location": str(managed_path)}) as imported, \
				patch("crate_music_importer.ipod_import.pipeline.sync_music_playlist", return_value="PLAYLIST"):
				result = apply_to_music(
					manifest,
					"list",
					paths,
					music_cache_tracks(load_music_cache(paths)),
					exact_lookup=exact,
					cache_updater=lambda value: upsert_music_cache_track(paths, value),
					mark_stale=lambda persistent_id, reason: mark_music_cache_entry_stale(paths, persistent_id, reason),
				)
			self.assertEqual(lookups, ["PID-EXISTING"])
			self.assertEqual(result["new_imports"], 1)
			imported.assert_called_once_with(managed_path, managed_key)
			cache = load_music_cache(paths)
			self.assertEqual(set(cache["tracks"]), {"PID-EXISTING", "PID-UNRELATED", "PID-NEW"})
			self.assertIn(f"recording_id={managed_key}", cache["tracks"]["PID-NEW"]["comment"])

	def test_missing_exact_id_marks_cache_stale_and_stops_before_music_write(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {"title": "Existing", "artists": "Artist", "duration_ms": 180000})
			recording["music"] = {"source": "existing_library", "persistent_id": "PID-MISSING"}
			recording["active_reference"] = {"kind": "existing_music", "persistent_id": "PID-MISSING"}
			set_playlist(manifest, {"id": "list", "name": "List", "url": "url", "complete": True}, [{"position": 1, "recording_id": key}])
			save_music_cache(paths, build_full_cache(paths, manifest, [cached_track("PID-MISSING", title="Existing")]))
			with patch("crate_music_importer.ipod_import.pipeline.playlist_status", return_value=("AVAILABLE", None)), \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as imported, \
				patch("crate_music_importer.ipod_import.pipeline.sync_music_playlist") as synced:
				with self.assertRaisesRegex(Exception, "Refresh Library Health"):
					apply_to_music(
						manifest,
						"list",
						paths,
						music_cache_tracks(load_music_cache(paths)),
						exact_lookup=lambda persistent_id: None,
						cache_updater=lambda value: upsert_music_cache_track(paths, value),
						mark_stale=lambda persistent_id, reason: mark_music_cache_entry_stale(paths, persistent_id, reason),
					)
			imported.assert_not_called()
			synced.assert_not_called()
			self.assertEqual(recording["review"]["kind"], "music_cache_stale")
			self.assertTrue(load_music_cache(paths)["tracks"]["PID-MISSING"]["stale"])

	def test_album_apply_exact_checks_only_album_id_and_updates_cache_in_place(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {
				"title": "Song", "artists": "Artist", "album": "Real Album", "album_id": "album",
				"duration_ms": 180000, "track_no": 1, "track_total": 1, "disc_no": 1, "disc_total": 1,
			}, source_type="album")
			relative = managed_relative_path(recording)
			managed_path = paths.root / relative
			managed_path.parent.mkdir(parents=True)
			managed_path.write_bytes(b"mp3")
			recording["managed_file"] = {"relative_path": relative, "tool_owned": True, "metadata_profile": "album", "spotify_album_id": "album"}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			recording["music"] = {"source": "managed_import", "persistent_id": "PID-MANAGED", "database_id": "1", "location": str(managed_path)}
			set_album(manifest, {"id": "album", "name": "Real Album", "album_artist": "Artist", "url": "url", "complete": True}, [{"position": 1, "recording_id": key}])
			comment = f"Managed by Crate Music Importer; recording_id={key}"
			old = cached_track("PID-MANAGED", title="Song", album="Playlist Imports", comment=comment, location=str(managed_path))
			unrelated = cached_track("PID-UNRELATED", title="Elsewhere")
			save_music_cache(paths, build_full_cache(paths, manifest, [old, unrelated]))
			lookups = []

			def exact(persistent_id):
				lookups.append(persistent_id)
				return old if persistent_id == "PID-MANAGED" else unrelated

			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=paths.staging / "art.jpg"), \
				patch("crate_music_importer.ipod_import.pipeline.update_managed_music_track") as updated, \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as imported:
				result = apply_album_to_music(
					manifest,
					"album",
					paths,
					music_cache_tracks(load_music_cache(paths)),
					exact_lookup=exact,
					cache_updater=lambda value: upsert_music_cache_track(paths, value),
					mark_stale=lambda persistent_id, reason: mark_music_cache_entry_stale(paths, persistent_id, reason),
				)
			self.assertEqual(lookups, ["PID-MANAGED"])
			self.assertEqual(result["updated_tracks"], 1)
			updated.assert_called_once()
			imported.assert_not_called()
			cache = load_music_cache(paths)
			self.assertEqual(cache["total_cached_tracks"], 2)
			self.assertEqual(cache["tracks"]["PID-MANAGED"]["album"], "Real Album")

	def test_album_apply_accepts_locationless_exact_lookup_with_cached_path_and_managed_sha(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {
				"title": "Song", "artists": "Artist", "album": "Real Album", "album_id": "album",
				"duration_ms": 180000, "track_no": 1, "track_total": 1, "disc_no": 1, "disc_total": 1,
			}, source_type="album")
			managed_path = paths.root / "Music" / "Compilations" / "Playlist Imports" / "Song.mp3"
			managed_path.parent.mkdir(parents=True)
			managed_path.write_bytes(b"managed-audio")
			relative = str(managed_path.relative_to(paths.root))
			recording["managed_file"] = {
				"relative_path": relative, "tool_owned": True, "metadata_profile": "album",
				"spotify_album_id": "album", "sha256": hashlib.sha256(b"managed-audio").hexdigest(),
			}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			recording["music"] = {"source": "managed_import", "persistent_id": "PID-MANAGED"}
			set_album(manifest, {"id": "album", "name": "Real Album", "url": "url", "complete": True}, [{"position": 1, "recording_id": key}])
			cached = cached_track("PID-MANAGED", title="Song", album="Playlist Imports", location=str(managed_path))
			live = dict(cached, album="Real Album", album_artist="Artist", location=None)

			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=paths.staging / "art.jpg"), \
				patch("crate_music_importer.ipod_import.pipeline.update_managed_music_track") as updated:
				result = apply_album_to_music(
					manifest,
					"album",
					paths,
					[cached],
					exact_lookup=lambda _persistent_id: live,
				)

			self.assertEqual(result["updated_tracks"], 1)
			updated.assert_called_once()

	def test_album_apply_repairs_missing_importer_owned_track_from_verified_managed_file(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {
				"title": "Song", "artists": "Artist", "album": "Real Album", "album_id": "album",
				"duration_ms": 180000, "track_no": 1, "track_total": 1, "disc_no": 1, "disc_total": 1,
			}, source_type="album")
			managed_path = paths.root / "Music" / "Artist" / "Real Album" / "01 — Song.mp3"
			managed_path.parent.mkdir(parents=True)
			managed_path.write_bytes(b"managed-audio")
			relative = str(managed_path.relative_to(paths.root))
			recording["managed_file"] = {
				"relative_path": relative, "tool_owned": True, "metadata_profile": "album",
				"spotify_album_id": "album", "sha256": hashlib.sha256(b"managed-audio").hexdigest(),
			}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			recording["music"] = {"source": "managed_album_import", "persistent_id": "PID-MISSING"}
			recording["review"] = {"kind": "music_cache_stale", "message": "The saved ID is missing."}
			recording["last_error"] = "The saved ID is missing."
			set_album(manifest, {"id": "album", "name": "Real Album", "url": "url", "complete": True}, [{"position": 1, "recording_id": key}])
			old = cached_track(
				"PID-MISSING", title="Song", album="Real Album",
				comment=f"Managed by Crate Music Importer; recording_id={key}", location=str(managed_path),
			)
			unrelated = cached_track("PID-UNRELATED", title="Elsewhere")
			save_music_cache(paths, build_full_cache(paths, manifest, [old, unrelated]))
			new_track = dict(old, persistent_id="PID-NEW", database_id="2")

			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=paths.staging / "art.jpg"), \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file", return_value=new_track) as imported:
				result = apply_album_to_music(
					manifest,
					"album",
					paths,
					music_cache_tracks(load_music_cache(paths)),
					exact_lookup=lambda _persistent_id: None,
					cache_updater=lambda value: upsert_music_cache_track(paths, value),
					cache_remover=lambda persistent_ids: remove_music_cache_tracks(paths, persistent_ids),
					owned_lookup=lambda _recording_id: None,
					verify_music=lambda persistent_ids: {persistent_id: new_track for persistent_id in persistent_ids},
				)

			self.assertEqual(result["stability_recoveries"], 1)
			self.assertEqual(recording["music"]["persistent_id"], "PID-NEW")
			self.assertNotIn("review", recording)
			self.assertNotIn("last_error", recording)
			imported.assert_called_once_with(managed_path, key)
			cache = load_music_cache(paths)
			self.assertEqual(set(cache["tracks"]), {"PID-UNRELATED", "PID-NEW"})


if __name__ == "__main__":
	unittest.main()
