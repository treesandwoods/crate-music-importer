"""Raycast argument routing plus durable native-extension job APIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from crate_music_importer.ipod_import import cli
from crate_music_importer.ipod_import.constants import MANAGED_ROOT
from crate_music_importer.ipod_import.jobs import JobStore, acknowledge_notification, cancel_incomplete, cancel_source_progress, enqueue, jobs_snapshot, public_job, retry_job, run_queue
from crate_music_importer.ipod_import.music import MusicAutomationError
from crate_music_importer.ipod_import.spotify import SpotifyError, parse_source_url


class RaycastInputError(ValueError):
	pass


def _resolve_source_action(action: str, url: str | None) -> str:
	if action not in ("source_preview", "source_combined"):
		return action
	if not url:
		raise RaycastInputError("A public Spotify playlist or album URL is required.")
	try:
		source_type, _, _ = parse_source_url(url)
	except SpotifyError as exc:
		raise RaycastInputError(str(exc)) from exc
	suffix = "preview" if action == "source_preview" else "combined"
	return f"{source_type}_{suffix}"


def read_clipboard() -> str:
	try:
		result = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, timeout=5, check=False)
	except (OSError, subprocess.TimeoutExpired):
		return ""
	stdout = result.stdout or b""
	text = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else str(stdout)
	return text.strip().replace("\r", "").replace("\n", "")


def _clipboard_when_needed(argument: str, clipboard_reader: Callable[[], str]) -> str:
	return "" if str(argument or "").strip() else clipboard_reader()


def validated_url(
	argument: str,
	clipboard: str,
	*,
	allow_all: bool = False,
	expected_type: str | None = "playlist",
) -> tuple[str | None, str]:
	argument = str(argument or "").strip()
	clipboard = str(clipboard or "").strip()
	if argument:
		try:
			_, _, canonical = parse_source_url(argument, expected_type=expected_type)
		except SpotifyError as exc:
			raise RaycastInputError(str(exc)) from exc
		return canonical, "argument"
	if clipboard:
		try:
			_, _, canonical = parse_source_url(clipboard, expected_type=expected_type)
		except SpotifyError:
			if allow_all:
				return None, "all"
			label = expected_type or "playlist or album"
			raise RaycastInputError(f"The clipboard does not contain a valid public Spotify {label} URL.")
		return canonical, "clipboard"
	if allow_all:
		return None, "all"
	label = expected_type or "playlist or album"
	raise RaycastInputError(f"Paste or copy a public Spotify {label} URL.")


def build_engine_commands(
	action: str,
	url: str | None,
	*,
	recording_id: str = "",
	youtube_url: str = "",
	spotify_fixture: str | None = None,
	music_fixture: str | None = None,
) -> list[list[str]]:
	action = _resolve_source_action(action, url)
	if action == "playlist_preview":
		command = ["preview", str(url or ""), "--json"]
		if spotify_fixture:
			command += ["--spotify-fixture", spotify_fixture]
		if music_fixture:
			command += ["--music-fixture", music_fixture]
		return [command]
	if action == "album_preview":
		command = ["album-preview", str(url or ""), "--json"]
		if spotify_fixture:
			command += ["--spotify-fixture", spotify_fixture]
		if music_fixture:
			command += ["--music-fixture", music_fixture]
		return [command]
	if action == "playlist_combined":
		return [["import", str(url or ""), "--confirm-download"], ["apply", str(url or ""), "--confirm-music-write"]]
	if action == "album_combined":
		return [["album-import", str(url or ""), "--confirm-download"], ["album-apply", str(url or ""), "--confirm-music-write"]]
	if action == "review_problems":
		recording_id = recording_id.strip()
		youtube_url = youtube_url.strip()
		if bool(recording_id) != bool(youtube_url):
			raise RaycastInputError("Provide both a recording ID and a chosen YouTube URL.")
		commands: list[list[str]] = []
		if recording_id and youtube_url:
			commands.append(["resolve", recording_id, youtube_url])
		commands.append(["review", str(url), "--json"] if url else ["review", "--json"])
		return commands
	raise RaycastInputError(f"Unknown Raycast action: {action}")


def run(
	action: str,
	arguments: list[str],
	*,
	clipboard_reader: Callable[[], str] = read_clipboard,
	engine: Callable[[list[str]], int] = cli.run,
) -> int:
	url_argument = arguments[0] if arguments else ""
	recording_id = arguments[1] if len(arguments) > 1 else ""
	youtube_url = arguments[2] if len(arguments) > 2 else ""
	if action in ("album_preview", "album_combined"):
		expected_type = "album"
	elif action in ("playlist_preview", "playlist_combined"):
		expected_type = "playlist"
	else:
		expected_type = None
	url, _ = validated_url(
		url_argument,
		_clipboard_when_needed(url_argument, clipboard_reader),
		allow_all=action == "review_problems",
		expected_type=expected_type,
	)
	commands = build_engine_commands(
		action,
		url,
		recording_id=recording_id,
		youtube_url=youtube_url,
		spotify_fixture=os.environ.get("CRATE_MUSIC_IMPORTER_SPOTIFY_FIXTURE") if action.endswith("preview") else None,
		music_fixture=os.environ.get("CRATE_MUSIC_IMPORTER_MUSIC_FIXTURE") if action.endswith("preview") else None,
	)
	result = 0
	for command in commands:
		result = engine(command)
		if result:
			return result
	return result


def _run_safely(action: str, arguments: list[str]) -> int:
	try:
		return run(action, arguments)
	except RaycastInputError as exc:
		print(f"INPUT ERROR: {exc}", file=sys.stderr)
		return 2
	except MusicAutomationError as exc:
		print(f"MUSIC ERROR: {exc}", file=sys.stderr)
		return 2
	except PermissionError as exc:
		path = Path(exc.filename) if exc.filename else None
		print(f"FILESYSTEM PERMISSION ERROR: Raycast could not access {path or 'the managed music folder'}.", file=sys.stderr)
		return 2
	except KeyboardInterrupt:
		print("Interrupted. Saved checkpoints remain resumable.", file=sys.stderr)
		return 130
	except Exception as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		return 2


def queue_background(
	action: str,
	arguments: list[str],
	*,
	clipboard_reader: Callable[[], str] = read_clipboard,
	popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> tuple[int | None, Path]:
	"""Compatibility wrapper for the non-indexed legacy shims."""
	url_argument = arguments[0] if arguments else ""
	expected_type = None if action == "source_combined" else ("album" if action == "album_combined" else "playlist")
	url, _ = validated_url(url_argument, _clipboard_when_needed(url_argument, clipboard_reader), expected_type=expected_type)
	assert url is not None
	try:
		job = enqueue(action, url, root=MANAGED_ROOT, popen=popen)
	except ValueError as exc:
		raise RaycastInputError(str(exc)) from exc
	log_path = Path(str(job.get("logPath") or (MANAGED_ROOT / ".state" / "logs" / f"pending-{job['jobId']}.log")))
	return job.get("runnerPid"), log_path


def _json(value: object) -> None:
	print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
	values = list(argv if argv is not None else sys.argv[1:])
	if not values:
		print("ERROR: Missing Raycast action.", file=sys.stderr)
		raise SystemExit(2)
	action = values.pop(0)
	try:
		if action in ("queue", "queue-json"):
			if not values:
				raise RaycastInputError("Missing background action.")
			background_action = values.pop(0)
			url_argument = values[0] if values else ""
			seed: dict | None = None
			if len(values) > 1 and values[1]:
				try:
					parsed_seed = json.loads(values[1])
				except json.JSONDecodeError as exc:
					raise RaycastInputError("Invalid queued source metadata.") from exc
				if not isinstance(parsed_seed, dict):
					raise RaycastInputError("Invalid queued source metadata.")
				seed = parsed_seed
			expected_type = None if background_action == "source_combined" else ("album" if background_action == "album_combined" else "playlist")
			url, _ = validated_url(url_argument, _clipboard_when_needed(url_argument, read_clipboard), expected_type=expected_type)
			assert url is not None
			job = enqueue(background_action, url, seed=seed)
			_json(public_job(job)) if action == "queue-json" else print(f"Queued {job['source']['type']} import {job['jobId']}.")
			raise SystemExit(0)
		if action == "jobs-json":
			_json(jobs_snapshot())
			raise SystemExit(0)
		if action == "job-json":
			if not values:
				raise RaycastInputError("Missing job ID.")
			_json(public_job(JobStore().load(values[0])))
			raise SystemExit(0)
		if action == "cancel-job":
			if not values:
				raise RaycastInputError("Missing job ID.")
			_json(public_job(cancel_incomplete(values[0])))
			raise SystemExit(0)
		if action == "cancel-source":
			if len(values) < 2:
				raise RaycastInputError("Missing source type or ID.")
			_json(cancel_source_progress(values[0], values[1]))
			raise SystemExit(0)
		if action == "retry-job":
			if not values:
				raise RaycastInputError("Missing job ID.")
			_json(public_job(retry_job(values[0])))
			raise SystemExit(0)
		if action == "ack-notification":
			if not values:
				raise RaycastInputError("Missing job ID.")
			_json(public_job(acknowledge_notification(values[0])))
			raise SystemExit(0)
		if action == "__runner":
			run_queue()
			raise SystemExit(0)
	except (RaycastInputError, MusicAutomationError, ValueError, PermissionError, OSError) as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		raise SystemExit(2)
	raise SystemExit(_run_safely(action, values))


if __name__ == "__main__":
	main()
