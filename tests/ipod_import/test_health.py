import tempfile
import unittest
from pathlib import Path

from crate_music_importer.ipod_import.health import _build_health_report
from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest, new_manifest, save_manifest, upsert_recording


def source(title: str = "Song", artists: str = "Artist", duration_ms: int = 180000) -> dict:
	return {"title": title, "artists": artists, "album": "Album", "duration_ms": duration_ms, "sp_id": title}


def music(persistent_id: str, title: str = "Song", artists: str = "Artist", comment: str = "") -> dict:
	return {"persistent_id": persistent_id, "title": title, "artist": artists, "album": "Album", "duration_s": 180, "comment": comment, "location": None}


class HealthTests(unittest.TestCase):
	def report(self, paths, manifest, tracks):
		return _build_health_report(paths, manifest, tracks, on_progress=None, deep_all=False)

	def test_stale_id_with_unique_current_match_repairs_silently(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source("Once Upon a Time in the West", "Dire Straits"))
			recording["music_binding"] = {"persistent_id": "STALE"}
			save_manifest(paths, manifest)
			result = self.report(paths, manifest, [music("CURRENT", "Once Upon a Time in the West", "Dire Straits")])
			self.assertEqual(result["status"], "healthy")
			self.assertEqual(result["summary"]["bindingRepairs"], 1)
			self.assertEqual(load_manifest(paths)["recordings"][key]["music_binding"], {"persistent_id": "CURRENT"})
			self.assertNotIn("manifest_id_absent", {issue["category"] for issue in result["issues"]})

	def test_missing_comment_marker_is_not_a_health_issue(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			_key, recording = upsert_recording(manifest, source())
			recording["music_binding"] = {"persistent_id": "PID"}
			result = self.report(paths, manifest, [music("PID", comment="")])
			self.assertEqual(result["status"], "healthy")
			self.assertEqual({issue["category"] for issue in result["issues"]}, {"cloud_only"})

	def test_ambiguous_matches_need_review(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			upsert_recording(manifest, source())
			result = self.report(paths, manifest, [music("ONE"), music("TWO")])
			self.assertIn("ambiguous_music_match", {issue["category"] for issue in result["issues"]})
			self.assertEqual(result["status"], "attention")

	def test_genuinely_absent_recording_warns(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			upsert_recording(manifest, source())
			result = self.report(paths, manifest, [])
			self.assertIn("recording_missing_from_music", {issue["category"] for issue in result["issues"]})

	def test_incompatible_recordings_cannot_share_one_binding_silently(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			_one, first = upsert_recording(manifest, source("Song One"))
			_two, second = upsert_recording(manifest, source("Song Two"))
			first["music_binding"] = {"persistent_id": "PID"}
			second["music_binding"] = {"persistent_id": "PID"}
			track = music("PID", "Song One")
			result = self.report(paths, manifest, [track])
			self.assertIn("recording_missing_from_music", {issue["category"] for issue in result["issues"]})

	def test_missing_managed_file_is_real_damage(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, source())
			recording["music_binding"] = {"persistent_id": "PID"}
			recording["managed_file"] = {"managed_by_crate": True, "relative_path": "Music/missing.mp3", "sha256": "abc"}
			result = self.report(paths, manifest, [music("PID")])
			issue = next(issue for issue in result["issues"] if issue["category"] == "missing_or_unreadable_file")
			self.assertEqual(issue["severity"], "critical")
			self.assertEqual(issue["recordingIds"], [key])

	def test_unregistered_external_file_is_never_treated_as_managed(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root / "managed")
			external = root / "external.mp3"
			external.write_bytes(b"user data")
			manifest = new_manifest(paths)
			_key, recording = upsert_recording(manifest, source())
			recording["music_binding"] = {"persistent_id": "PID"}
			track = music("PID")
			track["location"] = str(external)
			before = external.read_bytes()
			result = self.report(paths, manifest, [track])
			self.assertEqual(external.read_bytes(), before)
			self.assertEqual(result["summary"]["managedFiles"], 0)


if __name__ == "__main__":
	unittest.main()
