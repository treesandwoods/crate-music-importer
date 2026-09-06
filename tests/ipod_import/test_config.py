import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from crate_music_importer.ipod_import.config import configured_path, load_config


class ConfigTests(unittest.TestCase):
	def test_config_environment_and_home_paths(self):
		with tempfile.TemporaryDirectory() as directory:
			config = Path(directory) / "config.json"
			config.write_text(json.dumps({"managedMusicDirectory": "~/Music/Test"}))
			with patch.dict(os.environ, {"CRATE_CONFIG_FILE": str(config), "HOME": directory}, clear=True), patch.object(Path, "home", return_value=Path(directory)):
				self.assertEqual(configured_path("managedMusicDirectory", "TEST_ROOT", Path(directory)), Path(directory) / "Music/Test")
				with patch.dict(os.environ, {"TEST_ROOT": directory}):
					self.assertEqual(configured_path("managedMusicDirectory", "TEST_ROOT", Path("/fallback")), Path(directory))
				config.write_text('{"managedMusicDirectory":"relative"}')
				with self.assertRaises(ValueError): configured_path("managedMusicDirectory", "TEST_ROOT", Path(directory))
				config.write_text('[]')
				with self.assertRaises(ValueError): load_config()
