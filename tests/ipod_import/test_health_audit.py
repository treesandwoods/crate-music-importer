import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from crate_music_importer.ipod_import import health_audit as audit
from crate_music_importer.ipod_import.manifest import ManagedPaths


class HealthAuditTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.paths = ManagedPaths(Path(self.temp.name))
		self.report = {"schemaVersion": 1, "checkedAt": "2000-01-01", "status": "healthy", "summary": {}, "checks": {}, "issues": []}
		audit.save_health_report(self.paths, self.report)
		lock = patch.object(audit, "dependency_lock", return_value=contextlib.nullcontext())
		lock.start()
		self.addCleanup(lock.stop)

	def start(self, deep=False):
		with patch.object(audit.subprocess, "Popen", return_value=Mock(pid=os.getpid())) as launch:
			state = audit.start_health_audit(self.paths, deep)
			self.assertTrue(launch.call_args.kwargs["start_new_session"])
			self.assertTrue(launch.call_args.kwargs["close_fds"])
			self.assertEqual(launch.call_args.kwargs["stdout"], audit.subprocess.DEVNULL)
		return state

	def test_start_and_reattach_never_scan(self):
		with patch.object(audit, "scan_music_library_for_health") as scan:
			state = self.start()
			with patch.object(audit.subprocess, "Popen") as launch:
				self.assertEqual(audit.start_health_audit(self.paths)["runId"], state["runId"])
				launch.assert_not_called()
			scan.assert_not_called()
		self.assertEqual(audit.load_health_audit(self.paths)["lastReport"], self.report)

	def test_modes_progress_completion_and_notification(self):
		for deep in (False, True):
			state = self.start(deep)
			def build(*args, **kwargs):
				self.assertEqual(kwargs["deep_all"], deep)
				kwargs["on_progress"]({"checked": 2, "total": 4})
				progress = audit.load_health_audit(self.paths)
				self.assertEqual(progress["checked"], 2)
				self.assertEqual(progress["phase"], "Checking files")
				return dict(self.report)
			with patch.object(audit, "load_manifest", return_value={}), patch.object(audit, "scan_music_library_for_health", return_value=[]), patch.object(audit, "refresh_music_cache") as refresh, patch.object(audit, "build_health_report", side_effect=build), patch.object(audit, "_notify") as notify:
				audit.run_worker(self.paths, state["runId"])
				refresh.assert_called_once_with(self.paths, [])
				notify.assert_called_once_with(False)
			self.assertEqual(audit.load_health_audit(self.paths)["status"], "complete")

	def test_failure_saves_terminal_report(self):
		state = self.start()
		with patch.object(audit, "load_manifest", side_effect=RuntimeError("denied")), patch.object(audit, "_notify"):
			audit.run_worker(self.paths, state["runId"])
		result = audit.load_health_audit(self.paths)
		self.assertEqual(result["status"], "failed")
		self.assertEqual(result["lastReport"]["error"], "denied")

	def test_interrupted_preserves_report_and_can_retry(self):
		self.start()
		with patch.object(audit, "_alive", return_value=False):
			result = audit.load_health_audit(self.paths)
		self.assertEqual(result["status"], "failed")
		self.assertIn("interrupted", result["error"])
		self.assertEqual(result["lastReport"], self.report)
		self.assertTrue(self.start()["running"])

	def test_atomic_state_failure_preserves_old_snapshot(self):
		state = self.start()
		with patch.object(audit.os, "replace", side_effect=OSError("disk full")):
			with self.assertRaises(OSError):
				audit._save(self.paths, {"partial": True})
		self.assertEqual(json.loads((self.paths.state_dir / "health-audit.json").read_text())["runId"], state["runId"])
		self.assertEqual(list(self.paths.state_dir.glob("health-state-*")), [])

	def test_report_write_failure_preserves_prior(self):
		state = self.start()
		with patch.object(audit, "load_manifest", side_effect=RuntimeError("denied")), patch.object(audit, "save_health_report", side_effect=OSError("disk full")), patch.object(audit, "_notify"):
			audit.run_worker(self.paths, state["runId"])
		result = audit.load_health_audit(self.paths)
		self.assertEqual(result["lastReport"], self.report)
		self.assertIn("Could not save", result["error"])

	def test_status_only_does_not_read_report_or_acquire_dependency_lock(self):
		from crate_music_importer.ipod_import import cli
		import io
		self.start()
		with patch.object(cli, "ManagedPaths", return_value=self.paths), patch.object(audit, "_read", wraps=audit._read) as read:
			with contextlib.redirect_stdout(io.StringIO()) as output:
				cli.run(["health-audit", "status", "--status-only"])
			self.assertNotIn("lastReport", json.loads(output.getvalue()))
			self.assertEqual([call.args[0].name for call in read.call_args_list], ["health-audit.json"])

	def test_concurrent_starts_launch_once(self):
		from concurrent.futures import ThreadPoolExecutor
		with patch.object(audit.subprocess, "Popen", return_value=Mock(pid=os.getpid())) as launch:
			with ThreadPoolExecutor(max_workers=4) as pool:
				results = list(pool.map(lambda _: audit.start_health_audit(self.paths), range(4)))
			self.assertEqual(launch.call_count, 1)
			self.assertEqual(len({state["runId"] for state in results}), 1)

	def test_launch_failure_preserves_report(self):
		with patch.object(audit.subprocess, "Popen", side_effect=OSError("cannot launch")):
			state = audit.start_health_audit(self.paths)
		self.assertEqual(state["status"], "failed")
		self.assertEqual(state["lastReport"], self.report)
		self.assertIn("cannot launch", state["error"])

	def test_recovers_crash_between_report_and_terminal_state(self):
		state = self.start()
		state.pop("lastReport")
		state["reportCheckedAt"] = "2099-01-01"
		audit._save(self.paths, state)
		audit.save_health_report(self.paths, {**self.report, "checkedAt": state["reportCheckedAt"]})
		with patch.object(audit, "_alive", return_value=False):
			self.assertEqual(audit.load_health_audit(self.paths)["status"], "complete")

	def test_stale_worker_cannot_touch_current_run(self):
		self.start()
		with patch.object(audit, "scan_music_library_for_health") as scan:
			audit.run_worker(self.paths, "stale-run")
			scan.assert_not_called()

	def test_start_returns_while_detached_fixture_process_keeps_running(self):
		import subprocess
		import sys
		import time
		popen = subprocess.Popen
		children = []
		def fixture_worker(command, **kwargs):
			# Exercise real detach/stdio behavior without invoking Music or media tools.
			child = popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
			children.append(child)
			return child
		try:
			with patch.object(audit.subprocess, "Popen", side_effect=fixture_worker):
				before = time.monotonic()
				state = audit.start_health_audit(self.paths)
				self.assertLess(time.monotonic() - before, 2)
			self.assertIsNone(children[0].poll())
			self.assertEqual(os.getsid(state["pid"]), state["pid"])
			self.assertEqual(audit.load_health_audit(self.paths)["status"], "running")
		finally:
			for child in children:
				child.terminate()
				child.wait(timeout=5)
		self.assertEqual(audit.load_health_audit(self.paths)["status"], "failed")
