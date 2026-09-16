import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import.manifest import ManagedPaths, file_sha256, new_manifest, upsert_recording
from crate_music_importer.ipod_import.media import MediaError, _run, audio_sha256, available_managed_relative_path, download_recording, has_tcmp, retag_managed_recording_artwork


class MediaFixtureTests(unittest.TestCase):
	def test_canonical_path_collision_uses_stable_recording_suffix(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			recording = {
				"recording_id": "rec_1234567890abcdef",
				"source_metadata": {"title": "A Song"},
				"album_metadata": None,
			}
			requested = paths.root / "Music/Compilations/Playlist Imports/A Song.mp3"
			requested.parent.mkdir(parents=True)
			requested.write_bytes(b"another recording")
			self.assertEqual(
				available_managed_relative_path(recording, paths),
				"Music/Compilations/Playlist Imports/A Song — 1234567890.mp3",
			)

	@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
	def test_album_download_has_real_album_tags_and_is_not_a_compilation(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			_, recording = upsert_recording(manifest, {
				"title": "Album Fixture Song - 2005 Remaster",
				"artists": "Album Artist",
				"album": "Fixture Album (Deluxe Edition)",
				"album_artist": "Album Artist",
				"album_id": "fixture-album",
				"duration_ms": 1200,
				"sp_id": "fixture-album-track",
				"track_no": 2,
				"track_total": 8,
				"disc_no": 1,
				"disc_total": 1,
				"release_year": 2015,
				"cover_url": None,
			}, source_type="album")
			recording["youtube"] = {"url": "https://www.youtube.com/watch?v=unused-album-fixture", "video_id": "unused-album-fixture"}
			staging = paths.staging / recording["recording_id"]
			staging.mkdir(parents=True)
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "sine=frequency=440:duration=1.2", str(staging / "source--unused-album-fixture.wav"),
			], check=True)
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "color=c=blue:s=800x600", "-frames:v", "1", str(staging / "source--unused-album-fixture.jpg"),
			], check=True)
			managed = download_recording(recording, paths)
			target = paths.root / managed["relative_path"]
			self.assertEqual(managed["relative_path"], "Music/Album Artist/Fixture Album/02 — Album Fixture Song.mp3")
			self.assertEqual(managed["metadata_profile"], "album")
			self.assertFalse(has_tcmp(target))
			probe = subprocess.run([
				shutil.which("ffprobe"), "-v", "error", "-show_entries", "format_tags", "-of", "json", str(target),
			], capture_output=True, text=True, check=True)
			tags = json.loads(probe.stdout)["format"]["tags"]
			self.assertEqual(tags["title"], "Album Fixture Song")
			self.assertEqual(tags["album"], "Fixture Album")
			self.assertEqual(tags["album_artist"], "Album Artist")
			self.assertEqual(tags["genre"], "Music")
			self.assertEqual(tags["track"], "2/8")
			self.assertEqual(tags["disc"], "1/1")
			self.assertEqual(tags["date"], "2015")

	def test_command_output_streams_to_progress_callback(self):
		lines = []
		result = _run([sys.executable, "-u", "-c", "print('progress 10%'); print('progress 100%')"], on_output=lines.append)
		self.assertEqual(result.returncode, 0)
		self.assertIn("progress 10%", lines)
		self.assertIn("progress 100%", lines)

	@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
	def test_local_fixture_conversion_has_ipod_metadata_and_square_art(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			_, recording = upsert_recording(manifest, {
				"title": "Fixture Song (Live in Stereo) - 2005 Remaster",
				"artists": "Fixture Artist",
				"album": "Original Fixture Album",
				"duration_ms": 1200,
				"sp_id": "fixture-track",
				"isrc": "USFIX0000001",
				"cover_url": None,
			})
			recording["youtube"] = {"url": "https://www.youtube.com/watch?v=unused-fixture", "video_id": "unused-fixture"}
			staging = paths.staging / recording["recording_id"]
			staging.mkdir(parents=True)
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "sine=frequency=440:duration=1.2", str(staging / "source--unused-fixture.wav"),
			], check=True)
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "color=c=red:s=800x600", "-frames:v", "1", str(staging / "source--unused-fixture.jpg"),
			], check=True)
			managed = download_recording(recording, paths)
			target = paths.root / managed["relative_path"]
			self.assertEqual(managed["relative_path"], "Music/Compilations/Playlist Imports/Fixture Song (Live).mp3")
			self.assertTrue(target.is_file())
			self.assertTrue(has_tcmp(target))
			self.assertAlmostEqual(managed["duration_ms"], 1200, delta=100)
			self.assertAlmostEqual(managed["source_duration_ms"], 1200, delta=100)
			self.assertEqual(managed["audio_sha256"], audio_sha256(target))
			probe = subprocess.run([
				shutil.which("ffprobe"), "-v", "error", "-show_entries", "format_tags:stream=codec_name,width,height,disposition",
				"-of", "json", str(target),
			], capture_output=True, text=True, check=True)
			data = json.loads(probe.stdout)
			tags = data["format"]["tags"]
			self.assertEqual(tags["title"], "Fixture Song (Live)")
			self.assertEqual(tags["artist"], "Fixture Artist")
			self.assertEqual(tags["album"], "Playlist Imports")
			self.assertEqual(tags["album_artist"], "Various Artists")
			self.assertEqual(tags["genre"], "Music")
			self.assertNotIn("track", tags)
			art = next(stream for stream in data["streams"] if stream.get("codec_name") == "mjpeg")
			self.assertEqual((art["width"], art["height"]), (600, 600))

	@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
	def test_playlist_artwork_repair_preserves_audio_and_playlist_import_tags(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			_, recording = upsert_recording(manifest, {
				"title": "Fixture Song",
				"artists": "Fixture Artist",
				"album": "Original Fixture Album",
				"duration_ms": 1200,
				"sp_id": "fixture-track",
				"cover_url": None,
			})
			recording["youtube"] = {"url": "https://www.youtube.com/watch?v=unused-fixture", "video_id": "unused-fixture"}
			staging = paths.staging / recording["recording_id"]
			staging.mkdir(parents=True)
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "sine=frequency=440:duration=1.2", str(staging / "source--unused-fixture.wav"),
			], check=True)
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "color=c=red:s=800x600", "-frames:v", "1", str(staging / "source--unused-fixture.jpg"),
			], check=True)
			recording["managed_file"] = download_recording(recording, paths)
			original_audio_sha = recording["managed_file"]["audio_sha256"]
			recording["source_metadata"]["cover_url"] = "https://example.test/correct-album.jpg"
			new_cover = paths.staging / "correct-album.jpg"
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "color=c=blue:s=900x700", "-frames:v", "1", str(new_cover),
			], check=True)

			def download_cover(_url, target):
				target.write_bytes(new_cover.read_bytes())
				return target

			with patch("crate_music_importer.ipod_import.media._download_cover", side_effect=download_cover):
				managed = retag_managed_recording_artwork(recording, paths)
			target = paths.root / managed["relative_path"]
			self.assertEqual(managed["artwork_source_url"], "https://example.test/correct-album.jpg")
			self.assertEqual(managed["audio_sha256"], original_audio_sha)
			self.assertTrue(has_tcmp(target))
			probe = subprocess.run([
				shutil.which("ffprobe"), "-v", "error", "-show_entries", "format_tags:stream=codec_name,width,height",
				"-of", "json", str(target),
			], capture_output=True, text=True, check=True)
			data = json.loads(probe.stdout)
			self.assertEqual(data["format"]["tags"]["album"], "Playlist Imports")
			self.assertEqual(data["format"]["tags"]["album_artist"], "Various Artists")
			art = next(stream for stream in data["streams"] if stream.get("codec_name") == "mjpeg")
			self.assertEqual((art["width"], art["height"]), (600, 600))

	@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
	def test_legacy_retag_accepts_tag_drift_only_after_matching_saved_source_audio(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			_, recording = upsert_recording(manifest, {
				"title": "Legacy Fixture",
				"artists": "Fixture Artist",
				"album": "Original Fixture Album",
				"duration_ms": 1200,
				"sp_id": "legacy-fixture-track",
				"cover_url": None,
			})
			recording["youtube"] = {"url": "https://www.youtube.com/watch?v=legacy-fixture", "video_id": "legacy-fixture"}
			staging = paths.staging / recording["recording_id"]
			staging.mkdir(parents=True)
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "sine=frequency=440:duration=1.2", str(staging / "source--legacy-fixture.wav"),
			], check=True)
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "color=c=red:s=800x600", "-frames:v", "1", str(staging / "source--legacy-fixture.jpg"),
			], check=True)
			recording["managed_file"] = download_recording(recording, paths)
			target = paths.root / recording["managed_file"]["relative_path"]
			recorded_sha = recording["managed_file"]["sha256"]
			recording["managed_file"].pop("audio_sha256")
			drifted = target.with_suffix(".drifted.mp3")
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-i", str(target), "-map", "0", "-c", "copy",
				"-metadata", f"comment=Managed by Spotify iPod Importer; recording_id={recording['recording_id']}",
				"-id3v2_version", "3", str(drifted),
			], check=True)
			drifted.replace(target)
			self.assertNotEqual(file_sha256(target), recorded_sha)
			recording["source_metadata"]["cover_url"] = "https://example.test/correct-album.jpg"
			new_cover = staging / "correct-album.jpg"
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "color=c=blue:s=900x700", "-frames:v", "1", str(new_cover),
			], check=True)

			with patch("crate_music_importer.ipod_import.media._download_cover", side_effect=lambda _url, path: path.write_bytes(new_cover.read_bytes()) and path):
				managed = retag_managed_recording_artwork(recording, paths)
			self.assertEqual(managed["audio_sha256"], audio_sha256(target))
			self.assertEqual(managed["artwork_source_url"], "https://example.test/correct-album.jpg")

	@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
	def test_legacy_retag_still_refuses_changed_audio(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			_, recording = upsert_recording(manifest, {
				"title": "Changed Audio",
				"artists": "Fixture Artist",
				"duration_ms": 1200,
				"sp_id": "changed-audio-track",
				"cover_url": "https://example.test/cover.jpg",
			})
			recording["youtube"] = {"url": "https://www.youtube.com/watch?v=changed-audio", "video_id": "changed-audio"}
			staging = paths.staging / recording["recording_id"]
			staging.mkdir(parents=True)
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "sine=frequency=440:duration=1.2", str(staging / "source--changed-audio.wav"),
			], check=True)
			target = paths.root / "tracks" / "changed-audio.mp3"
			target.parent.mkdir(parents=True, exist_ok=True)
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "sine=frequency=880:duration=1.2", "-metadata",
				f"comment=Managed by Crate Music Importer; recording_id={recording['recording_id']}", str(target),
			], check=True)
			recording["managed_file"] = {
				"relative_path": "tracks/changed-audio.mp3",
				"sha256": "0" * 64,
				"managed_by_crate": True,
				"metadata_profile": "playlist",
			}
			with self.assertRaisesRegex(MediaError, "Managed file hash changed"):
				retag_managed_recording_artwork(recording, paths)

	@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
	def test_registered_truncated_mp3_is_rebuilt_from_staging(self):
		with tempfile.TemporaryDirectory() as directory:
			paths = ManagedPaths(Path(directory))
			paths.create()
			manifest = new_manifest(paths)
			_, recording = upsert_recording(manifest, {
				"title": "Fixture Song",
				"artists": "Fixture Artist",
				"album": "Original Fixture Album",
				"duration_ms": 1200,
				"sp_id": "fixture-track",
				"cover_url": None,
			})
			recording["youtube"] = {"url": "https://www.youtube.com/watch?v=unused-fixture", "video_id": "unused-fixture"}
			staging = paths.staging / recording["recording_id"]
			staging.mkdir(parents=True)
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "sine=frequency=440:duration=1.2", str(staging / "source--unused-fixture.wav"),
			], check=True)
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "color=c=red:s=800x600", "-frames:v", "1", str(staging / "source--unused-fixture.jpg"),
			], check=True)
			managed = download_recording(recording, paths)
			target = paths.root / managed["relative_path"]
			subprocess.run([
				shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
				"-f", "lavfi", "-i", "sine=frequency=440:duration=0.05", "-c:a", "libmp3lame", str(target),
			], check=True)
			managed["sha256"] = file_sha256(target)
			recording["managed_file"] = managed
			messages = []
			repaired = download_recording(recording, paths, on_output=messages.append)
			self.assertAlmostEqual(repaired["duration_ms"], 1200, delta=100)
			self.assertTrue(any("rebuilding it" in message for message in messages))


if __name__ == "__main__":
	unittest.main()
