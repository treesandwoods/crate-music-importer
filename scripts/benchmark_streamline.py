"""Repeatable synthetic benchmarks; never calls Music, network, or media tools.

Run from the repository root: PYTHONPATH=. python3 scripts/benchmark_streamline.py
Use --legacy with the pre-change checkout on PYTHONPATH for the same fixtures.
"""

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import import music_cache
from crate_music_importer.ipod_import.jobs import JobStore, _new_job, jobs_snapshot
from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest, new_manifest, save_manifest
from crate_music_importer.ipod_import.resolver import resolver_snapshot


def activity_fixture(paths, size, source_count, job_count):
	manifest = new_manifest(paths)
	for i in range(source_count):
		pid = f"{i:022d}"
		manifest["playlists"][pid] = {"name": f"Playlist {i}", "spotify_url": f"https://open.spotify.com/playlist/{pid}", "items": []}
	for i in range(size):
		pid = f"{i % source_count:022d}"
		key = f"recording-{i}"
		manifest["recordings"][key] = {"recording_id": key, "source_metadata": {"title": f"Song {i}", "artists": "Artist", "duration_ms": 180000}, "music_binding": {"persistent_id": f"PID-{i}"}, "playlist_memberships": {pid: [i + 1]}, "album_memberships": {}}
		manifest["playlists"][pid]["items"].append({"recording_id": key, "position": i + 1})
	save_manifest(paths, manifest)
	store = JobStore(paths.root)
	for i in range(job_count):
		pid = f"{i % source_count:022d}"
		job = _new_job("playlist_combined", f"https://open.spotify.com/playlist/{pid}", {"name": f"Playlist {i % source_count}", "total": size // source_count})
		job.update(jobId=f"fixture-{i:05d}", createdAt=f"2000-01-01T00:00:00.{i:06d}+00:00", status="complete", phase="complete", notification={"pending": False, "notifiedAt": None})
		store.save(job)


def measured(action, repetitions=3, prepare=None):
	times = []
	counts = {}
	result = None
	open_path = Path.open
	for _ in range(repetitions):
		if prepare:
			prepare()
		counts = {"manifest_reads": 0, "job_reads": 0, "cache_reads": 0}
		def opened(path, mode="r", *args, **kwargs):
			if mode == "r":
				if path.name == "manifest.json": counts["manifest_reads"] += 1
				elif path.name == "music-library-cache.json": counts["cache_reads"] += 1
				elif path.parent.name == "jobs" and path.suffix == ".json": counts["job_reads"] += 1
			return open_path(path, mode, *args, **kwargs)
		with patch.object(Path, "open", opened):
			start = time.perf_counter()
			result = action()
			times.append((time.perf_counter() - start) * 1000)
	return {"median_ms": round(statistics.median(times), 3), **counts}, result


def outcome_hash(value):
	def stable(item):
		if isinstance(item, dict):
			return {key: stable(data) for key, data in item.items() if key not in {"managedRoot", "updatedAt", "scanned_at", "managed_root"}}
		if isinstance(item, list):
			return [stable(data) for data in item]
		return item
	return hashlib.sha256(json.dumps(stable(value), sort_keys=True).encode()).hexdigest()


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--legacy", action="store_true")
	args = parser.parse_args()
	output = {"mode": "before" if args.legacy else "after", "activity": [], "cache": []}
	with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CRATE_APPLICATION_STATE": directory}):
		for size, sources, jobs in ((20, 2, 2), (5000, 50, 1000)):
			paths = ManagedPaths(Path(directory) / f"activity-{size}")
			activity_fixture(paths, size, sources, jobs)
			if args.legacy:
				def action():
					load_manifest(paths)  # The existing review CLI dispatch reads before the resolver.
					return {"resolver": resolver_snapshot(paths), "jobs": jobs_snapshot(root=paths.root)}
			else:
				from crate_music_importer.ipod_import.activity import activity_snapshot
				action = lambda: activity_snapshot(root=paths.root)
			metrics, result = measured(action)
			assert len(result["resolver"]["sources"]) == sources and len(result["jobs"]["jobs"]) == jobs
			output["activity"].append({"recordings": size, "sources": sources, "history": jobs, "outcome_sha256": outcome_hash(result), "subprocesses_per_ui_refresh": 2 if args.legacy else 1, **metrics})
			commands = [["crate_music_importer.ipod_import", "review", "--json"], ["crate_music_importer.ipod_import.raycast", "jobs-json"]] if args.legacy else [["crate_music_importer.ipod_import.raycast", "activity-json"]]
			times = []
			for _ in range(3):
				start = time.perf_counter()
				for command in commands:
					child = subprocess.run([sys.executable, "-m", *command], cwd=Path(music_cache.__file__).resolve().parents[2], env={**os.environ, "CRATE_MANAGED_ROOT": str(paths.root)}, capture_output=True, text=True, check=True)
					json.loads(child.stdout)
				times.append((time.perf_counter() - start) * 1000)
			output["activity"][-1]["subprocess_median_ms"] = round(statistics.median(times), 3)
		for size in (20, 5000):
			paths = ManagedPaths(Path(directory) / f"cache-{size}")
			tracks = [{"persistent_id": f"PID-{i}", "title": f"Song {i}", "artist": "Artist", "album": "Album", "duration_s": 180} for i in range(size)]
			music_cache.refresh_music_cache(paths, tracks)
			for changed in (False, True):
				writes = []
				def action():
					# Each repetition starts from identical persisted metadata.
					session = None if args.legacy else music_cache.MusicCacheSession(paths)
					with patch.object(music_cache, "save_music_cache", wraps=music_cache.save_music_cache) as save:
						for i, row in enumerate(tracks[:20]):
							value = dict(row, title=f"Changed {i}") if changed else row
							if session: session.upsert(value)
							else: music_cache.upsert_music_cache_track(paths, value)
						writes.append(save.call_count)
					return music_cache.read_music_cache(paths)
				metrics, result = measured(action, prepare=lambda: music_cache.refresh_music_cache(paths, tracks))
				assert len(result["tracks"]) == size
				metrics["cache_reads"] -= 1  # Exclude the final result verification read.
				output["cache"].append({"tracks": size, "upserts": 20, "changed": changed, "outcome_sha256": outcome_hash(result), "writes": writes[-1], "scans": 0, **metrics})
	print(json.dumps(output, indent=2))


if __name__ == "__main__":
	main()
