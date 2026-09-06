import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from crate_music_importer.ipod_import import dependencies as d
from crate_music_importer.ipod_import.cli import run
from crate_music_importer.ipod_import.dependency_lock import dependency_lock, DependenciesBusyError


class DependencyTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		self.addCleanup(patch.stopall)
		patch.dict(os.environ, {"CRATE_APPLICATION_STATE": str(self.root / "app")}).start()
		patch.object(d, "MANAGED_ROOT", self.root / "music").start()

	def tool(self, name, version="1.0", tap="homebrew/core"):
		formula = "ffmpeg" if name == "ffprobe" else name
		keg = self.root / "Cellar" / formula / version
		(keg / "bin").mkdir(parents=True, exist_ok=True)
		(keg / "INSTALL_RECEIPT.json").write_text(json.dumps({"source": {"tap": tap}}))
		path = keg / "bin" / name
		path.touch()
		return str(path)

	def setup_status(self, version="1.0", available="2.0", failure=None):
		paths = {name: self.tool(name, version) for name in ("yt-dlp", "ffmpeg", "ffprobe", "deno")}
		paths["brew"] = "/example/bin/brew"
		patch.object(d, "resolve_tool", side_effect=paths.get).start()
		patch.object(d, "version", return_value=version).start()
		patch.object(d, "formula_metadata", side_effect=failure if failure else lambda name: {"versions": {"stable": available}, "dependencies": ["deno"]}).start()
		def command(args, timeout=30):
			if args[1] == "--cellar": return str(self.root / "Cellar")
			if args[-1] == "--dry-run": return f"==> Would upgrade 1 package:\n{args[-2]} {version} -> {available}\n"
			return ""
		return paths, patch.object(d, "run_command", side_effect=command).start()

	def test_version_parsing(self):
		for name, text, result in [("yt-dlp", "2026.08.19", "2026.08.19"), ("deno", "deno 2.4.1 (stable)", "2.4.1"), ("ffmpeg", "ffmpeg version 8.0 Copyright", "8.0"), ("ffprobe", "ffprobe version 8.0_1", "8.0_1")]:
			self.assertEqual(d.parse_version(name, text), result)
		with self.assertRaises(ValueError): d.parse_version("yt-dlp", "unavailable")
		self.assertEqual(d.stable_version_key("2026.08.19"), d.stable_version_key("2026.8.19"))

	def test_ownership_requires_selected_core_receipt_not_prefix(self):
		path = self.tool("yt-dlp")
		self.assertTrue(d.homebrew_owns(path, str(self.root / "Cellar"), "yt-dlp"))
		self.assertFalse(d.homebrew_owns(path, str(self.root / "Cellar"), "ffmpeg"))
		custom = self.tool("deno", tap="some/tap")
		self.assertFalse(d.homebrew_owns(custom, str(self.root / "Cellar"), "deno"))
		self.assertFalse(d.homebrew_owns("/usr/local/bin/yt-dlp", None, "yt-dlp"))

	def test_plan_rejects_extra_packages_unknown_output_and_version_change(self):
		self.assertTrue(d.safe_upgrade_plan("yt-dlp 1.0 -> 2.0", "yt-dlp", "2.0"))
		for output in ("yt-dlp 1.0 -> 2.0\npython@3.14 1.0 -> 2.0", "yt-dlp 1.0 -> 2.0\nWould install 1 dependency:", "unknown output", "yt-dlp 1.0 -> 3.0"):
			self.assertFalse(d.safe_upgrade_plan(output, "yt-dlp", "2.0"))

	def test_outdated_current_and_newer(self):
		self.setup_status()
		rows = d.status()["dependencies"]
		self.assertTrue(all(row["status"] == "outdated" for row in rows))
		self.assertTrue(all(row["command"][1:3] == ["upgrade", "--formula"] for row in rows))
		with patch.object(d, "formula_metadata", return_value={"versions": {"stable": "1.0"}}):
			self.assertTrue(all(row["status"] == "current" for row in d.status()["dependencies"]))
		with patch.object(d, "formula_metadata", return_value={"versions": {"stable": "0.9"}}):
			self.assertTrue(all(row["status"] == "current" for row in d.status()["dependencies"]))

	def test_missing_unknown_and_homebrew_absent(self):
		paths, _ = self.setup_status()
		paths["yt-dlp"] = "/unknown/yt-dlp"
		paths["deno"] = None
		paths["brew"] = None
		rows = d.status()["dependencies"]
		self.assertEqual(rows[0]["installationMethod"], "unknown")
		self.assertEqual(rows[2]["installationMethod"], "missing")
		self.assertTrue(all(row["status"] == "skipped" for row in rows))

	def test_offline_unavailable_and_timeout_checks(self):
		self.setup_status(failure=TimeoutError("offline"))
		self.assertTrue(all(row["status"] == "failed" and "offline" in row["error"] for row in d.status()["dependencies"]))
		with patch.object(d, "formula_metadata", side_effect=ValueError("formula unavailable")):
			self.assertTrue(all(row["status"] == "failed" for row in d.status()["dependencies"]))

	def test_shared_lock_allows_consumers_blocks_update_and_concurrent_update(self):
		with dependency_lock():
			with dependency_lock(): pass
			with self.assertRaises(DependenciesBusyError):
				with dependency_lock(exclusive=True): pass
		with dependency_lock(exclusive=True):
			with self.assertRaises(DependenciesBusyError):
				with dependency_lock(): pass
			with self.assertRaises(DependenciesBusyError):
				with dependency_lock(exclusive=True): pass

	def test_refuses_active_import_without_invoking_homebrew(self):
		with patch.object(d, "active_import", return_value=True), patch.object(d, "_status") as status:
			with self.assertRaisesRegex(RuntimeError, "active"): d.update("plan")
			status.assert_not_called()

	def test_changed_plan_requires_new_confirmation(self):
		self.setup_status()
		with self.assertRaisesRegex(RuntimeError, "plan changed"): d.update("old-plan")

	def test_partial_failure_continues_sequentially_and_records_result(self):
		paths, _ = self.setup_status()
		plan = d.status()
		calls = []
		def command(args, timeout=30):
			if args[1] == "--cellar": return str(self.root / "Cellar")
			if args[-1] == "--dry-run": return f"{args[-2]} 1.0 -> 2.0"
			name = args[-1]; calls.append(name)
			if name == "ffmpeg": raise PermissionError("access_token=secret permission denied")
			paths[name] = self.tool(name, "2.0")
			return ""
		checks = {name: {"path": path, "version": "2.0", "error": None} for name, path in paths.items() if name != "brew"}
		checks["metadataSmokeTest"] = {"status": "passed", "error": None}
		with patch.object(d, "_status", return_value=plan), patch.object(d, "run_command", side_effect=command), patch.object(d, "validate_tools", return_value=checks):
			result = d.update(plan["planId"])
		self.assertEqual(calls, ["yt-dlp", "ffmpeg", "deno"])
		self.assertEqual([t["status"] for t in result["dependencies"]], ["updated", "failed", "updated"])
		text = (self.root / "app/dependencies-last-result.json").read_text()
		self.assertNotIn("secret", text)
		self.assertEqual(json.loads(text)["operation"], "update")
		self.assertFalse((self.root / "music").exists())

	def test_current_validation_and_metadata_failure_are_separate(self):
		paths, _ = self.setup_status(available="1.0")
		with patch.object(d, "run_command", return_value='{"id":"test","title":"Test"}'):
			checks = d.validate_tools()
		self.assertEqual(checks["metadataSmokeTest"]["status"], "passed")
		with patch.object(d, "run_command", side_effect=RuntimeError("offline")):
			checks = d.validate_tools()
		self.assertEqual(checks["metadataSmokeTest"]["status"], "failed")

	def test_cli_json_and_confirmation_required(self):
		with patch.object(d, "status", return_value={"operationTime":"now","dependencies":[]}), contextlib.redirect_stdout(io.StringIO()) as output:
			self.assertEqual(run(["dependencies", "status"]), 0)
		self.assertEqual(json.loads(output.getvalue())["dependencies"], [])
		with contextlib.redirect_stdout(io.StringIO()) as output:
			self.assertEqual(run(["dependencies", "update"]), 1)
		self.assertIn("confirm", json.loads(output.getvalue())["error"])

	def test_timeout_kills_process_group(self):
		process = MagicMock(pid=123)
		process.communicate.side_effect = [subprocess.TimeoutExpired("brew", 1), ("", "")]
		with patch.object(d.subprocess, "Popen", return_value=process), patch.object(d.os, "killpg") as kill:
			with self.assertRaisesRegex(RuntimeError, "timed out"): d.run_command(["brew", "upgrade"], 1)
			kill.assert_called_once()

	def test_redaction(self):
		for text in ("Authorization=secret", "Bearer secret", "https://name:secret@example.test", "cookie=secret", "refresh_token=secret"):
			self.assertNotIn("secret", d.redact(text))
