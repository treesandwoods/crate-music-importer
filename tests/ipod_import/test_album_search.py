import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import import cli
from crate_music_importer.ipod_import.album_search import check_album, run_album_search_session
from crate_music_importer.ipod_import.manifest import ManagedPaths
from crate_music_importer.ipod_import.music_cache import MusicCacheUnavailableError, refresh_music_cache
from crate_music_importer.ipod_import.pipeline import AlbumLibraryIndex


def source_album():
	return {"id": "album", "name": "Album", "totalTracks": 2, "tracks": [
		{"title": title, "artists": "Artist", "duration_ms": 180000, "track_no": position, "disc_no": 1}
		for position, title in enumerate(("First Song", "Second Song"), start=1)
	]}


def music_tracks():
	return [
		{"persistent_id": str(position), "title": track["title"], "artist": "Artist", "album": "Album", "duration_s": 180, "track_no": position, "disc_no": 1}
		for position, track in enumerate(source_album()["tracks"], start=1)
	]


class AlbumSearchTests(unittest.TestCase):
	def test_complete_partial_absent_and_wrong_album(self):
		tracks = music_tracks()
		album = source_album()
		for candidates, status, count in (
			(tracks, "complete", 2), (tracks[:1], "partial", 1), ([], "none", 0),
			([{**track, "album": "Compilation"} for track in tracks], "none", 0),
			([{**track, "artist": "Another Artist"} for track in tracks], "none", 0),
			(tracks + [{**tracks[0], "persistent_id": "duplicate"}], "partial", 1),
		):
			with self.subTest(status=status, candidates=candidates):
				before = copy.deepcopy(candidates)
				self.assertEqual(check_album(AlbumLibraryIndex(candidates), album), {"status": status, "matched": count, "total": 2})
				self.assertEqual(candidates, before)

	def test_album_name_is_only_a_candidate_hint_and_deluxe_requires_bonus_tracks(self):
		index = AlbumLibraryIndex(music_tracks())
		album = source_album()
		self.assertEqual(check_album(index, {key: value for key, value in album.items() if key != "tracks"}), {"status": "needs_tracks"})
		album["name"] = "Album (Deluxe Edition)"
		album["totalTracks"] = 3
		album["tracks"].append({"title": "Bonus Song", "artists": "Artist", "duration_ms": 180000, "track_no": 3})
		self.assertEqual(check_album(index, album), {"status": "partial", "matched": 2, "total": 3})

	def test_incomplete_metadata_and_empty_albums_never_count_as_complete(self):
		album = source_album()
		index = AlbumLibraryIndex(music_tracks())
		for invalid in ({**album, "tracks": album["tracks"][:1]}, {**album, "totalTracks": 0}, {**album, "tracks": [None, None]}):
			with self.subTest(invalid=invalid), self.assertRaises(ValueError):
				check_album(index, invalid)

	def test_session_loads_cache_once_preserves_state_and_returns_responses_in_order(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			refresh_music_cache(paths, music_tracks())
			before = paths.music_cache.read_bytes()
			requests = [source_album(), {**source_album(), "name": "Missing Album"}, source_album()]
			output = io.StringIO()
			from crate_music_importer.ipod_import.album_search import read_music_cache
			with patch("crate_music_importer.ipod_import.album_search.read_music_cache", wraps=read_music_cache) as read, \
				patch("crate_music_importer.ipod_import.music.scan_music_library", side_effect=AssertionError("No live scan")):
				run_album_search_session(paths, io.StringIO("\n".join(json.dumps(item) for item in requests)), output)
			self.assertEqual(read.call_count, 1)
			responses = [json.loads(line) for line in output.getvalue().splitlines()]
			self.assertTrue(responses[0]["ready"])
			self.assertEqual([item["status"] for item in responses[1:]], ["complete", "none", "complete"])
			self.assertEqual(paths.music_cache.read_bytes(), before)
			self.assertFalse(paths.manifest.exists())

	def test_missing_corrupt_and_expired_cache_do_not_scan_or_write(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			with patch("crate_music_importer.ipod_import.music.scan_music_library", side_effect=AssertionError("No live scan")):
				with self.assertRaisesRegex(MusicCacheUnavailableError, "Refresh Library Health"):
					run_album_search_session(paths, io.StringIO(), io.StringIO())
				self.assertFalse(paths.state_dir.exists())
				cache = refresh_music_cache(paths, music_tracks())
				cache["scanned_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
				for contents in ("invalid JSON", json.dumps(cache)):
					paths.music_cache.write_text(contents)
					with self.assertRaisesRegex(MusicCacheUnavailableError, "Refresh Library Health"):
						run_album_search_session(paths, io.StringIO(), io.StringIO())
					self.assertEqual(paths.music_cache.read_text(), contents)

	def test_cli_checker_does_not_load_manifest_or_take_downloader_lock(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			refresh_music_cache(paths, music_tracks())
			with patch("crate_music_importer.ipod_import.cli.ManagedPaths", return_value=paths), \
				patch("crate_music_importer.ipod_import.cli.load_manifest", side_effect=AssertionError("No manifest load")), \
				patch("crate_music_importer.ipod_import.dependency_lock.dependency_lock", side_effect=AssertionError("No downloader lock")), \
				patch("sys.stdin", io.StringIO(json.dumps(source_album()) + "\n")), \
				patch("sys.stdout", io.StringIO()) as output:
				self.assertEqual(cli.run(["album-search-check"]), 0)
			self.assertEqual(json.loads(output.getvalue().splitlines()[1])["status"], "complete")
