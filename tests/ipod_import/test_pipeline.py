import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from crate_music_importer.ipod_import.manifest import ManagedPaths, new_manifest, set_album, set_playlist, upsert_recording
from crate_music_importer.ipod_import.pipeline import (
	apply_album_to_music,
	apply_to_music,
	build_album_metadata_preview,
	build_album_library_preview,
	build_album_preview,
	build_preview,
	execute_album_import,
	execute_import,
)


def source_track(**updates):
	track = {"position": 1, "title": "Song", "artists": "Artist", "album": "Album", "album_artist": "Artist", "album_id": "album-id", "duration_ms": 180000, "sp_id": "track-id", "track_no": 1, "disc_no": 1}
	track.update(updates)
	return track


def playlist(tracks=None):
	return {"id": "playlist-id", "name": "Playlist", "url": "https://open.spotify.com/playlist/playlist-id", "tracks": tracks or [source_track()], "total_count": len(tracks or [1]), "complete": True}


def album(tracks=None):
	return {"id": "album-id", "name": "Album", "album_artist": "Artist", "url": "https://open.spotify.com/album/album-id", "tracks": tracks or [source_track()], "total_count": len(tracks or [1]), "complete": True}


def music_track(persistent_id="PID", *, album_name="Album", location="/Music/Song.m4a", comment=""):
	return {"persistent_id": persistent_id, "database_id": "1", "title": "Song", "artist": "Artist", "album": album_name, "duration_s": 180, "location": location, "comment": comment}


class PipelineTests(unittest.TestCase):
	def test_mislabelled_album_artist_cohort_is_in_library_without_retagging(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			titles = ["The Girl From Ipanema", "Doralice", "Desafinado", "Corcovado"]
			lengths = [324, 166, 255, 256]
			tracks = [source_track(position=n, track_no=n, title=title, artists="Stan Getz, João Gilberto", album="Getz/Gilberto", duration_ms=lengths[n - 1] * 1000) for n, title in enumerate(titles, 1)]
			candidates = [music_track(f"GETZ{n}", album_name="Stan Getz") | {"title": title if n != 4 else "Corcovado (Quiet Nights Of Quiet Stars)", "artist": "Getz/Gilberto", "album_artist": "Getz/Gilberto", "track_no": n, "disc_no": 0, "duration_s": lengths[n - 1]} for n, title in enumerate(titles, 1)]
			source_album = album(tracks) | {"name": "Getz/Gilberto"}
			library = build_album_library_preview(source_album, candidates, new_manifest(paths))
			self.assertEqual(library.counts, {"in_library": 4, "not_in_library": 0})
			preview = build_album_preview(source_album, candidates, new_manifest(paths), paths)
			self.assertEqual(preview.counts, {"reused_music": 4})
			self.assertFalse(paths.root.exists())
			incomplete = build_album_library_preview(source_album, candidates[:-1], new_manifest(paths))
			self.assertEqual(incomplete.counts, {"in_library": 0, "not_in_library": 4})
			ambiguous = build_album_library_preview(source_album, candidates + [candidate | {"persistent_id": "OTHER"} for candidate in candidates], new_manifest(paths))
			self.assertEqual(ambiguous.counts, {"in_library": 0, "not_in_library": 4})

	def test_named_album_examples_resolve_from_music_without_file_provenance(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			for title, artist, album_name in (
				("Cupid - Live at the Harlem Square Club, Miami, FL - January 1963", "Sam Cooke", "One Night Stand - Sam Cooke Live At The Harlem Square Club, 1963"),
				("The Shape I'm In - Concert Version", "The Band", "The Last Waltz"),
			):
				track = source_track(title=title, artists=artist, album=album_name, album_artist=artist)
				candidate = music_track("CURRENT", album_name=album_name, location="/Users/example/Music/User.m4a")
				candidate.update(title=title, artist=artist, track_no=1, disc_no=1, duration_s=150)
				preview = build_album_library_preview(album([track]) | {"name": album_name}, [candidate], new_manifest(paths))
				self.assertEqual(preview.counts, {"in_library": 1, "not_in_library": 0})
				self.assertEqual(preview.rows[0]["status"], "in_library")
				self.assertEqual(preview.manifest["recordings"][preview.rows[0]["recording_id"]]["music_binding"], {"persistent_id": "CURRENT"})
				wrong_position = build_album_library_preview(
					album([track]) | {"name": album_name}, [candidate | {"track_no": 2}], new_manifest(paths),
				)
				self.assertEqual(wrong_position.rows[0]["status"], "not_in_library")
				ambiguous = build_album_library_preview(
					album([track]) | {"name": album_name},
					[candidate, candidate | {"persistent_id": "DUPLICATE"}], new_manifest(paths),
				)
				self.assertEqual(ambiguous.rows[0]["status"], "not_in_library")

	def test_album_library_status_requires_unique_current_requested_album_match(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track(), source_type="album")
			recording["music_binding"] = {"persistent_id": "STALE"}
			recording["managed_file"] = {"managed_by_crate": True, "relative_path": "Music/Song.mp3", "sha256": "hash"}
			for candidates, expected in (
				([], "not_in_library"),
				([music_track("OTHER", album_name="Other Album")], "not_in_library"),
				([music_track("CURRENT")], "in_library"),
				([music_track("ONE"), music_track("TWO")], "not_in_library"),
			):
				preview = build_album_library_preview(album(), candidates, manifest)
				self.assertEqual(preview.rows[0]["status"], expected)
				self.assertEqual(preview.manifest["recordings"][key]["managed_file"], recording["managed_file"])
				self.assertFalse(paths.root.exists())
				if expected == "in_library":
					self.assertEqual(preview.manifest["recordings"][key]["music_binding"], {"persistent_id": "CURRENT"})
				else:
					self.assertEqual(preview.manifest["recordings"][key]["music_binding"], {"persistent_id": "STALE"})

	def test_metadata_preview_does_not_touch_music_or_media(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			preview = build_album_metadata_preview(album(), new_manifest(paths))
			self.assertEqual(preview.counts, {"not_started": 1})
			self.assertFalse(paths.root.exists())

	def test_existing_tracks_have_one_preview_state_regardless_of_origin(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			for comment in ("", "recording_id=rec_old_hint"):
				manifest = new_manifest(paths)
				preview = build_preview(playlist(), [music_track(comment=comment)], manifest, paths)
				self.assertEqual(preview.rows[0]["status"], "reused_music")
				self.assertNotIn("match_kind", preview.rows[0])
				self.assertEqual(preview.manifest["recordings"][preview.rows[0]["recording_id"]]["music_binding"], {"persistent_id": "PID"})

	def test_external_track_satisfies_future_playlist_without_download(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			first = build_preview(playlist(), [music_track()], new_manifest(paths), paths)
			second = build_preview(playlist(), [music_track()], first.manifest, paths)
			download = Mock()
			execute_import(second, paths, download=download)
			download.assert_not_called()

	def test_crate_managed_track_reuses_identically(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track())
			recording["managed_file"] = {"managed_by_crate": True, "relative_path": "Music/Song.mp3", "sha256": "hash"}
			recording["music_binding"] = {"persistent_id": "PID"}
			preview = build_preview(playlist(), [music_track()], manifest, paths)
			self.assertEqual(preview.rows[0]["status"], "reused_music")
			self.assertEqual(preview.rows[0]["recording_id"], key)

	def test_stale_binding_is_repaired_during_preview(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track())
			recording["music_binding"] = {"persistent_id": "STALE"}
			preview = build_preview(playlist(), [music_track("CURRENT")], manifest, paths)
			self.assertEqual(preview.rows[0]["status"], "reused_music")
			self.assertEqual(preview.manifest["recordings"][key]["music_binding"], {"persistent_id": "CURRENT"})

	def test_ambiguous_matches_require_review_without_download(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			preview = build_preview(playlist(), [music_track("ONE"), music_track("TWO")], new_manifest(paths), paths)
			self.assertEqual(preview.rows[0]["status"], "review_music")
			download = Mock()
			execute_import(preview, paths, download=download)
			download.assert_not_called()

	def test_absent_recording_downloads_once_even_with_duplicate_occurrences(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			tracks = [source_track(position=1), source_track(position=2)]
			preview = build_preview(playlist(tracks), [], new_manifest(paths), paths)
			recording = next(iter(preview.manifest["recordings"].values()))
			recording["youtube"] = {"url": "https://youtube.test/song", "video_id": "song", "selected_by": "manual_candidate"}
			def download(recording, target_paths, **_kwargs):
				target = target_paths.root / "Music/Song.mp3"
				target.parent.mkdir(parents=True, exist_ok=True)
				target.write_bytes(b"mp3")
				return {"managed_by_crate": True, "relative_path": "Music/Song.mp3", "sha256": "hash", "duration_ms": 180000, "metadata_profile": "playlist"}
			downloader = Mock(side_effect=download)
			result = execute_import(preview, paths, download=downloader)
			self.assertEqual(downloader.call_count, 1)
			self.assertEqual([item["status"] for item in result["playlist"]["items"]], ["managed_ready", "managed_ready"])

	def test_requested_album_copy_rebinds_instead_of_conflicting(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track(), source_type="album")
			recording["music_binding"] = {"persistent_id": "LOOSE"}
			preview = build_album_preview(album(), [music_track("LOOSE", album_name="Playlist Imports"), music_track("ALBUM")], manifest, paths)
			self.assertEqual(preview.rows[0]["status"], "reused_music")
			self.assertEqual(preview.manifest["recordings"][key]["music_binding"], {"persistent_id": "ALBUM"})

	def test_different_album_external_file_is_not_retagged(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			preview = build_album_preview(album(), [music_track("OTHER", album_name="Other Album", location="/Users/example/Music/User.m4a")], new_manifest(paths), paths)
			self.assertEqual(preview.rows[0]["status"], "review_music")
			self.assertEqual(next(iter(preview.manifest["recordings"].values()))["review"]["kind"], "album_identity_mismatch")

	def test_crate_managed_loose_file_can_be_retagged_for_album(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			paths.create()
			managed_path = paths.root / "Music/Song.mp3"
			managed_path.parent.mkdir(parents=True, exist_ok=True)
			managed_path.write_bytes(b"mp3")
			manifest = new_manifest(paths)
			_key, recording = upsert_recording(manifest, source_track(), source_type="album")
			recording["managed_file"] = {"managed_by_crate": True, "relative_path": "Music/Song.mp3", "sha256": "hash", "duration_ms": 180000, "metadata_profile": "playlist"}
			recording["music_binding"] = {"persistent_id": "LOOSE"}
			with patch("crate_music_importer.ipod_import.pipeline.managed_duration_is_valid", return_value=True):
				preview = build_album_preview(album(), [music_track("LOOSE", album_name="Playlist Imports", location=str(managed_path))], manifest, paths)
			self.assertEqual(preview.rows[0]["status"], "upgrade_managed")
			def retag(current, _paths, **_kwargs):
				managed = dict(current["managed_file"])
				managed.update(metadata_profile="album", spotify_album_id="album-id")
				return managed
			execute_album_import(preview, paths, retag=retag)

	def test_apply_reconciles_stale_id_before_music_write(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track())
			recording["music_binding"] = {"persistent_id": "STALE"}
			set_playlist(manifest, playlist(), [{"position": 1, "recording_id": key, "spotify_id": "track-id", "status": "reused_music"}])
			with patch("crate_music_importer.ipod_import.pipeline.playlist_status", return_value=("FOUND", "PLAYLIST")), patch("crate_music_importer.ipod_import.pipeline.sync_music_playlist", return_value="PLAYLIST") as sync:
				result = apply_to_music(manifest, "playlist-id", paths, [music_track("CURRENT")], exact_lookup=lambda pid: music_track("CURRENT") if pid == "CURRENT" else None)
			self.assertEqual(result["new_imports"], 0)
			self.assertEqual(manifest["recordings"][key]["music_binding"], {"persistent_id": "CURRENT"})
			sync.assert_called_once_with("Playlist", None, ["CURRENT"])

	def test_apply_imports_missing_managed_file_once(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			paths.create()
			file_path = paths.root / "Music/Song.mp3"
			file_path.parent.mkdir(parents=True, exist_ok=True)
			file_path.write_bytes(b"mp3")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track())
			recording["managed_file"] = {"managed_by_crate": True, "relative_path": "Music/Song.mp3", "sha256": "hash"}
			set_playlist(manifest, playlist(), [{"position": 1, "recording_id": key, "spotify_id": "track-id", "status": "managed_ready"}])
			with patch("crate_music_importer.ipod_import.pipeline.playlist_status", return_value=("AVAILABLE", None)), patch("crate_music_importer.ipod_import.pipeline.import_managed_file", return_value=music_track("NEW")) as importer, patch("crate_music_importer.ipod_import.pipeline.sync_music_playlist", return_value="PLAYLIST"):
				result = apply_to_music(manifest, "playlist-id", paths, [], exact_lookup=lambda _pid: None)
			self.assertEqual(result["new_imports"], 1)
			self.assertEqual(importer.call_count, 1)
			self.assertEqual(manifest["recordings"][key]["music_binding"], {"persistent_id": "NEW"})

	def test_album_apply_never_retags_unregistered_file(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source_track(), source_type="album")
			recording["music_binding"] = {"persistent_id": "OTHER"}
			set_album(manifest, album(), [{"position": 1, "track_no": 1, "disc_no": 1, "recording_id": key, "spotify_id": "track-id", "status": "review_music"}])
			with patch("crate_music_importer.ipod_import.pipeline.update_managed_music_track") as updater:
				with self.assertRaisesRegex(Exception, "not ready"):
					apply_album_to_music(manifest, "album-id", paths, [music_track("OTHER", album_name="Other Album", location="/Users/example/Music/User.m4a")], exact_lookup=lambda _pid: music_track("OTHER", album_name="Other Album", location="/Users/example/Music/User.m4a"))
			updater.assert_not_called()


if __name__ == "__main__":
	unittest.main()
