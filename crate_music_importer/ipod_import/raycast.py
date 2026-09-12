"""Raycast argument routing plus durable native-extension job APIs."""

from __future__ import annotations

import json
import sys

from crate_music_importer.ipod_import.jobs import acknowledge_notification, cancel_incomplete, cancel_source_progress, enqueue, jobs_snapshot, public_job, retry_job, run_queue
from crate_music_importer.ipod_import.music import MusicAutomationError


class RaycastInputError(ValueError):
	pass


def _json(value: object) -> None:
	print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
	values = list(argv if argv is not None else sys.argv[1:])
	if not values:
		print("ERROR: Missing Raycast action.", file=sys.stderr)
		raise SystemExit(2)
	action = values.pop(0)
	try:
		if action == "queue-json":
			if not values:
				raise RaycastInputError("Missing background action.")
			background_action = values.pop(0)
			url_argument = values[0] if values else ""
			if not url_argument:
				raise RaycastInputError("Missing Spotify source URL.")
			seed: dict | None = None
			if len(values) > 1 and values[1]:
				try:
					parsed_seed = json.loads(values[1])
				except json.JSONDecodeError as exc:
					raise RaycastInputError("Invalid queued source metadata.") from exc
				if not isinstance(parsed_seed, dict):
					raise RaycastInputError("Invalid queued source metadata.")
				seed = parsed_seed
			job = enqueue(background_action, url_argument, seed=seed)
			_json(public_job(job))
			raise SystemExit(0)
		if action == "jobs-json":
			_json(jobs_snapshot())
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
	print(f"ERROR: Unknown Raycast action: {action}", file=sys.stderr)
	raise SystemExit(2)


if __name__ == "__main__":
	main()
