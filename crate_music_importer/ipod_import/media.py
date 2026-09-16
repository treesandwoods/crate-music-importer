"""Download and atomically create Crate-managed, iPod-compatible MP3 files."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from crate_music_importer.ipod_import.constants import IMPORT_ALBUM, IMPORT_ALBUM_ARTIST, IMPORT_GENRE
from crate_music_importer.ipod_import.manifest import ManagedPaths, file_sha256, managed_relative_path
from crate_music_importer.ipod_import.tooling import resolve_executable
from crate_music_importer.ipod_import.youtube import YouTubeError, ytdlp_path


class MediaError(RuntimeError):
	pass


def _tool(name: str) -> str:
	resolved = resolve_executable(name)
	if resolved:
		return resolved
	raise MediaError(f"{name} is required. Install the prerequisites in the README, then run Library Health & Updates in Raycast.")


def check_tools() -> dict[str, str]:
	return {"yt-dlp": ytdlp_path(), "ffmpeg": _tool("ffmpeg"), "ffprobe": _tool("ffprobe")}


def _run(command: list[str], *, timeout: int = 1800, on_output: Callable[[str], None] | None = None) -> subprocess.CompletedProcess[str]:
	if on_output:
		on_output(" ".join(command[:2]) + " …")
		try:
			process = subprocess.Popen(
				command,
				stdout=subprocess.PIPE,
				stderr=subprocess.STDOUT,
				text=True,
				errors="replace",
				bufsize=1,
			)
		except OSError as exc:
			raise MediaError(f"Could not start {command[0]}: {exc}") from exc
		lines: list[str] = []
		def stream_output() -> None:
			assert process.stdout is not None
			for raw_line in process.stdout:
				line = raw_line.strip("\r\n")
				if not line:
					continue
				lines.append(line)
				del lines[:-80]
				on_output(line)

		reader = threading.Thread(target=stream_output, name="crate-music-importer-command-output", daemon=True)
		reader.start()
		try:
			return_code = process.wait(timeout=timeout)
		except subprocess.TimeoutExpired as exc:
			process.kill()
			process.wait()
			reader.join(timeout=5)
			if process.stdout:
				process.stdout.close()
			raise MediaError(f"Command timed out: {command[0]}") from exc
		except BaseException:
			process.terminate()
			try:
				process.wait(timeout=5)
			except subprocess.TimeoutExpired:
				process.kill()
				process.wait()
			reader.join(timeout=5)
			if process.stdout:
				process.stdout.close()
			raise
		reader.join(timeout=5)
		if process.stdout:
			process.stdout.close()
		result = subprocess.CompletedProcess(command, return_code, "\n".join(lines), "")
	else:
		try:
			result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
		except subprocess.TimeoutExpired as exc:
			raise MediaError(f"Command timed out: {command[0]}") from exc
	if result.returncode != 0:
		lines = [line.strip() for line in (result.stderr or result.stdout or "").splitlines() if line.strip()]
		detail = lines[-1] if lines else "no diagnostic output"
		lower = detail.casefold()
		if "age-restricted" in lower or "confirm your age" in lower or "inappropriate for some users" in lower:
			raise MediaError("YouTube rejected the download because it is age-restricted.")
		if "private" in lower:
			raise MediaError("The selected YouTube video is private.")
		if "not available in your country" in lower or "geo" in lower:
			raise MediaError("The selected YouTube video is unavailable in this region.")
		if "sign in to confirm" in lower:
			raise MediaError("YouTube requested sign-in or bot verification; the track was left resumable and unresolved.")
		raise MediaError(f"{Path(command[0]).name} failed: {detail}")
	return result


def _download_cover(url: str, target: Path) -> Path | None:
	if not str(url or "").startswith(("https://", "http://")):
		return None
	request = Request(str(url), headers={"User-Agent": "Mozilla/5.0"})
	try:
		with urlopen(request, timeout=30) as response:
			content_type = str(response.headers.get("Content-Type") or "")
			if not content_type.casefold().startswith("image/"):
				return None
			data = response.read(10 * 1024 * 1024 + 1)
	except (OSError, URLError):
		return None
	if not data or len(data) > 10 * 1024 * 1024:
		return None
	target.write_bytes(data)
	return target


def _source_audio(staging: Path, video_id: str) -> Path | None:
	ignored = {".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl", ".json"}
	candidates = [path for path in staging.glob(f"source--{video_id}.*") if path.is_file() and path.suffix.casefold() not in ignored]
	return max(candidates, key=lambda path: path.stat().st_size) if candidates else None


def _source_cover(staging: Path, spotify_cover: str | None, video_id: str) -> Path | None:
	if spotify_cover:
		target = staging / "spotify-cover.image"
		target.unlink(missing_ok=True)
		return _download_cover(spotify_cover, target)
	for pattern in (f"source--{video_id}.jpg", f"source--{video_id}.jpeg", f"source--{video_id}.png", f"source--{video_id}.webp"):
		candidate = staging / pattern
		if candidate.is_file():
			return candidate
	return None


def _syncsafe(value: int) -> bytes:
	if value < 0 or value >= (1 << 28):
		raise MediaError("ID3 tag is too large.")
	return bytes(((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F))


def _unsyncsafe(value: bytes) -> int:
	if len(value) != 4:
		return 0
	return (value[0] << 21) | (value[1] << 14) | (value[2] << 7) | value[3]


def has_tcmp(path: Path) -> bool:
	data = path.read_bytes()
	if len(data) < 10 or data[:3] != b"ID3":
		return False
	tag_end = min(len(data), 10 + _unsyncsafe(data[6:10]))
	return b"TCMP" in data[10:tag_end]


def ensure_tcmp(path: Path) -> None:
	data = path.read_bytes()
	if len(data) < 10 or data[:3] != b"ID3" or data[3] != 3:
		raise MediaError("ffmpeg did not produce the required ID3v2.3 tag.")
	if has_tcmp(path):
		return
	if data[5] & 0x40:
		raise MediaError("Cannot safely add TCMP to an ID3 tag with an extended header.")
	tag_size = _unsyncsafe(data[6:10])
	tag_end = 10 + tag_size
	if tag_end > len(data):
		raise MediaError("The generated MP3 has a truncated ID3 tag.")
	payload = b"\x001"
	frame = b"TCMP" + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload
	header = data[:6] + _syncsafe(tag_size + len(frame))
	temporary = path.with_suffix(path.suffix + ".tcmp")
	with temporary.open("wb") as handle:
		handle.write(header)
		handle.write(frame)
		handle.write(data[10:])
	os.replace(temporary, path)
	if not has_tcmp(path):
		raise MediaError("Failed to verify the Apple-compatible TCMP compilation frame.")


def probe_duration_ms(path: Path) -> int:
	result = _run([
		_tool("ffprobe"),
		"-v", "error",
		"-show_entries", "format=duration",
		"-of", "json",
		str(path),
	], timeout=60)
	try:
		return int(round(float(json.loads(result.stdout)["format"]["duration"]) * 1000))
	except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
		raise MediaError("ffprobe could not verify the MP3 duration.") from exc


def audio_sha256(path: Path, *, transcode_to_managed_mp3: bool = False) -> str:
	"""Hash encoded audio packets without including mutable tags or artwork."""
	codec_arguments = ["-c:a", "libmp3lame", "-q:a", "0", "-ar", "44100", "-ac", "2"] if transcode_to_managed_mp3 else ["-c:a", "copy"]
	result = _run([
		_tool("ffmpeg"),
		"-v", "error",
		"-i", str(path),
		"-map", "0:a:0",
		*codec_arguments,
		"-f", "hash",
		"-hash", "sha256",
		"-",
	], timeout=600)
	value = str(result.stdout or "").strip()
	if not value.startswith("SHA256=") or len(value) != len("SHA256=") + 64:
		raise MediaError("ffmpeg could not verify the managed audio hash.")
	return value.split("=", 1)[1].casefold()


def _retag_audio_is_unchanged(
	recording: dict[str, Any],
	paths: ManagedPaths,
	target: Path,
	*,
	on_output: Callable[[str], None] | None = None,
) -> bool:
	managed = recording.get("managed_file") or {}
	current_audio_sha = audio_sha256(target)
	expected_audio_sha = str(managed.get("audio_sha256") or "").casefold()
	if expected_audio_sha:
		return current_audio_sha == expected_audio_sha
	video_id = str((recording.get("youtube") or {}).get("video_id") or "")
	source = _source_audio(paths.staging / str(recording.get("recording_id") or ""), video_id)
	if source is None or audio_sha256(source, transcode_to_managed_mp3=True) != current_audio_sha:
		return False
	managed["audio_sha256"] = current_audio_sha
	if on_output:
		on_output("Verified the registered legacy MP3 against its saved source; only tags or artwork changed.")
	return True


def _verify_retag_integrity(
	recording: dict[str, Any],
	paths: ManagedPaths,
	target: Path,
	*,
	on_output: Callable[[str], None] | None = None,
) -> None:
	managed = recording.get("managed_file") or {}
	expected_sha = str(managed.get("sha256") or "")
	if not expected_sha or file_sha256(target) == expected_sha:
		return
	if _retag_audio_is_unchanged(recording, paths, target, on_output=on_output):
		return
	raise MediaError(f"Managed file hash changed; refusing to retag it: {target}")


def _duration_matches(actual_ms: int, expected_ms: int, *, ratio: float, minimum_tolerance_ms: int = 500) -> bool:
	if actual_ms <= 0 or expected_ms <= 0:
		return False
	tolerance_ms = max(minimum_tolerance_ms, int(round(expected_ms * ratio)))
	return abs(actual_ms - expected_ms) <= tolerance_ms


def _validate_transcode_duration(actual_ms: int, source_ms: int) -> None:
	if not _duration_matches(actual_ms, source_ms, ratio=0.01):
		raise MediaError(
			f"Generated MP3 duration {actual_ms / 1000:.2f}s does not match the downloaded source "
			f"duration {source_ms / 1000:.2f}s. The partial MP3 was not accepted."
		)


def managed_duration_is_valid(recording: dict[str, Any], paths: ManagedPaths) -> bool:
	managed = recording.get("managed_file") or {}
	relative_path = str(managed.get("relative_path") or "")
	if not relative_path:
		return False
	existing = paths.root / relative_path
	if not existing.is_file():
		return False
	try:
		existing_duration_ms = probe_duration_ms(existing)
	except MediaError:
		return False
	expected_duration_ms = int(
		managed.get("source_duration_ms")
		or recording.get("source_metadata", {}).get("duration_ms")
		or 0
	)
	has_source_duration = bool(managed.get("source_duration_ms"))
	return _duration_matches(
		existing_duration_ms,
		expected_duration_ms,
		ratio=0.01 if has_source_duration else 0.05,
		minimum_tolerance_ms=500 if has_source_duration else 5000,
	)


def available_managed_relative_path(recording: dict[str, Any], paths: ManagedPaths) -> str:
	"""Choose the readable canonical path without overwriting another recording."""
	managed = recording.get("managed_file") or {}
	relative = managed_relative_path(recording)
	requested = paths.root / relative
	if not requested.exists() or str(managed.get("relative_path") or "") == relative:
		return relative
	stable_suffix = str(recording.get("recording_id") or "recording").removeprefix("rec_")[:10]
	relative = managed_relative_path(recording, suffix=stable_suffix)
	counter = 1
	while (paths.root / relative).exists() and str(managed.get("relative_path") or "") != relative:
		counter += 1
		relative = managed_relative_path(recording, suffix=f"{stable_suffix}-{counter}")
	return relative


def recover_moved_managed_file(
	recording: dict[str, Any],
	candidate: dict[str, Any],
	paths: ManagedPaths,
) -> str | None:
	"""Recover a Crate-managed MP3 after Music organized it inside the managed root."""
	managed = recording.get("managed_file") or {}
	persistent_id = str((recording.get("music_binding") or {}).get("persistent_id") or "")
	if (
		not managed.get("managed_by_crate")
		or not managed.get("sha256")
		or str(candidate.get("persistent_id") or "") != persistent_id
	):
		return None
	location = str(candidate.get("location") or "")
	if not location:
		return None
	try:
		target = Path(location).resolve(strict=True)
		relative = target.relative_to(paths.root.resolve())
	except (OSError, ValueError):
		return None
	if not target.is_file() or file_sha256(target) != str(managed["sha256"]):
		return None
	managed["relative_path"] = str(relative)
	recording["managed_file"] = managed
	return str(relative)


def _metadata_arguments(recording: dict[str, Any]) -> list[str]:
	meta = recording["source_metadata"]
	album = recording.get("album_metadata") or {}
	arguments = [
		"-metadata", f"title={meta.get('title', '')}",
		"-metadata", f"artist={meta.get('artists', '')}",
		"-metadata", f"genre={IMPORT_GENRE}",
	]
	if album:
		track_no = int(album.get("track_no") or 0)
		track_total = int(album.get("track_total") or 0)
		disc_no = int(album.get("disc_no") or 1)
		disc_total = int(album.get("disc_total") or 1)
		arguments += [
			"-metadata", f"album={album.get('album', '')}",
			"-metadata", f"album_artist={album.get('album_artist', '')}",
			"-metadata", f"track={track_no}/{track_total}" if track_total else f"track={track_no}",
			"-metadata", f"disc={disc_no}/{disc_total}" if disc_total else f"disc={disc_no}",
		]
		if album.get("release_year"):
			arguments += ["-metadata", f"date={album['release_year']}"]
		if album.get("is_compilation"):
			arguments += ["-metadata", "compilation=1"]
	else:
		arguments += [
			"-metadata", f"album={IMPORT_ALBUM}",
			"-metadata", f"album_artist={IMPORT_ALBUM_ARTIST}",
			"-metadata", "compilation=1",
		]
	arguments += ["-metadata", f"comment=Managed by Crate Music Importer; recording_id={recording['recording_id']}"]
	return arguments


def download_recording(
	recording: dict[str, Any],
	paths: ManagedPaths,
	*,
	on_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
	managed = recording.get("managed_file") or {}
	if managed.get("relative_path"):
		existing = paths.root / managed["relative_path"]
		if existing.is_file():
			if managed.get("sha256") and file_sha256(existing) != managed["sha256"]:
				raise MediaError(f"Managed file hash changed; refusing to overwrite it: {existing}")
			if managed_duration_is_valid(recording, paths):
				return managed
			if on_output:
				on_output(
					"Existing managed MP3 has an invalid duration; rebuilding it from the resumable source."
				)
	youtube = recording.get("youtube") or {}
	video_id = str(youtube.get("video_id") or "")
	if not youtube.get("url") or not re_full_video_id(video_id):
		raise MediaError("No reviewed YouTube source is available for this recording.")
	paths.create()
	staging = paths.staging / recording["recording_id"]
	staging.mkdir(parents=True, exist_ok=True)
	source = _source_audio(staging, video_id)
	if source is None:
		try:
			_run([
				ytdlp_path(),
				"--no-playlist",
				"--continue",
				"--retries", "5",
				"--fragment-retries", "5",
				"--newline",
				"-f", "bestaudio/best",
				"--write-thumbnail",
				"--convert-thumbnails", "jpg",
				"-o", str(staging / "source--%(id)s.%(ext)s"),
				str(youtube["url"]),
			], on_output=on_output)
		except YouTubeError as exc:
			raise MediaError(str(exc)) from exc
		source = _source_audio(staging, video_id)
	if source is None:
		raise MediaError("yt-dlp finished but no source audio was found.")
	source_duration_ms = probe_duration_ms(source)
	cover = _source_cover(staging, recording.get("source_metadata", {}).get("cover_url"), video_id)
	if cover is None:
		raise MediaError("No usable artwork was available; refusing to create an artwork-free iPod import.")
	relative = available_managed_relative_path(recording, paths)
	target = paths.root / relative
	target.parent.mkdir(parents=True, exist_ok=True)
	if target.exists() and not managed.get("relative_path"):
		raise MediaError(f"Refusing to overwrite an unregistered file: {target}")
	temporary = target.with_suffix(".partial.mp3")
	ffmpeg = _tool("ffmpeg")
	_run([
		ffmpeg,
		"-y",
		"-hide_banner",
		"-loglevel", "error",
		"-i", str(source),
		"-i", str(cover),
		"-map_metadata", "-1",
		"-filter_complex", "[1:v:0]crop='min(iw,ih)':'min(iw,ih)',scale=600:600[cover]",
		"-map", "0:a:0",
		"-map", "[cover]",
		"-c:a", "libmp3lame",
		"-q:a", "0",
		"-ar", "44100",
		"-ac", "2",
		"-c:v", "mjpeg",
		"-disposition:v:0", "attached_pic",
		"-metadata:s:v", "title=Album cover",
		"-metadata:s:v", "comment=Cover (front)",
		"-id3v2_version", "3",
		"-write_id3v1", "1",
		*_metadata_arguments(recording),
		str(temporary),
	], on_output=on_output)
	album = recording.get("album_metadata") or {}
	if not album or album.get("is_compilation"):
		ensure_tcmp(temporary)
	elif has_tcmp(temporary):
		temporary.unlink(missing_ok=True)
		raise MediaError("A non-compilation album unexpectedly retained the TCMP compilation frame.")
	duration_ms = probe_duration_ms(temporary)
	try:
		_validate_transcode_duration(duration_ms, source_duration_ms)
	except MediaError:
		temporary.unlink(missing_ok=True)
		raise
	sha256 = file_sha256(temporary)
	os.replace(temporary, target)
	return {
		"relative_path": relative,
		"sha256": sha256,
		"audio_sha256": audio_sha256(target),
		"duration_ms": duration_ms,
		"source_duration_ms": source_duration_ms,
		"size_bytes": target.stat().st_size,
		"id3_version": "2.3",
		"compilation_frame": "TCMP=1" if not album or album.get("is_compilation") else None,
		"artwork": "600x600 baseline JPEG",
		"metadata_profile": "album" if album else "playlist",
		"spotify_album_id": album.get("spotify_album_id") if album else None,
		"artwork_source_url": album.get("cover_url") if album else recording.get("source_metadata", {}).get("cover_url"),
		"managed_by_crate": True,
	}


def retag_managed_recording_artwork(
	recording: dict[str, Any],
	paths: ManagedPaths,
	*,
	on_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
	if recording.get("album_metadata"):
		raise MediaError("Use the album metadata workflow to update artwork for a real album recording.")
	managed = dict(recording.get("managed_file") or {})
	if not managed.get("managed_by_crate") or not managed.get("relative_path") or managed.get("metadata_profile") not in (None, "playlist"):
		raise MediaError("Only a Crate-managed Playlist Imports MP3 can receive repaired artwork.")
	target = paths.root / str(managed["relative_path"])
	if not target.is_file():
		raise MediaError(f"Managed MP3 is missing: {target}")
	_verify_retag_integrity(recording, paths, target, on_output=on_output)
	cover_url = str(recording.get("source_metadata", {}).get("cover_url") or "")
	if not cover_url:
		raise MediaError("Spotify album artwork is missing; the existing playlist copy was left unchanged.")
	if managed.get("artwork_source_url") == cover_url:
		return managed
	paths.create()
	staging = paths.staging / recording["recording_id"]
	staging.mkdir(parents=True, exist_ok=True)
	video_id = str((recording.get("youtube") or {}).get("video_id") or "")
	cover = _source_cover(staging, cover_url, video_id)
	if cover is None:
		raise MediaError("Spotify album artwork could not be downloaded; the existing playlist copy was left unchanged.")
	if on_output:
		on_output(f"Repairing album artwork: {recording.get('source_metadata', {}).get('title', '')}")
	source_duration_ms = probe_duration_ms(target)
	temporary = target.with_suffix(".artwork-retag.partial.mp3")
	_run([
		_tool("ffmpeg"),
		"-y",
		"-hide_banner",
		"-loglevel", "error",
		"-i", str(target),
		"-i", str(cover),
		"-map_metadata", "-1",
		"-filter_complex", "[1:v:0]crop='min(iw,ih)':'min(iw,ih)',scale=600:600[cover]",
		"-map", "0:a:0",
		"-map", "[cover]",
		"-c:a", "copy",
		"-c:v", "mjpeg",
		"-disposition:v:0", "attached_pic",
		"-metadata:s:v", "title=Album cover",
		"-metadata:s:v", "comment=Cover (front)",
		"-id3v2_version", "3",
		"-write_id3v1", "1",
		*_metadata_arguments(recording),
		str(temporary),
	], on_output=on_output)
	ensure_tcmp(temporary)
	duration_ms = probe_duration_ms(temporary)
	try:
		_validate_transcode_duration(duration_ms, source_duration_ms)
	except MediaError:
		temporary.unlink(missing_ok=True)
		raise
	os.replace(temporary, target)
	managed.update({
		"sha256": file_sha256(target),
		"audio_sha256": audio_sha256(target),
		"duration_ms": duration_ms,
		"size_bytes": target.stat().st_size,
		"id3_version": "2.3",
		"compilation_frame": "TCMP=1",
		"artwork": "600x600 baseline JPEG",
		"metadata_profile": "playlist",
		"artwork_source_url": cover_url,
	})
	return managed


def retag_managed_recording_as_album(
	recording: dict[str, Any],
	paths: ManagedPaths,
	*,
	on_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
	album = recording.get("album_metadata") or {}
	if not album:
		raise MediaError("Album metadata is missing; refusing to retag the managed recording.")
	managed = dict(recording.get("managed_file") or {})
	if not managed.get("managed_by_crate") or not managed.get("relative_path"):
		raise MediaError("Only a Crate-managed MP3 can be upgraded to album metadata.")
	target = paths.root / str(managed["relative_path"])
	if not target.is_file():
		raise MediaError(f"Managed MP3 is missing: {target}")
	_verify_retag_integrity(recording, paths, target, on_output=on_output)
	if managed.get("metadata_profile") == "album" and managed.get("spotify_album_id") == album.get("spotify_album_id"):
		return managed
	paths.create()
	staging = paths.staging / recording["recording_id"]
	staging.mkdir(parents=True, exist_ok=True)
	video_id = str((recording.get("youtube") or {}).get("video_id") or "")
	cover = _source_cover(staging, album.get("cover_url"), video_id)
	if cover is None:
		raise MediaError("No usable Spotify album artwork was available; the existing playlist copy was left unchanged.")
	if on_output:
		on_output(f"Upgrading Crate-managed MP3 to album metadata: {album.get('album', '')}")
	source_duration_ms = probe_duration_ms(target)
	temporary = target.with_suffix(".album-retag.partial.mp3")
	_run([
		_tool("ffmpeg"),
		"-y",
		"-hide_banner",
		"-loglevel", "error",
		"-i", str(target),
		"-i", str(cover),
		"-map_metadata", "-1",
		"-filter_complex", "[1:v:0]crop='min(iw,ih)':'min(iw,ih)',scale=600:600[cover]",
		"-map", "0:a:0",
		"-map", "[cover]",
		"-c:a", "copy",
		"-c:v", "mjpeg",
		"-disposition:v:0", "attached_pic",
		"-metadata:s:v", "title=Album cover",
		"-metadata:s:v", "comment=Cover (front)",
		"-id3v2_version", "3",
		"-write_id3v1", "1",
		*_metadata_arguments(recording),
		str(temporary),
	], on_output=on_output)
	if album.get("is_compilation"):
		ensure_tcmp(temporary)
	elif has_tcmp(temporary):
		temporary.unlink(missing_ok=True)
		raise MediaError("A non-compilation album unexpectedly retained the TCMP compilation frame.")
	duration_ms = probe_duration_ms(temporary)
	try:
		_validate_transcode_duration(duration_ms, source_duration_ms)
	except MediaError:
		temporary.unlink(missing_ok=True)
		raise
	os.replace(temporary, target)
	managed.update({
		"sha256": file_sha256(target),
		"audio_sha256": audio_sha256(target),
		"duration_ms": duration_ms,
		"size_bytes": target.stat().st_size,
		"id3_version": "2.3",
		"compilation_frame": "TCMP=1" if album.get("is_compilation") else None,
		"artwork": "600x600 baseline JPEG",
		"metadata_profile": "album",
		"spotify_album_id": album.get("spotify_album_id"),
		"artwork_source_url": album.get("cover_url"),
	})
	return managed


def extract_embedded_artwork(path: Path, target: Path) -> Path:
	if not path.is_file():
		raise MediaError(f"Managed MP3 is missing: {path}")
	target.parent.mkdir(parents=True, exist_ok=True)
	_run([
		_tool("ffmpeg"),
		"-y",
		"-hide_banner",
		"-loglevel", "error",
		"-i", str(path),
		"-map", "0:v:0",
		"-frames:v", "1",
		str(target),
	], timeout=60)
	if not target.is_file():
		raise MediaError("Could not extract the embedded album artwork for Music.app.")
	return target


def re_full_video_id(value: str) -> bool:
	return bool(value and len(value) <= 64 and all(char.isalnum() or char in "_-" for char in value))
