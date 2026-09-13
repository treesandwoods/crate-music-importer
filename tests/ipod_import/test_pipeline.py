import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import.manifest import ManagedPaths, managed_relative_path, new_manifest, set_album, set_playlist, upsert_recording
from crate_music_importer.ipod_import.media import MediaError
from crate_music_importer.ipod_import.music import load_music_fixture
from crate_music_importer.ipod_import.pipeline import (
	apply_album_to_music,
	apply_to_music,
	build_album_metadata_preview,
	build_album_preview,
	build_preview,
	execute_album_import,
	execute_import,
	promote_recording,
)
from crate_music_importer.ipod_import.resolver import resolve_youtube
from crate_music_importer.ipod_import.spotify import load_fixture


FIXTURES = Path(__file__).parent / "fixtures"


class PipelineTests(unittest.TestCase):
	def test_album_metadata_preview_lists_tracks_without_music_or_media_checks(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			album = load_fixture(FIXTURES / "spotify_album.json").to_dict()
			with patch("crate_music_importer.ipod_import.pipeline.managed_duration_is_valid") as duration_check:
				preview = build_album_metadata_preview(album, new_manifest(paths))
			self.assertEqual(preview.counts, {"not_started": 2})
			self.assertEqual([row["status"] for row in preview.rows], ["not_started", "not_started"])
			self.assertFalse(paths.root.exists())
			duration_check.assert_not_called()

	def test_previews_use_clean_titles_and_match_a_clean_existing_album(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			album = {
				"id": "album-id",
				"name": "Album (Deluxe Edition)",
				"album_artist": "Artist",
				"url": "https://open.spotify.com/album/album-id",
				"tracks": [{
					"position": 1,
					"title": "Song (Live) - 2005 Remaster",
					"artists": "Artist",
					"album": "Album (Deluxe Edition)",
					"album_artist": "Artist",
					"album_id": "album-id",
					"duration_ms": 180000,
					"sp_id": "track-id",
				}],
				"total_count": 1,
				"complete": True,
			}
			music = [{
				"title": "Song (Live)",
				"artist": "Artist",
				"album": "Album",
				"duration_s": 180,
				"persistent_id": "PID",
				"database_id": "1",
				"location": "/Music/Album/Song.m4a",
				"comment": "",
			}]
			preview = build_album_preview(album, music, new_manifest(paths), paths)
			self.assertEqual(preview.counts, {"reused_music": 1})
			self.assertEqual(preview.rows[0]["title"], "Song (Live)")
			self.assertEqual(preview.manifest["albums"]["album-id"]["name"], "Album")

			playlist = dict(album, id="playlist-id", name="Playlist", source_type="playlist")
			playlist_preview = build_preview(playlist, music, new_manifest(paths), paths)
			self.assertEqual(playlist_preview.rows[0]["title"], "Song (Live)")

	def test_album_first_tracks_are_reused_by_later_spotify_playlist(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			album = load_fixture(FIXTURES / "spotify_album.json").to_dict()
			music = [
				{
					"title": track["title"],
					"artist": track["artists"],
					"album": "Visions",
					"duration_s": track["duration_ms"] / 1000,
					"persistent_id": f"ALBUM-PID-{index}",
					"database_id": str(index),
					"location": f"/Music/Visions/{index}.m4a",
					"comment": "",
				}
				for index, track in enumerate(album["tracks"], start=1)
			]
			album_preview = build_album_preview(album, music, new_manifest(paths), paths)
			self.assertEqual(album_preview.counts, {"reused_music": 2})
			playlist = load_fixture(FIXTURES / "spotify_playlist.json").to_dict()
			playlist_preview = build_preview(playlist, music, album_preview.manifest, paths)
			self.assertEqual(playlist_preview.rows[1]["status"], "reused_music")
			self.assertEqual(playlist_preview.rows[3]["status"], "reused_music")
			self.assertEqual(playlist_preview.rows[1]["recording_id"], playlist_preview.rows[3]["recording_id"])

	def test_playlist_import_keeps_reused_album_music_ready_when_managed_source_is_gone(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest = new_manifest(paths)
			playlist = {
				"id": "playlist-id",
				"name": "Album Reuse",
				"url": "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1",
				"tracks": [{
					"position": 1,
					"title": "Album Song",
					"artists": "Artist",
					"album": "Album",
					"duration_ms": 180000,
					"sp_id": "album-track-id",
				}],
				"total_count": 1,
				"complete": True,
			}
			key, recording = upsert_recording(manifest, playlist["tracks"][0], source_type="album")
			recording["managed_file"] = {
				"relative_path": "tracks/missing-album-source.mp3",
				"tool_owned": True,
				"metadata_profile": "album",
			}
			recording["active_reference"] = {
				"kind": "managed_file",
				"relative_path": "tracks/missing-album-source.mp3",
			}
			recording["music"] = {
				"source": "managed_album_import",
				"persistent_id": "ALBUM-MUSIC-PID",
				"database_id": "10",
				"location": None,
			}
			music = [{
				"title": "Album Song",
				"artist": "Artist",
				"album": "Album",
				"duration_s": 180,
				"persistent_id": "ALBUM-MUSIC-PID",
				"database_id": "10",
				"location": None,
				"comment": f"Managed by Crate Music Importer; recording_id={key}",
			}]
			preview = build_preview(playlist, music, manifest, paths)
			self.assertEqual(preview.rows[0]["status"], "reused_music")

			result = execute_import(preview, paths)

			self.assertEqual(result["playlist"]["items"][0]["status"], "reused_music")
			self.assertFalse((paths.root / "tracks/missing-album-source.mp3").exists())

	def test_playlist_revalidates_marker_only_stale_music_without_editing_track(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			track = {
				"position": 1,
				"title": "Album Song",
				"artists": "Artist",
				"album": "Album",
				"duration_ms": 180000,
				"sp_id": "album-track-id",
			}
			key, recording = upsert_recording(manifest, track, source_type="album")
			relative = "tracks/album-song.mp3"
			managed_path = paths.root / relative
			recording["managed_file"] = {"relative_path": relative, "tool_owned": True, "metadata_profile": "album"}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			recording["music"] = {"source": "managed_album_import", "persistent_id": "ALBUM-PID"}
			recording["review"] = {"kind": "music_cache_stale", "message": "missing marker", "candidates": []}
			recording["last_error"] = "missing marker"
			playlist = {
				"id": "playlist-id",
				"name": "Album Reuse",
				"url": "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1",
				"tracks": [track],
				"total_count": 1,
				"complete": True,
			}
			music_track = {
				"title": "Album Song",
				"artist": "Artist",
				"album": "Album",
				"duration_s": 180,
				"persistent_id": "ALBUM-PID",
				"database_id": "10",
				"location": None,
				"comment": "",
				"stale": True,
				"stale_reason": "the importer ownership marker is missing or changed",
			}
			preview = build_preview(playlist, [music_track], manifest, paths)
			self.assertEqual(preview.rows[0]["status"], "reused_music")
			self.assertNotIn("review", preview.manifest["recordings"][key])
			prepared = execute_import(preview, paths)
			refreshed = []
			with patch("crate_music_importer.ipod_import.pipeline.playlist_status", return_value=("AVAILABLE", None)), \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as imported, \
				patch("crate_music_importer.ipod_import.pipeline.sync_music_playlist", return_value="PLAYLIST-PID"):
				result = apply_to_music(
					prepared["manifest"],
					"playlist-id",
					paths,
					[music_track],
					exact_lookup=lambda _persistent_id: dict(music_track),
					cache_updater=refreshed.append,
				)
			self.assertEqual(result["track_count"], 1)
			self.assertEqual(result["new_imports"], 0)
			self.assertEqual(len(refreshed), 1)
			imported.assert_not_called()

	def test_playlist_first_managed_track_is_upgraded_in_place_for_album(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			album_fixture = load_fixture(FIXTURES / "spotify_album.json").to_dict()
			track = dict(album_fixture["tracks"][1])
			key, recording = upsert_recording(manifest, track)
			relative = managed_relative_path(recording)
			target = paths.root / relative
			target.parent.mkdir(parents=True, exist_ok=True)
			target.write_bytes(b"managed-playlist-mp3")
			recording["managed_file"] = {
				"relative_path": relative,
				"sha256": "fixture",
				"duration_ms": track["duration_ms"],
				"tool_owned": True,
				"metadata_profile": "playlist",
			}
			recording["music"] = {
				"source": "managed_import",
				"persistent_id": "MANAGED-PID",
				"database_id": "10",
				"location": str(target),
			}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			album = dict(album_fixture, tracks=[track], total_count=1)
			music = [{
				"title": "Genesis",
				"artist": "Grimes",
				"album": "Playlist Imports",
				"duration_s": 255,
				"persistent_id": "MANAGED-PID",
				"database_id": "10",
				"location": str(target),
				"comment": f"Managed by Crate Music Importer; recording_id={key}",
			}]
			with patch("crate_music_importer.ipod_import.pipeline.managed_duration_is_valid", return_value=True):
				preview = build_album_preview(album, music, manifest, paths)
			self.assertEqual(preview.rows[0]["status"], "upgrade_managed")

			def retag(value, managed_paths, on_output=None):
				updated = dict(value["managed_file"])
				updated.update({"metadata_profile": "album", "spotify_album_id": album["id"]})
				return updated

			result = execute_album_import(preview, paths, retag=retag)
			self.assertEqual(result["album"]["items"][0]["status"], "managed_ready")
			self.assertEqual(len(result["manifest"]["recordings"]), 1)
			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=paths.staging / "art.jpg"), \
				patch("crate_music_importer.ipod_import.pipeline.update_managed_music_track") as updated_music, \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as imported:
				applied = apply_album_to_music(result["manifest"], album["id"], paths, music)
			self.assertEqual(applied["new_imports"], 0)
			self.assertEqual(applied["updated_tracks"], 1)
			imported.assert_not_called()
			updated_music.assert_called_once()
			self.assertEqual(updated_music.call_args.args[0], "MANAGED-PID")
			self.assertEqual(result["manifest"]["recordings"][key]["music"]["persistent_id"], "MANAGED-PID")

	def test_album_preview_refuses_to_edit_user_owned_different_album(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			album_fixture = load_fixture(FIXTURES / "spotify_album.json").to_dict()
			track = dict(album_fixture["tracks"][1])
			album = dict(album_fixture, tracks=[track], total_count=1)
			music = [{
				"title": "Genesis",
				"artist": "Grimes",
				"album": "Greatest Hits",
				"duration_s": 255,
				"persistent_id": "USER-PID",
				"database_id": "20",
				"location": "/Music/User/Genesis.m4a",
				"comment": "",
			}]
			preview = build_album_preview(album, music, new_manifest(paths), paths)
			self.assertEqual(preview.rows[0]["status"], "review_music")
			recording = preview.manifest["recordings"][preview.rows[0]["recording_id"]]
			self.assertEqual(recording["review"]["kind"], "album_conflict")

	def test_album_preview_recovers_music_organized_playlist_import_for_in_place_upgrade(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			album_fixture = load_fixture(FIXTURES / "spotify_album.json").to_dict()
			track = dict(album_fixture["tracks"][1])
			album = dict(album_fixture, tracks=[track], total_count=1)
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, track)
			moved = paths.root / "Music" / "Compilations" / "Playlist Imports" / "Genesis.mp3"
			moved.parent.mkdir(parents=True)
			moved.write_bytes(b"managed-audio")
			recording["managed_file"] = {
				"relative_path": "tracks/missing.mp3", "tool_owned": True, "metadata_profile": "playlist",
				"sha256": hashlib.sha256(b"managed-audio").hexdigest(),
			}
			recording["music"] = {"source": "managed_import", "persistent_id": "MANAGED-PID"}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": "tracks/missing.mp3"}
			recording["review"] = {"kind": "album_conflict", "message": "stale conflict", "candidates": []}
			music = [{
				"title": "Genesis", "artist": "Grimes", "album": "Playlist Imports", "duration_s": 255,
				"persistent_id": "MANAGED-PID", "database_id": "10", "location": str(moved), "comment": "",
			}]

			with patch("crate_music_importer.ipod_import.pipeline.managed_duration_is_valid", return_value=True):
				preview = build_album_preview(album, music, manifest, paths)

			self.assertEqual(preview.rows[0]["status"], "upgrade_managed")
			recovered = preview.manifest["recordings"][key]
			self.assertEqual(recovered["managed_file"]["relative_path"], "Music/Compilations/Playlist Imports/Genesis.mp3")
			self.assertNotIn("review", recovered)

	def test_album_preview_clears_expected_stale_flag_after_file_side_promotion(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			album_fixture = load_fixture(FIXTURES / "spotify_album.json").to_dict()
			track = dict(album_fixture["tracks"][1])
			album = dict(album_fixture, tracks=[track], total_count=1)
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, track, source_type="album")
			promoted = paths.root / "Music" / "Compilations" / "Album" / "Genesis.mp3"
			promoted.parent.mkdir(parents=True)
			promoted.write_bytes(b"managed-audio")
			relative = str(promoted.relative_to(paths.root))
			recording["managed_file"] = {
				"relative_path": relative, "tool_owned": True, "metadata_profile": "album",
				"spotify_album_id": album["id"], "sha256": hashlib.sha256(b"managed-audio").hexdigest(),
			}
			recording["music"] = {"source": "managed_import", "persistent_id": "MANAGED-PID"}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			recording["review"] = {"kind": "music_cache_stale", "message": "stale", "candidates": []}
			recording["last_error"] = "stale"
			stale = {
				"title": "Genesis", "artist": "Grimes", "album": "Playlist Imports", "duration_s": 255,
				"persistent_id": "MANAGED-PID", "database_id": "10", "location": str(promoted), "comment": "",
				"stale": True, "stale_reason": "the Music album changed",
			}

			preview = build_album_preview(album, [stale], manifest, paths)

			self.assertEqual(preview.rows[0]["status"], "managed_existing")
			self.assertNotIn("review", preview.manifest["recordings"][key])
			self.assertNotIn("last_error", preview.manifest["recordings"][key])

	def test_new_album_import_uses_embedded_tags_without_immediate_music_rewrite(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			track = {
				"position": 1,
				"track_no": 1,
				"track_total": 1,
				"disc_no": 1,
				"disc_total": 1,
				"title": "Album Song",
				"artists": "Artist",
				"album": "Album",
				"album_artist": "Artist",
				"album_id": "album-id",
				"duration_ms": 180000,
				"sp_id": "track-id",
			}
			key, recording = upsert_recording(manifest, track, source_type="album")
			relative = managed_relative_path(recording)
			managed_path = paths.root / relative
			managed_path.parent.mkdir(parents=True)
			managed_path.write_bytes(b"album-mp3")
			recording["managed_file"] = {"relative_path": relative, "tool_owned": True, "metadata_profile": "album"}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			set_album(
				manifest,
				{"id": "album-id", "name": "Album", "album_artist": "Artist", "url": "url", "total_count": 1},
				[{"position": 1, "track_no": 1, "disc_no": 1, "recording_id": key, "spotify_id": "track-id", "status": "managed_ready"}],
			)
			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=paths.staging / "art.jpg"), \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file", return_value={"persistent_id": "NEW-PID", "database_id": "2", "location": str(managed_path)}) as imported, \
				patch("crate_music_importer.ipod_import.pipeline.update_managed_music_track") as updated_music:
				result = apply_album_to_music(manifest, "album-id", paths, [])
			self.assertEqual(result["new_imports"], 1)
			self.assertEqual(result["recovered_imports"], 0)
			imported.assert_called_once_with(managed_path, key)
			updated_music.assert_not_called()
			self.assertEqual(recording["music"]["source"], "managed_album_import")
			self.assertNotIn("music_import_pending", recording)

	def test_album_apply_recovers_a_new_music_addition_that_disappears_before_completion(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			track = {
				"position": 1, "track_no": 1, "track_total": 1, "disc_no": 1, "disc_total": 1,
				"title": "Album Song", "artists": "Artist", "album": "Album", "album_artist": "Artist",
				"album_id": "album-id", "duration_ms": 180000, "sp_id": "track-id",
			}
			key, recording = upsert_recording(manifest, track, source_type="album")
			relative = managed_relative_path(recording)
			managed_path = paths.root / relative
			managed_path.parent.mkdir(parents=True)
			managed_path.write_bytes(b"album-mp3")
			recording["managed_file"] = {"relative_path": relative, "tool_owned": True, "metadata_profile": "album"}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			set_album(
				manifest,
				{"id": "album-id", "name": "Album", "album_artist": "Artist", "url": "url", "total_count": 1},
				[{"position": 1, "track_no": 1, "disc_no": 1, "recording_id": key, "spotify_id": "track-id", "status": "managed_ready"}],
			)
			verification_results = iter((
				{},
				{"RETRY-PID": {"persistent_id": "RETRY-PID"}},
				{"RETRY-PID": {"persistent_id": "RETRY-PID"}},
			))
			cache_updates = []
			removed = []
			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=paths.staging / "art.jpg"), \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file", side_effect=(
					{"persistent_id": "FIRST-PID", "database_id": "2", "location": str(managed_path)},
					{"persistent_id": "RETRY-PID", "database_id": "3", "location": str(managed_path)},
				)) as imported:
				result = apply_album_to_music(
					manifest,
					"album-id",
					paths,
					[],
					cache_updater=cache_updates.append,
					cache_remover=lambda persistent_ids: removed.append(persistent_ids) or len(persistent_ids),
					owned_lookup=lambda _recording_id: None,
					verify_music=lambda _persistent_ids: next(verification_results),
				)
			self.assertEqual(result["stability_recoveries"], 1)
			self.assertEqual(recording["music"]["persistent_id"], "RETRY-PID")
			self.assertEqual(imported.call_args_list[0].args, (managed_path, key))
			self.assertEqual(imported.call_args_list[1].args, (managed_path, key))
			self.assertEqual(removed, [{"FIRST-PID"}])
			self.assertEqual([value["persistent_id"] for value in cache_updates], ["FIRST-PID", "RETRY-PID"])

	def test_album_apply_stabilizes_the_first_new_addition_before_importing_the_rest(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			items = []
			recording_ids = []
			for position in (1, 2):
				track = {
					"position": position, "track_no": position, "track_total": 2, "disc_no": 1, "disc_total": 1,
					"title": f"Album Song {position}", "artists": "Artist", "album": "Album", "album_artist": "Artist",
					"album_id": "album-id", "duration_ms": 180000, "sp_id": f"track-{position}",
				}
				key, recording = upsert_recording(manifest, track, source_type="album")
				relative = managed_relative_path(recording)
				managed_path = paths.root / relative
				managed_path.parent.mkdir(parents=True, exist_ok=True)
				managed_path.write_bytes(b"album-mp3")
				recording["managed_file"] = {"relative_path": relative, "tool_owned": True, "metadata_profile": "album"}
				recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
				items.append({"position": position, "track_no": position, "disc_no": 1, "recording_id": key, "spotify_id": f"track-{position}", "status": "managed_ready"})
				recording_ids.append(key)
			set_album(
				manifest,
				{"id": "album-id", "name": "Album", "album_artist": "Artist", "url": "url", "total_count": 2},
				items,
			)
			events = []

			def import_track(_path, recording_id):
				position = recording_ids.index(recording_id) + 1
				persistent_id = f"PID-{position}"
				events.append(f"import:{persistent_id}")
				return {"persistent_id": persistent_id, "database_id": str(position), "location": str(_path)}

			def verify_tracks(persistent_ids):
				events.append("verify:" + ",".join(persistent_ids))
				return {persistent_id: {"persistent_id": persistent_id} for persistent_id in persistent_ids}

			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=paths.staging / "art.jpg"), \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file", side_effect=import_track):
				result = apply_album_to_music(manifest, "album-id", paths, [], verify_music=verify_tracks)
			self.assertEqual(result["new_imports"], 2)
			self.assertEqual(result["stability_recoveries"], 0)
			self.assertEqual(events, ["import:PID-1", "verify:PID-1", "import:PID-2", "verify:PID-1,PID-2"])

	def test_album_apply_adopts_a_partial_import_by_managed_comment_without_duplication(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			track = {
				"position": 1,
				"track_no": 1,
				"track_total": 1,
				"disc_no": 1,
				"disc_total": 1,
				"title": "Recovered Song",
				"artists": "Artist",
				"album": "Album",
				"album_artist": "Artist",
				"album_id": "album-id",
				"duration_ms": 180000,
				"sp_id": "track-id",
			}
			key, recording = upsert_recording(manifest, track, source_type="album")
			relative = managed_relative_path(recording)
			managed_path = paths.root / relative
			managed_path.parent.mkdir(parents=True)
			managed_path.write_bytes(b"album-mp3")
			recording["managed_file"] = {"relative_path": relative, "tool_owned": True, "metadata_profile": "album"}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			set_album(
				manifest,
				{"id": "album-id", "name": "Album", "album_artist": "Artist", "url": "url", "total_count": 1},
				[{"position": 1, "track_no": 1, "disc_no": 1, "recording_id": key, "spotify_id": "track-id", "status": "managed_ready"}],
			)
			music = [{
				"title": "Recovered Song",
				"artist": "Artist",
				"album": "Album",
				"duration_s": 180,
				"persistent_id": "RECOVERED-PID",
				"database_id": "3",
				"location": str(managed_path),
				"comment": f"Managed by Crate Music Importer; recording_id={key}",
			}]
			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=paths.staging / "art.jpg"), \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as imported, \
				patch("crate_music_importer.ipod_import.pipeline.update_managed_music_track") as updated_music:
				result = apply_album_to_music(manifest, "album-id", paths, music)
			self.assertEqual(result["new_imports"], 0)
			self.assertEqual(result["recovered_imports"], 1)
			imported.assert_not_called()
			updated_music.assert_not_called()
			self.assertEqual(recording["music"]["persistent_id"], "RECOVERED-PID")
			self.assertEqual(recording["music"]["source"], "managed_album_import")

	def test_album_apply_refuses_to_duplicate_an_unreconciled_pending_import(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			track = {
				"position": 1,
				"track_no": 1,
				"track_total": 1,
				"disc_no": 1,
				"disc_total": 1,
				"title": "Pending Song",
				"artists": "Artist",
				"album": "Album",
				"album_artist": "Artist",
				"album_id": "album-id",
				"duration_ms": 180000,
				"sp_id": "track-id",
			}
			key, recording = upsert_recording(manifest, track, source_type="album")
			relative = managed_relative_path(recording)
			managed_path = paths.root / relative
			managed_path.parent.mkdir(parents=True)
			managed_path.write_bytes(b"album-mp3")
			recording["managed_file"] = {"relative_path": relative, "tool_owned": True, "metadata_profile": "album"}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			recording["music_import_pending"] = {"relative_path": relative, "started_at": "fixture"}
			set_album(
				manifest,
				{"id": "album-id", "name": "Album", "album_artist": "Artist", "url": "url", "total_count": 1},
				[{"position": 1, "track_no": 1, "disc_no": 1, "recording_id": key, "spotify_id": "track-id", "status": "managed_ready"}],
			)
			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=paths.staging / "art.jpg"), \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as imported:
				with self.assertRaisesRegex(Exception, "refusing to create a possible duplicate"):
					apply_album_to_music(manifest, "album-id", paths, [])
			imported.assert_not_called()

	def test_album_preview_does_not_silently_move_recording_between_real_albums(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			album_fixture = load_fixture(FIXTURES / "spotify_album.json").to_dict()
			track = dict(album_fixture["tracks"][1])
			key, recording = upsert_recording(manifest, track)
			relative = managed_relative_path(recording)
			target = paths.root / relative
			target.parent.mkdir(parents=True, exist_ok=True)
			target.write_bytes(b"existing-real-album")
			recording["managed_file"] = {
				"relative_path": relative,
				"tool_owned": True,
				"metadata_profile": "album",
				"spotify_album_id": "different-album-id",
			}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			album = dict(album_fixture, tracks=[track], total_count=1)
			with patch("crate_music_importer.ipod_import.pipeline.managed_duration_is_valid", return_value=True):
				preview = build_album_preview(album, [], manifest, paths)
			self.assertEqual(preview.rows[0]["status"], "review_music")
			self.assertEqual(preview.manifest["recordings"][key]["review"]["kind"], "album_release_conflict")

	def test_album_preview_surfaces_preexisting_album_and_managed_playlist_duplicate(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			album_fixture = load_fixture(FIXTURES / "spotify_album.json").to_dict()
			track = dict(album_fixture["tracks"][1])
			key, recording = upsert_recording(manifest, track)
			relative = managed_relative_path(recording)
			target = paths.root / relative
			target.parent.mkdir(parents=True, exist_ok=True)
			target.write_bytes(b"managed-playlist-copy")
			recording["managed_file"] = {"relative_path": relative, "tool_owned": True, "metadata_profile": "playlist"}
			recording["music"] = {"source": "managed_import", "persistent_id": "MANAGED-PID", "location": str(target)}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			album = dict(album_fixture, tracks=[track], total_count=1)
			music = [
				{
					"title": "Genesis", "artist": "Grimes", "album": "Playlist Imports", "duration_s": 255,
					"persistent_id": "MANAGED-PID", "database_id": "1", "location": str(target),
					"comment": f"Managed by Crate Music Importer; recording_id={key}",
				},
				{
					"title": "Genesis", "artist": "Grimes", "album": "Visions", "duration_s": 255,
					"persistent_id": "USER-ALBUM-PID", "database_id": "2", "location": "/Music/Visions/Genesis.m4a", "comment": "",
				},
			]
			with patch("crate_music_importer.ipod_import.pipeline.managed_duration_is_valid", return_value=True):
				preview = build_album_preview(album, music, manifest, paths)
			self.assertEqual(preview.rows[0]["status"], "review_music")
			self.assertEqual(preview.manifest["recordings"][key]["review"]["kind"], "album_duplicate_conflict")

	def test_fixture_preview_is_read_only_and_conservative(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			playlist = load_fixture(FIXTURES / "spotify_playlist.json").to_dict()
			music = load_music_fixture(FIXTURES / "music_library.json")
			preview = build_preview(playlist, music, manifest, paths)
			self.assertEqual(preview.counts, {"reused_music": 1, "search_required": 3})
			self.assertEqual([row["position"] for row in preview.rows], [1, 2, 3, 4])
			self.assertFalse(paths.root.exists())
			self.assertEqual(len(preview.manifest["recordings"]), 3)

	def test_import_processes_each_unmatched_recording_once_and_keeps_duplicate_positions(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			playlist = load_fixture(FIXTURES / "spotify_playlist.json").to_dict()
			music = load_music_fixture(FIXTURES / "music_library.json")
			preview = build_preview(playlist, music, new_manifest(paths), paths)
			calls = []
			progress = []

			def search(track):
				return [{
					"video_id": "fixture-video-" + str(len(calls)),
					"url": "https://www.youtube.com/watch?v=fixture",
					"title": track["title"],
					"uploader": track["artists"] + " - Topic",
					"duration_s": track["duration_ms"] / 1000,
					"score": 0.99,
					"metadata_verified": True,
					"reasons": [],
				}]

			def download(recording, managed_paths, on_output=None):
				calls.append(recording["recording_id"])
				relative = managed_relative_path(recording)
				target = managed_paths.root / relative
				target.parent.mkdir(parents=True, exist_ok=True)
				target.write_bytes(b"fixture-mp3")
				return {"relative_path": relative, "sha256": "fixture", "duration_ms": recording["source_metadata"]["duration_ms"], "tool_owned": True}

			result = execute_import(preview, paths, search=search, download=download, on_progress=progress.append)
			self.assertEqual(len(calls), 2)
			self.assertIn("reused", [event["phase"] for event in progress])
			self.assertIn("searching_youtube", [event["phase"] for event in progress])
			self.assertIn("youtube_match_found", [event["phase"] for event in progress])
			self.assertIn("downloading", [event["phase"] for event in progress])
			self.assertIn("downloaded", [event["phase"] for event in progress])
			self.assertTrue(all(event.get("recording_id") for event in progress))
			statuses = [item["status"] for item in result["playlist"]["items"]]
			self.assertEqual(statuses, ["reused_music", "managed_ready", "managed_ready", "managed_ready"])
			self.assertTrue(Path(result["m3u8"]).is_file())

	def test_album_import_accepts_manual_choice_made_while_youtube_search_is_running(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			album = load_fixture(FIXTURES / "spotify_album.json").to_dict()
			album["tracks"] = album["tracks"][:1]
			album["total_count"] = 1
			preview = build_album_preview(album, [], new_manifest(paths), paths)
			chosen_key = preview.rows[0]["recording_id"]
			downloaded = []

			def search(_metadata):
				resolve_youtube(chosen_key, "https://www.youtube.com/watch?v=chosen", paths=paths, inspect=lambda _url: {
					"video_id": "chosen",
					"url": "https://www.youtube.com/watch?v=chosen",
					"title": album["tracks"][0]["title"],
					"uploader": f"{album['tracks'][0]['artists']} - Topic",
					"duration_s": album["tracks"][0]["duration_ms"] / 1000,
					"reasons": [],
				})
				return []

			def download(recording, managed_paths, on_output=None):
				downloaded.append(recording["youtube"]["video_id"])
				relative = managed_relative_path(recording)
				target = managed_paths.root / relative
				target.parent.mkdir(parents=True, exist_ok=True)
				target.write_bytes(b"fixture-mp3")
				return {"relative_path": relative, "sha256": "fixture", "duration_ms": recording["source_metadata"]["duration_ms"], "tool_owned": True, "metadata_profile": "album"}

			result = execute_album_import(preview, paths, search=search, download=download)
			self.assertEqual(downloaded[0], "chosen")
			self.assertEqual(result["album"]["items"][0]["status"], "managed_ready")

	def test_preview_schedules_registered_invalid_managed_mp3_for_rebuild(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {
				"title": "Damaged",
				"artists": "Artist",
				"duration_ms": 180000,
				"sp_id": "damaged-track",
			})
			relative = managed_relative_path(recording)
			target = paths.root / relative
			target.parent.mkdir(parents=True)
			target.write_bytes(b"registered-but-invalid")
			recording["managed_file"] = {"relative_path": relative, "tool_owned": True}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			playlist = {
				"id": "playlist",
				"name": "Repair",
				"url": "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1",
				"tracks": [{
					"position": 1,
					"title": "Damaged",
					"artists": "Artist",
					"duration_ms": 180000,
					"sp_id": "damaged-track",
				}],
				"total_count": 1,
				"complete": True,
			}
			with patch("crate_music_importer.ipod_import.pipeline.managed_duration_is_valid", return_value=False):
				preview = build_preview(playlist, [], manifest, paths)
			self.assertEqual(preview.rows[0]["recording_id"], key)
			self.assertEqual(preview.rows[0]["status"], "ready_to_download")
			self.assertIn("rebuild required", preview.rows[0]["detail"])

	def test_reimport_repairs_stale_playlist_artwork_without_redownloading_audio(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {
				"title": "Artwork Repair",
				"artists": "Artist",
				"album": "Original Album",
				"duration_ms": 180000,
				"sp_id": "artwork-repair-track",
				"cover_url": "https://example.test/playlist-cover.jpg",
			})
			relative = managed_relative_path(recording)
			target = paths.root / relative
			target.parent.mkdir(parents=True)
			target.write_bytes(b"managed-playlist-mp3")
			recording["managed_file"] = {
				"relative_path": relative,
				"tool_owned": True,
				"metadata_profile": "playlist",
				"artwork_source_url": "https://example.test/playlist-cover.jpg",
			}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			playlist = {
				"id": "playlist",
				"name": "Artwork Repair",
				"url": "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1",
				"tracks": [{
					"position": 1,
					"title": "Artwork Repair",
					"artists": "Artist",
					"album": "Original Album",
					"duration_ms": 180000,
					"sp_id": "artwork-repair-track",
					"cover_url": "https://example.test/correct-album.jpg",
				}],
				"total_count": 1,
				"complete": True,
			}
			with patch("crate_music_importer.ipod_import.pipeline.managed_duration_is_valid", return_value=True):
				preview = build_preview(playlist, [], manifest, paths)
			self.assertEqual(preview.rows[0]["recording_id"], key)
			self.assertEqual(preview.rows[0]["status"], "artwork_update")
			search_calls = []

			def retag(value, _paths, on_output=None):
				updated = dict(value["managed_file"])
				updated["artwork_source_url"] = value["source_metadata"]["cover_url"]
				return updated

			result = execute_import(
				preview,
				paths,
				search=lambda metadata: search_calls.append(metadata) or [],
				retag_artwork=retag,
			)
			self.assertEqual(search_calls, [])
			self.assertEqual(result["playlist"]["items"][0]["status"], "managed_ready")
			self.assertEqual(
				result["manifest"]["recordings"][key]["managed_file"]["artwork_source_url"],
				"https://example.test/correct-album.jpg",
			)

	def test_manual_youtube_override_does_not_turn_retag_failure_into_success(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {
				"title": "Retag Failure",
				"artists": "Artist",
				"album": "Original Album",
				"duration_ms": 180000,
				"sp_id": "retag-failure-track",
				"cover_url": "https://example.test/correct-cover.jpg",
			})
			recording["youtube"] = {
				"url": "https://www.youtube.com/watch?v=chosen",
				"video_id": "chosen",
				"selected_by": "manual_candidate",
			}
			manifest["manual_youtube_overrides"][key] = dict(recording["youtube"])
			relative = managed_relative_path(recording)
			target = paths.root / relative
			target.parent.mkdir(parents=True)
			target.write_bytes(b"managed-playlist-mp3")
			recording["managed_file"] = {
				"relative_path": relative,
				"tool_owned": True,
				"metadata_profile": "playlist",
				"artwork_source_url": "https://example.test/old-cover.jpg",
			}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			playlist = {
				"id": "playlist", "name": "Retag Failure", "url": "https://open.spotify.com/playlist/playlist",
				"tracks": [{
					"position": 1, "title": "Retag Failure", "artists": "Artist", "album": "Original Album",
					"duration_ms": 180000, "sp_id": "retag-failure-track", "cover_url": "https://example.test/correct-cover.jpg",
				}],
				"total_count": 1,
				"complete": True,
			}
			with patch("crate_music_importer.ipod_import.pipeline.managed_duration_is_valid", return_value=True):
				preview = build_preview(playlist, [], manifest, paths)
			result = execute_import(
				preview,
				paths,
				retag_artwork=lambda *_args, **_kwargs: (_ for _ in ()).throw(MediaError("Managed file hash changed")),
			)
			self.assertEqual(result["playlist"]["items"][0]["status"], "failed")
			saved = result["manifest"]["recordings"][key]
			self.assertEqual(saved["last_error"], "Managed file hash changed")
			self.assertEqual(saved["last_error_source"], {"type": "playlist", "id": "playlist"})

	def test_apply_imports_only_new_managed_file_and_preserves_order(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			existing_key, existing = upsert_recording(manifest, {"title": "Existing", "artists": "Artist", "duration_ms": 100000, "sp_id": "one"})
			existing["music"] = {"source": "existing_library", "persistent_id": "PID-EXISTING", "database_id": "1", "location": "/Music/Existing.m4a"}
			existing["active_reference"] = {"kind": "existing_music", "persistent_id": "PID-EXISTING"}
			managed_key, managed = upsert_recording(manifest, {"title": "Managed", "artists": "Artist", "duration_ms": 120000, "sp_id": "two"})
			relative = managed_relative_path(managed)
			managed_path = paths.root / relative
			managed_path.parent.mkdir(parents=True)
			managed_path.write_bytes(b"mp3")
			managed["managed_file"] = {"relative_path": relative, "tool_owned": True}
			managed["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			set_playlist(manifest, {"id": "playlist", "name": "Order", "url": "url", "tracks": [], "complete": True, "total_count": 3}, [
				{"position": 1, "recording_id": existing_key, "spotify_id": "one", "status": "reused_music"},
				{"position": 2, "recording_id": managed_key, "spotify_id": "two", "status": "managed_ready"},
				{"position": 3, "recording_id": managed_key, "spotify_id": "two", "status": "managed_ready"},
			])
			music = [{"title": "Existing", "artist": "Artist", "album": "", "duration_s": 100, "persistent_id": "PID-EXISTING", "database_id": "1", "location": "/Music/Existing.m4a", "comment": ""}]
			with patch("crate_music_importer.ipod_import.pipeline.playlist_status", return_value=("AVAILABLE", None)), \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file", return_value={"persistent_id": "PID-MANAGED", "database_id": "2", "location": "/Music/Managed.mp3"}) as imported, \
				patch("crate_music_importer.ipod_import.pipeline.sync_music_playlist", return_value="PLAYLIST-PID") as synced:
				result = apply_to_music(manifest, "playlist", paths, music)
			self.assertEqual(result["new_imports"], 1)
			imported.assert_called_once_with(managed_path, managed_key)
			synced.assert_called_once_with("Order", None, ["PID-EXISTING", "PID-MANAGED", "PID-MANAGED"])

	def test_apply_refuses_unowned_name_collision_before_import(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {"title": "Managed", "artists": "Artist", "duration_ms": 120000, "sp_id": "two"})
			recording["managed_file"] = {"relative_path": "tracks/a.mp3", "tool_owned": True}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": "tracks/a.mp3"}
			set_playlist(manifest, {"id": "playlist", "name": "Collision", "url": "url", "tracks": [], "complete": True, "total_count": 1}, [
				{"position": 1, "recording_id": key, "spotify_id": "two", "status": "managed_ready"},
			])
			with patch("crate_music_importer.ipod_import.pipeline.playlist_status", return_value=("COLLISION", "OTHER-PID")), \
				patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as imported:
				with self.assertRaisesRegex(Exception, "not recorded as importer-owned"):
					apply_to_music(manifest, "playlist", paths, [])
			imported.assert_not_called()

	def test_deliberate_youtube_choice_can_resolve_music_ambiguity(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			playlist = {
				"id": "playlist",
				"name": "Ambiguous",
				"url": "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1",
				"tracks": [{"position": 1, "title": "Song", "artists": "Artist", "album": "Album", "duration_ms": 180000, "sp_id": "track"}],
				"total_count": 1,
				"complete": True,
			}
			music = [
				{"title": "Song", "artist": "Artist", "album": "Album", "duration_s": 180, "persistent_id": "PID1", "database_id": "1", "location": "/one", "comment": ""},
				{"title": "Song", "artist": "Artist", "album": "Album", "duration_s": 180, "persistent_id": "PID2", "database_id": "2", "location": "/two", "comment": ""},
			]
			first = build_preview(playlist, music, new_manifest(paths), paths)
			self.assertEqual(first.rows[0]["status"], "review_music")
			recording = next(iter(first.manifest["recordings"].values()))
			recording["youtube"] = {
				"video_id": "chosen",
				"url": "https://www.youtube.com/watch?v=chosen",
				"selected_by": "manual_url",
				"override_music_ambiguity": True,
			}
			second = build_preview(playlist, music, first.manifest, paths)
			self.assertEqual(second.rows[0]["status"], "ready_to_download")

	def test_promotion_clears_the_problem_it_deliberately_resolves(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest = new_manifest(paths)
			key, recording = upsert_recording(
				manifest,
				{"title": "Song", "artists": "Artist", "duration_ms": 180000, "sp_id": "track-id"},
			)
			recording["review"] = {"kind": "music_cache_stale", "message": "stale", "candidates": []}
			recording["last_error"] = "stale"
			music = [{
				"title": "Song",
				"artist": "Artist",
				"album": "Album",
				"duration_s": 180,
				"persistent_id": "PID",
				"database_id": "1",
				"location": None,
				"comment": "",
			}]

			promote_recording(manifest, key, "PID", music, paths)

			self.assertNotIn("review", recording)
			self.assertNotIn("last_error", recording)
			self.assertEqual(recording["active_reference"], {"kind": "existing_music", "persistent_id": "PID"})


if __name__ == "__main__":
	unittest.main()
