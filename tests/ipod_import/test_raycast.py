import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crate_music_importer.ipod_import.raycast import RaycastInputError, build_engine_commands, main, queue_background, read_clipboard, run, validated_url


PLAYLIST_URL = "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1"
ALBUM_URL = "https://open.spotify.com/album/48a7rOjTzpD1zzJAteeveE"


class RaycastRoutingTests(unittest.TestCase):
	def test_argument_wins_over_clipboard(self):
		url, source = validated_url(PLAYLIST_URL + "?si=argument", PLAYLIST_URL + "?si=clipboard")
		self.assertEqual(url, PLAYLIST_URL)
		self.assertEqual(source, "argument")

	def test_supplied_url_never_reads_an_unrelated_binary_clipboard(self):
		def fail_clipboard():
			raise UnicodeDecodeError("utf-8", b"\xd2", 0, 1, "invalid continuation byte")

		commands = []
		self.assertEqual(run("album_combined", [ALBUM_URL], clipboard_reader=fail_clipboard, engine=lambda command: commands.append(command) or 0), 0)
		self.assertEqual([command[0] for command in commands], ["album-import", "album-apply"])

	def test_clipboard_replacement_decode_cannot_break_raycast(self):
		with patch("crate_music_importer.ipod_import.raycast.subprocess.run", return_value=SimpleNamespace(stdout=b"prefix-\xd2-suffix\n")):
			self.assertEqual(read_clipboard(), "prefix-\ufffd-suffix")

	def test_queue_json_with_url_does_not_read_clipboard(self):
		job = {"jobId": "job", "source": {"type": "album", "id": "id", "total": 1}}
		with patch("crate_music_importer.ipod_import.raycast.read_clipboard", side_effect=AssertionError("clipboard should not be read")), \
			patch("crate_music_importer.ipod_import.raycast.enqueue", return_value=job), \
			self.assertRaises(SystemExit) as stopped:
			main(["queue-json", "album_combined", ALBUM_URL])
		self.assertEqual(stopped.exception.code, 0)

	def test_blank_argument_uses_valid_clipboard(self):
		self.assertEqual(validated_url("", PLAYLIST_URL + "?si=clipboard"), (PLAYLIST_URL, "clipboard"))

	def test_unified_validation_accepts_album_and_playlist_urls(self):
		self.assertEqual(validated_url(PLAYLIST_URL, "", expected_type=None), (PLAYLIST_URL, "argument"))
		self.assertEqual(validated_url("", ALBUM_URL + "?si=clipboard", expected_type=None), (ALBUM_URL, "clipboard"))

	def test_invalid_or_wrong_source_is_rejected(self):
		with self.assertRaises(RaycastInputError):
			validated_url("https://open.spotify.com/track/48a7rOjTzpD1zzJAteeveE", "", expected_type=None)
		with self.assertRaises(RaycastInputError):
			validated_url(PLAYLIST_URL, "", expected_type="album")

	def test_preview_commands_are_json_and_source_specific(self):
		self.assertEqual(build_engine_commands("source_preview", PLAYLIST_URL), [["preview", PLAYLIST_URL, "--json"]])
		self.assertEqual(build_engine_commands("source_preview", ALBUM_URL), [["album-preview", ALBUM_URL, "--json"]])

	def test_combined_commands_keep_download_before_music(self):
		self.assertEqual(build_engine_commands("source_combined", PLAYLIST_URL), [
			["import", PLAYLIST_URL, "--confirm-download"],
			["apply", PLAYLIST_URL, "--confirm-music-write"],
		])
		self.assertEqual(build_engine_commands("source_combined", ALBUM_URL), [
			["album-import", ALBUM_URL, "--confirm-download"],
			["album-apply", ALBUM_URL, "--confirm-music-write"],
		])

	def test_combined_stops_before_music_when_import_needs_attention(self):
		commands = []

		def engine(command):
			commands.append(command)
			return 2

		self.assertEqual(run("source_combined", [PLAYLIST_URL], clipboard_reader=lambda: "", engine=engine), 2)
		self.assertEqual(commands, [["import", PLAYLIST_URL, "--confirm-download"]])

	def test_compatibility_queue_creates_durable_job_and_detached_runner(self):
		calls = []

		def popen(command, **kwargs):
			calls.append((command, kwargs))
			return SimpleNamespace(pid=4321)

		with tempfile.TemporaryDirectory() as directory, patch("crate_music_importer.ipod_import.raycast.MANAGED_ROOT", Path(directory)):
			pid, _ = queue_background("source_combined", [PLAYLIST_URL], clipboard_reader=lambda: "", popen=popen)
			self.assertEqual(pid, 4321)
			self.assertEqual(calls[0][0][-1], "__runner")
			self.assertEqual(calls[0][1]["stdin"], -3)
			self.assertTrue(calls[0][1]["start_new_session"])
			jobs = list((Path(directory) / ".state" / "jobs").glob("*.json"))
			self.assertEqual(len(jobs), 1)


if __name__ == "__main__":
	unittest.main()
