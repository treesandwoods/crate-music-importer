import json
import tempfile
import unittest
from pathlib import Path

from crate_music_importer.ipod_import import health_audit as audit
from crate_music_importer.ipod_import.manifest import ManagedPaths


class HealthCorruptionTests(unittest.TestCase):
	def test_corrupt_controller_keeps_report_and_original_state(self):
		for raw in ("broken", "[]", '{"status":"running","pid":"invalid"}', '{"status":[]}'):
			with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
				paths = ManagedPaths(Path(directory))
				report = {"schemaVersion": 1, "checkedAt": "saved", "status": "healthy", "summary": {}, "checks": {}, "issues": []}
				audit.save_health_report(paths, report)
				state_path = paths.state_dir / "health-audit.json"
				state_path.write_text(raw)
				result = audit.load_health_audit(paths)
				self.assertEqual(result["status"], "failed")
				self.assertIn("Refresh Library Health", result["error"])
				self.assertEqual(result["lastReport"], report)
				self.assertEqual(state_path.read_text(), raw)

	def test_corrupt_report_is_actionable_and_not_overwritten(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.state_dir.mkdir(parents=True)
			path = paths.state_dir / "health-last-result.json"
			path.write_text(json.dumps(["invalid"]))
			before = path.read_bytes()
			result = audit.load_health_audit(paths)
			self.assertEqual(result["lastReport"]["status"], "failed")
			self.assertIn("Refresh Library Health", result["lastReport"]["error"])
			self.assertEqual(path.read_bytes(), before)
