import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from crate_music_importer.ipod_import.health import _build_health_report
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
	return {"persistent_id": persistent_id, "database_id": "1", "title": "Song", "artist": "Artist", "album": album_name, "duration_s": 180, "location": location, "comment": comment, "track_no": 1, "track_total": 0, "disc_no": 1, "disc_total": 1}


class PipelineTests(unittest.TestCase):
	def test_partial_album_aligns_unknown_disc_and_shifted_numbers_without_replacing_existing_tracks(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			sources = [source_track(title=title, position=n, track_no=n, track_total=3, disc_total=1, sp_id=f"track-{n}") for n, title in enumerate(["First", "Missing", "Last"], 1)]
			existing = [music_track(pid) | {"title": title, "track_no": n, "track_total": 0, "disc_no": 0, "disc_total": 0} for pid, title, n in [("FIRST", "First", 1), ("LAST", "Last", 2)]]
			preview = build_album_preview(album(sources), existing, new_manifest(paths), paths)
			missing = preview.manifest["recordings"][preview.rows[1]["recording_id"]]
			path = paths.root / "Music/Missing.mp3"
			path.parent.mkdir(parents=True)
			path.write_bytes(b"mp3")
			missing["managed_file"] = {"managed_by_crate": True, "relative_path": "Music/Missing.mp3", "metadata_profile": "album", "duration_ms": 180000}
			live = {t["persistent_id"]: t.copy() for t in existing}
			def update(before, metadata):
				live[before["persistent_id"]] = before | {k: metadata[k] for k in ("track_no", "track_total", "disc_no", "disc_total")}
			def add(_path):
				live["NEW"] = music_track("NEW", location=str(path)) | {"title": "Missing", "track_no": 2, "track_total": 3}
				return live["NEW"]
			final = Mock(side_effect=lambda ids: {pid: live[pid] for pid in ids})
			with patch("crate_music_importer.ipod_import.pipeline.update_music_album_position", side_effect=update) as updater, patch("crate_music_importer.ipod_import.pipeline.import_managed_file", side_effect=add) as importer, patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=path), patch("crate_music_importer.ipod_import.pipeline.update_managed_music_track") as retagger:
				result = apply_album_to_music(preview.manifest, "album-id", paths, existing, exact_lookup=live.get, verify_music=lambda ids: {pid: live[pid] for pid in ids}, verify_music_final=final)
			self.assertEqual((result["new_imports"], result["updated_tracks"]), (1, 2))
			self.assertEqual(updater.call_count, 2)
			importer.assert_called_once_with(path)
			retagger.assert_not_called()
			self.assertEqual([(live[pid]["track_no"], live[pid]["disc_no"]) for pid in ["FIRST", "NEW", "LAST"]], [(1, 1), (2, 1), (3, 1)])
			final.assert_called_once_with(["NEW", "FIRST", "LAST"])

	def test_album_numbering_update_must_survive_final_verification(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			before = music_track() | {"disc_no": 0, "disc_total": 0}
			preview = build_album_preview(album(), [before], new_manifest(paths), paths)
			after = before | {"disc_no": 1, "disc_total": 1}
			with patch("crate_music_importer.ipod_import.pipeline.update_music_album_position"), patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as importer:
				with self.assertRaisesRegex(Exception, "disc_no"):
					apply_album_to_music(preview.manifest, "album-id", paths, [before], exact_lookup=Mock(side_effect=[before, after]), verify_music_final=lambda ids: {"PID": before})
			importer.assert_not_called()

	def test_album_addition_with_wrong_disc_is_left_pending(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest, key, recording, path = self.ready_album(paths)
			actual = music_track("NEW", location=str(path)) | {"duration_s": 175.04, "disc_no": 0}
			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=path), patch("crate_music_importer.ipod_import.pipeline.import_managed_file", return_value={"persistent_id": "NEW"}):
				with self.assertRaisesRegex(Exception, "disc_no"):
					apply_album_to_music(manifest, "album-id", paths, [], verify_music=lambda ids: {"NEW": actual})
			self.assertIn("music_import_pending", recording)

	def test_numbering_preflight_failure_does_not_modify_earlier_album_members(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			sources = [source_track(), source_track(title="Second", position=2, track_no=2, sp_id="second")]
			before = music_track() | {"disc_no": 0, "disc_total": 0}
			preview = build_album_preview(album(sources), [before], new_manifest(paths), paths)
			with patch("crate_music_importer.ipod_import.pipeline.update_music_album_position") as updater:
				with self.assertRaisesRegex(Exception, "not ready"):
					apply_album_to_music(preview.manifest, "album-id", paths, [before], exact_lookup=lambda pid: before)
			updater.assert_not_called()

	def test_multidisc_album_keeps_requested_disc_and_per_disc_track_numbers(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			sources = [source_track(title=title, position=n, track_no=1, track_total=1, disc_no=n, disc_total=2, sp_id=f"track-{n}") for n, title in enumerate(["Disc One", "Disc Two"], 1)]
			live = {str(n): music_track(str(n)) | {"title": title, "track_no": n, "disc_no": 0, "disc_total": 0} for n, title in enumerate(["Disc One", "Disc Two"], 1)}
			before = list(live.values())
			preview = build_album_preview(album(sources), before, new_manifest(paths), paths)
			def update(current, metadata):
				live[current["persistent_id"]] = current | {k: metadata[k] for k in ("track_no", "track_total", "disc_no", "disc_total")}
			with patch("crate_music_importer.ipod_import.pipeline.update_music_album_position", side_effect=update):
				apply_album_to_music(preview.manifest, "album-id", paths, before, exact_lookup=live.get)
			self.assertEqual([(live[str(n)]["disc_no"], live[str(n)]["track_no"], live[str(n)]["disc_total"]) for n in [1, 2]], [(1, 1, 2), (2, 1, 2)])

	def test_disappearing_existing_album_member_is_never_reimported(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			before = music_track() | {"disc_no": 0, "disc_total": 0}
			preview = build_album_preview(album(), [before], new_manifest(paths), paths)
			after = before | {"disc_no": 1, "disc_total": 1}
			with patch("crate_music_importer.ipod_import.pipeline.update_music_album_position"), patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as importer:
				with self.assertRaisesRegex(Exception, "refusing to replace"):
					apply_album_to_music(preview.manifest, "album-id", paths, [before], exact_lookup=Mock(side_effect=[before, after]), verify_music_final=lambda ids: {})
			importer.assert_not_called()

	def test_repeated_recording_positions_require_review_before_any_numbering_change(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			sources = [source_track(), source_track(position=2, track_no=2)]
			before = music_track() | {"disc_no": 0, "disc_total": 0}
			preview = build_album_preview(album(sources), [before], new_manifest(paths), paths)
			with patch("crate_music_importer.ipod_import.pipeline.update_music_album_position") as updater:
				with self.assertRaisesRegex(Exception, "Multiple album positions"):
					apply_album_to_music(preview.manifest, "album-id", paths, [before], exact_lookup=lambda pid: before)
			updater.assert_not_called()

	def ready_album(self, paths, *, duration_ms=175000):
		paths.create()
		path = paths.root / "Music/Song.mp3"
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(b"mp3")
		manifest = new_manifest(paths)
		key, recording = upsert_recording(manifest, source_track(), source_type="album")
		recording["managed_file"] = {"managed_by_crate": True, "relative_path": "Music/Song.mp3", "duration_ms": duration_ms, "metadata_profile": "album", "spotify_album_id": "album-id"}
		set_album(manifest, album(), [{"position": 1, "recording_id": key, "status": "managed_ready"}])
		return manifest, key, recording, path

	def test_album_addition_validates_downloaded_duration_and_caches_full_music_metadata(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest, key, recording, path = self.ready_album(paths)
			actual = music_track("NEW", location=str(path)) | {"duration_s": 175.04}
			cache = Mock()
			verify = Mock(return_value={"NEW": actual})
			final = Mock(return_value={"NEW": actual})
			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=path), patch("crate_music_importer.ipod_import.pipeline.import_managed_file", return_value={"persistent_id": "NEW"}):
				result = apply_album_to_music(manifest, "album-id", paths, [], verify_music=verify, verify_music_final=final, cache_updater=cache)
			self.assertEqual(result["new_imports"], 1)
			self.assertNotIn("music_import_pending", recording)
			self.assertEqual(recording["music_binding"], {"persistent_id": "NEW"})
			self.assertTrue(all(call.args == (actual,) for call in cache.call_args_list))
			final.assert_called_once_with(["NEW"])

	def test_album_wrong_audio_stays_pending_and_does_not_enter_cache(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest, key, recording, path = self.ready_album(paths)
			cache = Mock()
			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=path), patch("crate_music_importer.ipod_import.pipeline.import_managed_file", return_value={"persistent_id": "NEW"}):
				with self.assertRaisesRegex(Exception, "duration changed"):
					apply_album_to_music(manifest, "album-id", paths, [], verify_music=lambda _ids: {"NEW": music_track("NEW") | {"duration_s": 100}}, cache_updater=cache)
			self.assertIn("music_import_pending", recording)
			self.assertIn("duration", recording["last_error"])
			cache.assert_not_called()

	def test_music_read_failure_keeps_addition_pending_instead_of_reimporting(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest, key, recording, path = self.ready_album(paths)
			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=path), patch("crate_music_importer.ipod_import.pipeline.import_managed_file", return_value={"persistent_id": "NEW"}) as importer:
				with self.assertRaisesRegex(Exception, "Music read failed"):
					apply_album_to_music(manifest, "album-id", paths, [], verify_music=Mock(side_effect=RuntimeError("Music read failed")))
			importer.assert_called_once()
			self.assertIn("music_import_pending", recording)

	def test_album_upgrades_original_loose_music_id_without_importing_or_changing_playlist(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest, key, recording, path = self.ready_album(paths, duration_ms=180000)
			recording["music_binding"] = {"persistent_id": "ORIGINAL"}
			recording["playlist_memberships"] = {"saved": [7]}
			loose = music_track("ORIGINAL", album_name="Playlist Imports", location=str(path))
			lookup = Mock(side_effect=[loose, loose | {"album": "Album"}])
			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=path), patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as importer, patch("crate_music_importer.ipod_import.pipeline.update_managed_music_track") as update:
				result = apply_album_to_music(manifest, "album-id", paths, [loose], exact_lookup=lookup)
			self.assertEqual((result["new_imports"], result["updated_tracks"]), (0, 1))
			self.assertEqual(recording["music_binding"], {"persistent_id": "ORIGINAL"})
			self.assertEqual(recording["playlist_memberships"], {"saved": [7]})
			importer.assert_not_called()
			update.assert_called_once()

	def test_album_missing_music_location_refuses_duplicate_addition(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest, key, recording, path = self.ready_album(paths, duration_ms=180000)
			recording["music_binding"] = {"persistent_id": "ORIGINAL"}
			loose = music_track("ORIGINAL", album_name="Playlist Imports", location=None)
			with patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as importer:
				with self.assertRaisesRegex(Exception, "refusing to add a duplicate"):
					apply_album_to_music(manifest, "album-id", paths, [loose], exact_lookup=lambda _pid: loose)
			importer.assert_not_called()

	def test_album_retag_refresh_before_cache_update_keeps_original_music_id(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest, key, recording, path = self.ready_album(paths, duration_ms=180000)
			recording["music_binding"] = {"persistent_id": "ORIGINAL"}
			cached = music_track("ORIGINAL", album_name="Playlist Imports", location=str(path))
			actual = cached | {"album": "Album"}
			with patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as importer:
				result = apply_album_to_music(manifest, "album-id", paths, [cached], exact_lookup=lambda _pid: actual)
			self.assertEqual(result["new_imports"], 0)
			self.assertEqual(recording["music_binding"], {"persistent_id": "ORIGINAL"})
			importer.assert_not_called()

	def test_managed_album_refreshes_stale_compilation_flag_in_place(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest, key, recording, path = self.ready_album(paths, duration_ms=180000)
			recording["music_binding"] = {"persistent_id": "ORIGINAL"}
			actual = music_track("ORIGINAL", location=str(path)) | {"compilation": True}
			lookup = Mock(side_effect=[actual, actual | {"compilation": False}])
			with patch("crate_music_importer.ipod_import.pipeline.extract_embedded_artwork", return_value=path), patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as importer, patch("crate_music_importer.ipod_import.pipeline.update_managed_music_track") as update:
				result = apply_album_to_music(manifest, "album-id", paths, [actual], exact_lookup=lookup)
			self.assertEqual((result["new_imports"], result["updated_tracks"]), (0, 1))
			importer.assert_not_called()
			update.assert_called_once()

	def test_failed_verification_retry_adopts_already_added_track_without_download_or_add(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest, key, recording, path = self.ready_album(paths)
			recording["music_binding"] = {"persistent_id": "NEW"}
			recording["music_import_pending"] = {"relative_path": "Music/Song.mp3"}
			recording["last_error"] = "Previous Music check timed out."
			actual = music_track("NEW", location=str(path)) | {"duration_s": 175.04}
			with patch("crate_music_importer.ipod_import.pipeline.import_managed_file") as importer:
				result = apply_album_to_music(manifest, "album-id", paths, [], exact_lookup=lambda _pid: actual)
			self.assertEqual(result["new_imports"], 0)
			self.assertEqual(recording["music_binding"], {"persistent_id": "NEW"})
			self.assertNotIn("music_import_pending", recording)
			self.assertNotIn("last_error", recording)
			importer.assert_not_called()

	def test_album_library_preview_identifies_a_managed_loose_track_upgrade(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest, key, recording, path = self.ready_album(paths, duration_ms=180000)
			recording["managed_file"]["metadata_profile"] = "playlist"
			recording["music_binding"] = {"persistent_id": "ORIGINAL"}
			with patch("crate_music_importer.ipod_import.pipeline.managed_duration_is_valid", return_value=True):
				preview = build_album_library_preview(album(), [music_track("ORIGINAL", album_name="Playlist Imports", location=str(path))], manifest, paths)
			self.assertEqual(preview.rows[0]["status"], "upgrade_managed")
			self.assertEqual(preview.counts, {"in_library": 0, "not_in_library": 0, "upgrade_managed": 1})
			self.assertEqual(preview.manifest["recordings"][key]["music_binding"], {"persistent_id": "ORIGINAL"})

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
			health = _build_health_report(paths, library.manifest, [candidate | {"location": None} for candidate in candidates], on_progress=None, deep_all=False)
			self.assertEqual(health["summary"]["manifestInconsistencies"], 0)
			self.assertEqual(health["status"], "healthy")
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
