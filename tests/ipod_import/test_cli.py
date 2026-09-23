import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crate_music_importer.ipod_import import cli
from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest, new_manifest, save_manifest, set_album, set_playlist, upsert_recording
from crate_music_importer.ipod_import.music_cache import build_full_cache, save_music_cache
from crate_music_importer.ipod_import.spotify import load_fixture


FIXTURES = Path(__file__).parent / "fixtures"
ALBUM_URL = "https://open.spotify.com/album/48a7rOjTzpD1zzJAteeveE"


class AlbumCliEfficiencyTests(unittest.TestCase):
	def setUp(self):
		lock = patch("crate_music_importer.ipod_import.dependency_lock.dependency_lock", return_value=contextlib.nullcontext())
		lock.start()
		self.addCleanup(lock.stop)

	def test_saved_playlists_backfills_and_returns_playlist_thumbnail(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			set_playlist(manifest, {
				"id": "37i9dQZF1DXTESTFIXTURE1",
				"name": "Fixture Playlist",
				"url": "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1",
				"complete": True,
				"total_count": 0,
			}, [])
			save_manifest(paths, manifest)
			output = io.StringIO()
			with patch("crate_music_importer.ipod_import.cli.ManagedPaths", return_value=paths), \
				patch("crate_music_importer.ipod_import.cli.fetch_playlist_cover", return_value="https://example.test/playlist.jpg"), \
				contextlib.redirect_stdout(output):
				code = cli.run(["saved-playlists", "--json"])
			self.assertEqual(code, 0)
			self.assertEqual(json.loads(output.getvalue())["playlists"][0]["cover_url"], "https://example.test/playlist.jpg")
			self.assertEqual(load_manifest(paths)["playlists"]["37i9dQZF1DXTESTFIXTURE1"]["cover_url"], "https://example.test/playlist.jpg")

	def test_saved_playlists_updates_changed_cover_and_manual_refresh_keeps_same_url(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			playlist_id = "37i9dQZF1DXTESTFIXTURE1"
			set_playlist(manifest, {
				"id": playlist_id, "name": "Fixture Playlist",
				"url": f"https://open.spotify.com/playlist/{playlist_id}",
				"cover_url": "https://example.test/old.jpg", "complete": True, "total_count": 0,
			}, [])
			save_manifest(paths, manifest)
			with patch("crate_music_importer.ipod_import.cli.ManagedPaths", return_value=paths), \
				patch("crate_music_importer.ipod_import.cli.fetch_playlist_cover", return_value="https://example.test/new.jpg") as fetch_cover, \
				contextlib.redirect_stdout(io.StringIO()):
				self.assertEqual(cli.run(["saved-playlists", "--json"]), 0)
				self.assertEqual(cli.run(["playlist-artwork-refresh", playlist_id, "--json"]), 0)
			self.assertEqual(fetch_cover.call_count, 2)
			self.assertEqual(load_manifest(paths)["playlists"][playlist_id]["cover_url"], "https://example.test/new.jpg")

	def test_playlist_update_preview_saves_current_cover_without_music_changes(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			playlist_id = "37i9dQZF1DXTESTFIXTURE1"
			set_playlist(manifest, {
				"id": playlist_id, "name": "Fixture Playlist",
				"url": f"https://open.spotify.com/playlist/{playlist_id}",
				"cover_url": "https://example.test/old.jpg", "complete": True, "total_count": 0,
			}, [])
			save_manifest(paths, manifest)
			current = SimpleNamespace(to_dict=lambda: {
				"id": playlist_id, "url": f"https://open.spotify.com/playlist/{playlist_id}",
				"cover_url": "https://example.test/current.jpg", "tracks": [], "total_count": 0, "complete": True,
			})
			preview = SimpleNamespace(to_dict=lambda: {"source": {"cover_url": "https://example.test/current.jpg"}})
			with patch("crate_music_importer.ipod_import.cli.ManagedPaths", return_value=paths), \
				patch("crate_music_importer.ipod_import.cli.fetch_playlist", return_value=current), \
				patch("crate_music_importer.ipod_import.cli.build_playlist_update_preview", return_value=preview), \
				patch("crate_music_importer.ipod_import.cli._music_tracks", return_value=[]), \
				contextlib.redirect_stdout(io.StringIO()):
				self.assertEqual(cli.run(["playlist-update-preview", playlist_id, "--json"]), 0)
			self.assertEqual(load_manifest(paths)["playlists"][playlist_id]["cover_url"], "https://example.test/current.jpg")

	def test_album_preview_refreshes_music_and_saves_unique_binding(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			album = load_fixture(FIXTURES / "spotify_album.json").to_dict()
			key, recording = upsert_recording(manifest, album["tracks"][0], source_type="album")
			recording["music_binding"] = {"persistent_id": "STALE"}
			save_manifest(paths, manifest)
			save_music_cache(paths, build_full_cache(paths, []))
			live = {
				"persistent_id": "CURRENT", "title": album["tracks"][0]["title"],
				"artist": "Grimes", "album": "Visions", "duration_s": 116,
			}
			output = io.StringIO()
			with patch("crate_music_importer.ipod_import.cli.ManagedPaths", return_value=paths), \
				patch("crate_music_importer.ipod_import.cli.scan_music_library", return_value=[live]) as full_scan, \
				contextlib.redirect_stdout(output):
				code = cli.run([
					"album-preview",
					"--spotify-fixture",
					str(FIXTURES / "spotify_album.json"),
					"--json",
				])
			self.assertEqual(code, 0)
			full_scan.assert_called_once_with()
			self.assertIn("CURRENT", json.dumps(json.loads(paths.music_cache.read_text())))
			self.assertEqual(load_manifest(paths)["recordings"][key]["music_binding"], {"persistent_id": "CURRENT"})
			self.assertEqual(json.loads(output.getvalue())["counts"], {"in_library": 1, "not_in_library": 1})

	def test_album_preview_saves_new_binding_without_importing_or_changing_music(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			album = load_fixture(FIXTURES / "spotify_album.json").to_dict()
			live = {
				"persistent_id": "CURRENT", "title": album["tracks"][0]["title"],
				"artist": "Grimes", "album": "Visions", "duration_s": 116,
			}
			with patch("crate_music_importer.ipod_import.cli.ManagedPaths", return_value=paths), \
				patch("crate_music_importer.ipod_import.cli.scan_music_library", return_value=[live]), \
				patch("crate_music_importer.ipod_import.cli.build_album_preview") as import_planner, \
				contextlib.redirect_stdout(io.StringIO()):
				cli.run(["album-preview", "--spotify-fixture", str(FIXTURES / "spotify_album.json"), "--json"])
			import_planner.assert_not_called()
			recordings = load_manifest(paths)["recordings"]
			self.assertEqual(len(recordings), 1)
			self.assertEqual(next(iter(recordings.values()))["music_binding"], {"persistent_id": "CURRENT"})

	def test_album_download_planning_does_not_scan_music(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			save_music_cache(paths, build_full_cache(paths, []))
			album = load_fixture(FIXTURES / "spotify_album.json")
			preview = SimpleNamespace(album_id=album.id)
			result = {"album": {"items": [{"status": "managed_ready"}]}}
			with patch("crate_music_importer.ipod_import.cli.ManagedPaths", return_value=paths), \
				patch("crate_music_importer.ipod_import.cli.load_manifest", return_value=manifest), \
				patch("crate_music_importer.ipod_import.cli.check_tools", return_value={}), \
				patch("crate_music_importer.ipod_import.cli.fetch_album", return_value=album), \
				patch("crate_music_importer.ipod_import.cli.build_album_preview", return_value=preview) as build_preview, \
				patch("crate_music_importer.ipod_import.cli.execute_album_import", return_value=result), \
				patch("crate_music_importer.ipod_import.cli.scan_music_library_for_health") as full_scan, \
				contextlib.redirect_stdout(io.StringIO()):
				code = cli.run(["album-import", ALBUM_URL, "--confirm-download"])
			self.assertEqual(code, 0)
			self.assertEqual(build_preview.call_args.args[1], [])
			full_scan.assert_not_called()

	def test_album_apply_runs_one_targeted_final_check(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {
				"title": "Song",
				"artists": "Artist",
				"album": "Album",
				"album_id": "48a7rOjTzpD1zzJAteeveE",
				"duration_ms": 180000,
				"sp_id": "track-id",
			}, source_type="album")
			recording["music_binding"] = {"persistent_id": "SAVED-PID"}
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE",
				"name": "Album",
				"url": ALBUM_URL,
				"tracks": [],
				"complete": True,
				"total_count": 1,
			}, [{"position": 1, "recording_id": key, "status": "managed_ready"}])
			cached_track = {
				"persistent_id": "SAVED-PID", "database_id": "1", "title": "Song", "artist": "Artist",
				"album": "Album", "album_artist": "Artist", "duration_s": 180, "location": "/Music/Song.m4a",
				"comment": "", "track_no": 1, "track_total": 1, "disc_no": 1, "disc_total": 1, "compilation": False,
			}
			save_music_cache(paths, build_full_cache(paths, [cached_track]))
			apply_result = {"track_count": 1, "new_imports": 0, "updated_tracks": 0, "reused_tracks": 1}
			with patch("crate_music_importer.ipod_import.cli.ManagedPaths", return_value=paths), \
				patch("crate_music_importer.ipod_import.cli.load_manifest", return_value=manifest), \
				patch("crate_music_importer.ipod_import.cli.scan_music_library_for_health") as full_scan, \
				patch("crate_music_importer.ipod_import.cli.apply_album_to_music", return_value=apply_result) as apply_album, \
				contextlib.redirect_stdout(io.StringIO()):
				code = cli.run(["album-apply", ALBUM_URL, "--confirm-music-write"])
			self.assertEqual(code, 0)
			full_scan.assert_not_called()
			self.assertEqual(apply_album.call_args.args[3][0]["persistent_id"], "SAVED-PID")
			self.assertIs(apply_album.call_args.kwargs["exact_lookup"], cli.lookup_music_track)

	def test_playlist_preview_and_download_planning_never_scan_music(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			save_music_cache(paths, build_full_cache(paths, []))
			playlist = load_fixture(FIXTURES / "spotify_playlist.json")
			preview = SimpleNamespace(playlist_id=playlist.id, manifest={"playlists": {playlist.id: {"name": "Fixture", "spotify_url": "", "warning": None}}}, rows=[], counts={})
			result = {"playlist": {"items": []}, "m3u8": "/tmp/list.m3u8"}
			with patch("crate_music_importer.ipod_import.cli.ManagedPaths", return_value=paths), \
				patch("crate_music_importer.ipod_import.cli.load_manifest", return_value=manifest), \
				patch("crate_music_importer.ipod_import.cli.build_preview", return_value=preview), \
				patch("crate_music_importer.ipod_import.cli.execute_import", return_value=result), \
				patch("crate_music_importer.ipod_import.cli.fetch_playlist", return_value=playlist), \
				patch("crate_music_importer.ipod_import.cli.check_tools", return_value={}), \
				patch("crate_music_importer.ipod_import.cli.scan_music_library_for_health") as full_scan, \
				contextlib.redirect_stdout(io.StringIO()):
				self.assertEqual(cli.run(["preview", "--spotify-fixture", str(FIXTURES / "spotify_playlist.json"), "--json"]), 0)
				self.assertEqual(cli.run(["import", "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1", "--confirm-download"]), 0)
			full_scan.assert_not_called()


if __name__ == "__main__":
	unittest.main()
