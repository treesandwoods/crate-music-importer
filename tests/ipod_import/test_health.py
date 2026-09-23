import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import.health import _build_health_report, save_health_report
from crate_music_importer.ipod_import.health_audit import dismiss_duplicate_alert, load_health_audit
from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest, new_manifest, save_manifest, upsert_recording


def source(title: str = "Song", artists: str = "Artist", duration_ms: int = 180000) -> dict:
	return {"title": title, "artists": artists, "album": "Album", "duration_ms": duration_ms, "sp_id": title}


def music(persistent_id: str, title: str = "Song", artists: str = "Artist", comment: str = "") -> dict:
	return {"persistent_id": persistent_id, "title": title, "artist": artists, "album": "Album", "duration_s": 180, "comment": comment, "location": None}


class HealthTests(unittest.TestCase):
	def report(self, paths, manifest, tracks):
		return _build_health_report(paths, manifest, tracks, on_progress=None, deep_all=False)

	def duplicate_report(self, paths, specs):
		tracks = []
		durations = {}
		for pid, title, artist, album, seconds, content in specs:
			path = paths.root / f"{pid}.mp3"
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes(content)
			durations[path.name] = seconds * 1000
			tracks.append({"persistent_id": pid, "title": title, "artist": artist, "album": album, "location": str(path), "duration_s": seconds})
		with patch("crate_music_importer.ipod_import.health.probe_duration_ms", side_effect=lambda path: durations[path.name]):
			return self.report(paths, new_manifest(paths), tracks)

	def test_different_measured_durations_suppress_reported_patterns(self):
		for title, artist, albums, durations in (
			("Slow Dancing in a Burning Room", "John Mayer", ("Continuum", "Where the Light Is"), (242, 319)),
			("The Girl From Ipanema", "Getz/Gilberto", ("Stan Getz", "Stan Getz"), (325, 175)),
		):
			with self.subTest(artist=artist), tempfile.TemporaryDirectory() as directory:
				paths = ManagedPaths(Path(directory) / "managed")
				report = self.duplicate_report(paths, [("ONE", title, artist, albums[0], durations[0], b"one"), ("TWO", title, artist, albums[1], durations[1], b"two")])
				self.assertEqual(report["summary"]["possibleRecordingDuplicates"], 0)
				self.assertEqual(report["checks"]["duplicates"], "passed")

	def test_cross_album_duration_boundary_is_inclusive(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			base = [("ONE", "Song", "Artist", "Original", 100, b"one"), ("TWO", "Song", "Artist", "Compilation", 102, b"two")]
			report = self.duplicate_report(paths, base)
			self.assertEqual(report["summary"]["possibleRecordingDuplicates"], 1)
			self.assertEqual(report["status"], "attention")
			self.assertEqual(report["issues"][-1]["persistentIds"], ["ONE", "TWO"])
			outside = self.duplicate_report(paths, [base[0], (*base[1][:4], 103, b"two")])
			self.assertEqual(outside["summary"]["possibleRecordingDuplicates"], 0)

	def test_percent_threshold_and_long_duration_outlier(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			base = [("ONE", "Song", "Artist", "A", 300, b"one"), ("TWO", "Song", "Artist", "B", 306, b"two"), ("THREE", "Song", "Artist", "C", 330, b"three")]
			report = self.duplicate_report(paths, base)
			self.assertEqual(report["summary"]["possibleRecordingDuplicates"], 1)
			self.assertEqual(next(issue for issue in report["issues"] if issue["category"] == "possible_recording_duplicate")["persistentIds"], ["ONE", "TWO"])
			outside = self.duplicate_report(paths, [base[0], (*base[1][:4], 307, b"two")])
			self.assertEqual(outside["summary"]["possibleRecordingDuplicates"], 0)

	def test_dismissal_persists_and_new_id_remains_reportable(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			group = [("ONE", "Song", "Artist", "Album A", 180, b"one"), ("TWO", "Song", "Artist", "Album B", 181, b"two"), ("THREE", "Song", "Artist", "Album C", 182, b"three")]
			report = self.duplicate_report(paths, group)
			save_health_report(paths, report)
			issue = next(item for item in report["issues"] if item["category"] == "possible_recording_duplicate")
			self.assertEqual(issue["persistentIds"], ["ONE", "THREE", "TWO"])
			updated = dismiss_duplicate_alert(paths, issue["id"])["lastReport"]
			self.assertEqual(json.loads((paths.state_dir / "health-duplicate-dismissals.json").read_text())["pairs"], [["ONE", "THREE"], ["ONE", "TWO"], ["THREE", "TWO"]])
			self.assertEqual(updated["summary"]["possibleRecordingDuplicates"], 0)
			self.assertEqual(updated["checks"]["duplicates"], "passed")
			self.assertEqual(updated["status"], "healthy")
			self.assertEqual(load_health_audit(paths)["lastReport"], updated)
			self.assertEqual(self.duplicate_report(paths, group)["summary"]["possibleRecordingDuplicates"], 0)
			new = self.duplicate_report(paths, [*group, ("FOUR", "Song", "Artist", "Album D", 181, b"four")])
			self.assertEqual(new["summary"]["possibleRecordingDuplicates"], 1)
			self.assertIn("FOUR", next(item for item in new["issues"] if item["category"] == "possible_recording_duplicate")["persistentIds"])

	def test_exact_file_duplicate_is_unaffected_by_dismissal(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory) / "managed")
			specs = [("ONE", "Song", "Artist", "A", 180, b"one"), ("TWO", "Song", "Artist", "B", 181, b"two"), ("THREE", "Else", "Artist", "C", 200, b"same"), ("FOUR", "Other", "Artist", "D", 200, b"same")]
			report = self.duplicate_report(paths, specs)
			save_health_report(paths, report)
			issue = next(item for item in report["issues"] if item["category"] == "possible_recording_duplicate")
			updated = dismiss_duplicate_alert(paths, issue["id"])["lastReport"]
			self.assertEqual(updated["summary"]["exactDuplicateGroups"], 1)
			self.assertEqual(updated["checks"]["duplicates"], "attention")
			self.assertEqual(updated["status"], "attention")
			self.assertEqual(self.duplicate_report(paths, specs)["summary"]["exactDuplicateGroups"], 1)
			with self.assertRaises(ValueError):
				dismiss_duplicate_alert(paths, next(item for item in updated["issues"] if item["category"] == "exact_file_duplicate")["id"])

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
			categories = {issue["category"] for issue in result["issues"]}
			self.assertIn("bound_music_identity_mismatch", categories)
			self.assertNotIn("recording_missing_from_music", categories)
			self.assertIn("incompatible_shared_binding", categories)

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
