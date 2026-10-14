import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import import activity, cli, raycast
from crate_music_importer.ipod_import.jobs import JobStore, _new_job, jobs_snapshot
from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest, new_manifest, save_manifest, set_playlist, upsert_recording
from crate_music_importer.ipod_import.resolver import resolver_snapshot


class ActivityTests(unittest.TestCase):
	def test_combined_snapshot_preserves_fields_and_reuses_manifest_and_jobs(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest = new_manifest(paths)
			url = "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1"
			key, recording = upsert_recording(manifest, {"title": "Song", "artists": "Artist", "sp_id": "track"})
			recording["review"] = {"kind": "youtube_missing", "candidates": [], "message": "Choose"}
			set_playlist(manifest, {"id": "37i9dQZF1DXTESTFIXTURE1", "name": "Playlist", "url": url}, [{"recording_id": key, "position": 1}])
			save_manifest(paths, manifest)
			store = JobStore(paths.root)
			job = _new_job("playlist_combined", url)
			job["runnerPid"] = 123
			store.save(job)
			expected = {"resolver": resolver_snapshot(paths), "jobs": jobs_snapshot(root=paths.root)}
			with patch.object(activity, "load_manifest", wraps=load_manifest) as read, patch.object(JobStore, "list", autospec=True, side_effect=lambda value: [value.load(job["jobId"])]) as listing, patch.object(JobStore, "save", side_effect=AssertionError("unchanged snapshot must not write")):
				result = activity.activity_snapshot(root=paths.root)
			self.assertEqual(result, expected)
			self.assertEqual(read.call_count, 1)
			self.assertEqual(listing.call_count, 1)
			self.assertEqual(result["resolver"]["sources"][0]["activeJob"]["pid"], 123)

	def test_empty_combined_snapshot_and_additive_raycast_action(self):
		with tempfile.TemporaryDirectory() as directory:
			result = activity.activity_snapshot(root=Path(directory))
			self.assertEqual(result["resolver"]["sources"], [])
			self.assertEqual(result["jobs"]["jobs"], [])
			with patch.object(activity, "activity_snapshot", return_value=result), contextlib.redirect_stdout(io.StringIO()) as output:
				with self.assertRaises(SystemExit) as exit:
					raycast.main(["activity-json"])
			self.assertEqual(exit.exception.code, 0)
			self.assertEqual(json.loads(output.getvalue()), result)

	def test_cached_saved_playlists_returns_artwork_without_network_or_writes(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			manifest = new_manifest(paths)
			manifest["playlists"]["playlist"] = {"name": "Saved", "spotify_url": "saved-link", "cover_url": "https://example.test/saved.jpg", "items": []}
			save_manifest(paths, manifest)
			before = paths.manifest.read_bytes()
			with patch.object(cli, "ManagedPaths", return_value=paths), patch.object(cli, "fetch_playlist_cover", side_effect=AssertionError("cached startup must not fetch")), contextlib.redirect_stdout(io.StringIO()) as output:
				self.assertEqual(cli._run(["saved-playlists", "--cached", "--json"]), 0)
			self.assertEqual(json.loads(output.getvalue())["playlists"][0]["cover_url"], "https://example.test/saved.jpg")
			self.assertEqual(paths.manifest.read_bytes(), before)
