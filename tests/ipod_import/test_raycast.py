import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from crate_music_importer.ipod_import.raycast import main


PLAYLIST_URL = "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1"
ALBUM_URL = "https://open.spotify.com/album/48a7rOjTzpD1zzJAteeveE"


class RaycastRoutingTests(unittest.TestCase):
	def test_queue_json_passes_the_explicit_action_url_and_seed(self):
		job = {"jobId": "job", "source": {"type": "album", "id": "id", "total": 1}}
		with patch("crate_music_importer.ipod_import.raycast.enqueue", return_value=job) as enqueue, \
			self.assertRaises(SystemExit) as stopped:
			main(["queue-json", "album_combined", ALBUM_URL, '{"name":"Fixture"}'])
		self.assertEqual(stopped.exception.code, 0)
		enqueue.assert_called_once_with("album_combined", ALBUM_URL, seed={"name": "Fixture"})

	def test_queue_json_rejects_missing_or_invalid_seed_data(self):
		for values in (
			["queue-json", "album_combined"],
			["queue-json", "album_combined", ALBUM_URL, "not-json"],
			["queue-json", "album_combined", ALBUM_URL, "[]"],
		):
			with self.subTest(values=values), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
				main(values)
			self.assertEqual(stopped.exception.code, 2)

	def test_unknown_raycast_action_is_rejected(self):
		with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
			main(["unsupported-action", PLAYLIST_URL])
		self.assertEqual(stopped.exception.code, 2)


if __name__ == "__main__":
	unittest.main()
