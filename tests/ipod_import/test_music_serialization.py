import contextlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from crate_music_importer.ipod_import import cli, music


class MusicSerializationTests(unittest.TestCase):
	def test_parallel_automation_calls_are_serialized(self):
		entered = threading.Event()
		release = threading.Event()
		second_entered = threading.Event()
		def subprocess(command, **kwargs):
			if "first" in command:
				entered.set()
				self.assertTrue(release.wait(2))
			else:
				second_entered.set()
			return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
		with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"CRATE_APPLICATION_STATE": directory}), patch.object(music.subprocess, "run", side_effect=subprocess):
			with ThreadPoolExecutor(max_workers=2) as pool:
				first = pool.submit(music._osascript, "first")
				self.assertTrue(entered.wait(2))
				second = pool.submit(music._osascript, "second")
				overlapped = second_entered.wait(0.05)
				release.set()
				first.result(timeout=2)
				second.result(timeout=2)
		self.assertFalse(overlapped)

	def test_parallel_cli_music_writes_hold_one_operation_lock(self):
		entered = threading.Event()
		release = threading.Event()
		second_entered = threading.Event()
		def run(arguments, **kwargs):
			if arguments[1] == "first":
				entered.set()
				self.assertTrue(release.wait(2))
			else:
				second_entered.set()
			return 0
		with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"CRATE_APPLICATION_STATE": directory}), patch.object(cli, "_run", side_effect=run), patch("crate_music_importer.ipod_import.dependency_lock.dependency_lock", return_value=contextlib.nullcontext()):
			with ThreadPoolExecutor(max_workers=2) as pool:
				first = pool.submit(cli.run, ["apply", "first", "--confirm-music-write"])
				self.assertTrue(entered.wait(2))
				second = pool.submit(cli.run, ["album-apply", "second", "--confirm-music-write"])
				overlapped = second_entered.wait(0.05)
				release.set()
				first.result(timeout=2)
				second.result(timeout=2)
		self.assertFalse(overlapped)
