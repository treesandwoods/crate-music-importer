import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import import tooling


class ToolingTests(unittest.TestCase):
	def test_path_resolution_precedes_standard_homebrew_fallbacks(self):
		with tempfile.TemporaryDirectory() as directory:
			executable = Path(directory) / "fixture-tool"
			executable.touch()
			with patch.object(tooling.shutil, "which", return_value=str(executable)):
				self.assertEqual(tooling.resolve_executable("fixture-tool"), str(executable))

	def test_missing_executable_returns_none(self):
		with patch.object(tooling.shutil, "which", return_value=None), patch.object(tooling, "HOMEBREW_BIN_DIRECTORIES", ()):
			self.assertIsNone(tooling.resolve_executable("missing-fixture-tool"))


if __name__ == "__main__":
	unittest.main()
