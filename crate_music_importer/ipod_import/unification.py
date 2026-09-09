"""Read-only canonical plans and fail-closed, resumable organization transactions."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from crate_music_importer.ipod_import.catalog import atomic_json, library_id, plan_paths, register_tracks, path_key
from crate_music_importer.ipod_import.manifest import ManagedPaths, file_sha256, load_manifest, update_manifest, write_playlist_m3u8
from crate_music_importer.ipod_import.media import audio_sha256, probe_duration_ms, _tool
from crate_music_importer.ipod_import.health import _decode, _tags_and_artwork
from crate_music_importer.ipod_import.preservation import digest, assert_preserved
from crate_music_importer.ipod_import.working_files import review_working_files


def fingerprints(path: Path) -> dict[str, Any]:
	before = path.stat()
	if path.is_symlink():
		raise ValueError("Symbolic links require manual review.")
	result = subprocess.run([_tool("ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0", "-c:a", "pcm_s32le", "-f", "hash", "-hash", "sha256", "-"], capture_output=True, text=True, timeout=1800, check=True)
	pcm = result.stdout.strip()
	if not pcm.startswith("SHA256=") or len(pcm) != 71 or result.stderr.strip():
		raise ValueError("Decoded audio fingerprint could not be verified.")
	tags, artwork_errors = _tags_and_artwork(path)
	value = {"sha256": file_sha256(path), "audio_sha256": audio_sha256(path), "decoded_sha256": pcm[7:],
		"duration_ms": probe_duration_ms(path), "format": path.suffix.lower().lstrip("."), "size_bytes": before.st_size,
		"artwork_errors": artwork_errors, "tags": tags, "audio_verified": True}
	after = path.stat()
	if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
		raise ValueError("File changed while verifying its integrity.")
	return value


def inventory(root: Path) -> list[str]:
	# Includes staging MP3s: no finished audio can disappear behind a directory label.
	return sorted(str(path) for path in root.rglob("*") if path.suffix.casefold() == ".mp3" and (path.is_file() or path.is_symlink())) if root.exists() else []


def generated_playlists(paths: ManagedPaths, manifest: dict[str, Any]) -> dict[str, str]:
	result = {}
	for playlist in manifest.get("playlists", {}).values():
		if playlist.get("m3u8_tool_owned"):
			path = paths.root / playlist["m3u8_relative_path"]
			result[str(path)] = path.read_text(encoding="utf-8")
	return result


def build_preview(paths: ManagedPaths, manifest: dict[str, Any], baseline: dict[str, Any], *, inspect: Callable = fingerprints, on_progress: Callable | None = None, deferred_orphans: tuple[str, ...] = ()) -> dict[str, Any]:
	if inspect is not fingerprints:
		return _build_preview(paths, manifest, baseline, inspect=inspect, on_progress=on_progress, deferred_orphans=deferred_orphans)
	from concurrent.futures import ThreadPoolExecutor
	locations = list(dict.fromkeys(str(track["location"]) for track in baseline["tracks"] if track.get("location") and Path(track["location"]).suffix.casefold() == ".mp3"))
	with ThreadPoolExecutor(max_workers=4, thread_name_prefix="crate-audio-preview") as pool:
		futures = {location: pool.submit(inspect, Path(location)) for location in locations}
		return _build_preview(paths, manifest, baseline, inspect=lambda path: futures[str(path)].result(), on_progress=on_progress, deferred_orphans=deferred_orphans)


def _build_preview(paths: ManagedPaths, manifest: dict[str, Any], baseline: dict[str, Any], *, inspect: Callable = fingerprints, on_progress: Callable | None = None, deferred_orphans: tuple[str, ...] = ()) -> dict[str, Any]:
	working = copy.deepcopy(manifest)
	tracks = baseline["tracks"]
	blockers = []
	try:
		register_tracks(working, tracks, complete=True)
	except ValueError as exc:
		blockers.append(str(exc))
	if not baseline.get("complete"):
		blockers.append("Complete preservation baseline unavailable.")
	if baseline.get("settings") != {"keep_organized": False, "copy_files": False}:
		blockers.append("Both Music organization settings must be freshly verified off.")
	files = inventory(paths.root)
	locations = {str(track.get("location")) for track in tracks if track.get("location")}
	orphans = sorted(set(files) - locations)
	if not set(deferred_orphans).issubset(orphans):
		raise ValueError("Only existing orphan files can be explicitly deferred.")
	deferred = {name: file_sha256(Path(name)) for name in deferred_orphans}
	unresolved = sorted(set(orphans) - set(deferred))
	if unresolved:
		blockers.append(f"{len(unresolved)} MP3 files have no exact Music identity; review before migration.")
	# Occupied destinations are reserved, except for their exact current owner.
	plan, collisions = plan_paths([track for track in tracks if track.get("persistent_id")], set())
	used = {path_key(str(paths.root / value)) for value in plan.values()}
	for track in tracks:
		pid = track.get("persistent_id")
		if pid not in plan:
			continue
		target = paths.root / plan[pid]
		if path_key(str(target)) in {path_key(p) for p in files} and str(target) != str(track.get("location")):
			from crate_music_importer.ipod_import.catalog import canonical_relative_path
			counter = 0
			while True:
				counter += 1
				candidate = canonical_relative_path(track, suffix=library_id(pid)[4:14] + (f"-{counter}" if counter > 1 else ""))
				key = path_key(str(paths.root / candidate))
				if key not in used and key not in {path_key(p) for p in files}:
					break
			used.add(key)
			collisions.append({"persistent_id": pid, "requested": plan[pid], "resolved": candidate})
			plan[pid] = candidate
	rows = []
	by_hash: dict[str, list[str]] = defaultdict(list)
	by_audio: dict[str, list[str]] = defaultdict(list)
	by_location: dict[str, list[str]] = defaultdict(list)
	for index, track in enumerate(tracks):
		if on_progress:
			on_progress({"checked": index, "total": len(tracks), "phase": "Verifying local audio"})
		pid = str(track.get("persistent_id") or "")
		source = str(track.get("location") or "")
		row = {"persistent_id": pid, "library_id": library_id(pid) if pid else None, "source": source, "target": str(paths.root / plan[pid]) if pid in plan else None, "metadata": track}
		if not source:
			row["status"] = "cloud_only"
		elif Path(source).suffix.casefold() != ".mp3":
			row["status"] = "unsupported"
			blockers.append(f"Unsupported local format: {source}")
		else:
			by_location[path_key(source)].append(pid)
			try:
				row["fingerprints"] = inspect(Path(source))
				row["status"] = "canonical" if source == row["target"] else "awaiting_organization" if Path(source).is_relative_to(paths.root) else "awaiting_adoption"
				by_hash[row["fingerprints"]["sha256"]].append(pid)
				by_audio[row["fingerprints"]["audio_sha256"]].append(pid)
				entry = working["catalog"]["entries"][row["library_id"]]
				entry["fingerprints"] = row["fingerprints"]
				for rid in entry["recording_ids"]:
					recording = manifest["recordings"][rid]
					managed = recording.get("managed_file") or {}
					if managed.get("audio_sha256") and managed["audio_sha256"] != row["fingerprints"]["audio_sha256"]:
						blockers.append(f"Importer audio baseline differs for {pid}; review prerequisite.")
					if managed.get("relative_path") and str(paths.root / managed["relative_path"]) != source:
						blockers.append(f"Importer file location differs from Music for {pid}; reconcile prerequisite.")
				old = manifest.get("catalog", {}).get("entries", {}).get(row["library_id"], {}).get("fingerprints", {})
				if old.get("audio_sha256") and old["audio_sha256"] != row["fingerprints"]["audio_sha256"]:
					blockers.append(f"Audio changed since catalog baseline: {pid}")
			except Exception as exc:
				row.update(status="missing_or_unverified", error=str(exc))
				blockers.append(f"Cannot verify {pid}: {exc}")
		rows.append(row)
	shared = [pids for pids in by_location.values() if len(pids) > 1]
	if shared:
		blockers.append("Multiple Music entries share a source file; resolve before organizing.")
	exact = [pids for pids in by_hash.values() if len(pids) > 1]
	if exact:
		blockers.append("Exact duplicates require review before one-file-per-song acceptance.")
	try:
		m3us = generated_playlists(paths, manifest)
	except OSError as exc:
		m3us = {}
		blockers.append(f"Generated playlist baseline unavailable: {exc}")
	for name, content in m3us.items():
		for line in content.splitlines():
			if line and not line.startswith("#") and line not in locations:
				blockers.append(f"Generated playlist references a file without a current Music identity: {name}")
				break
	semantic: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
	from crate_music_importer.ipod_import.identity import normalized_artists, normalize_recording_title, version_markers
	for row in rows:
		track = row["metadata"]
		if row.get("fingerprints"):
			key = (normalize_recording_title(track.get("title")), normalized_artists(track.get("artist")), version_markers(track.get("title")))
			if key[0] and key[1]:
				semantic[key].append(row)
	possible = []
	for group in semantic.values():
		group.sort(key=lambda row: row["fingerprints"].get("duration_ms", 0))
		clusters: list[list[dict[str, Any]]] = []
		for row in group:
			if not clusters or row["fingerprints"].get("duration_ms", 0) - clusters[-1][0]["fingerprints"].get("duration_ms", 0) > 2000:
				clusters.append([])
			clusters[-1].append(row)
		for cluster in clusters:
			members = {row["persistent_id"]: row["fingerprints"]["sha256"] for row in cluster}
			if len(cluster) > 1 and not any(item.get("files") == members for item in manifest.get("health_preferences", {}).get("distinct_recordings", [])):
				possible.append(list(members))
	staging = review_working_files(paths, manifest)
	from crate_music_importer.ipod_import.reconciliation import MUSIC_LIBRARY_PACKAGE
	backup_bytes = sum(path.stat().st_size for path in MUSIC_LIBRARY_PACKAGE.rglob("*") if path.is_file()) if MUSIC_LIBRARY_PACKAGE.exists() else 0
	if not backup_bytes:
		blockers.append("Configured Music library package is unavailable for recovery.")
	result = {"version": 1, "preview_id": uuid.uuid4().hex, "managed_root": str(paths.root), "baseline": baseline, "manifest_digest": digest(manifest),
		"catalog": working.get("catalog"), "files": files, "tracks": rows, "collisions": collisions, "orphans": orphans, "deferred_orphans": deferred,
		"exact_duplicates": exact, "possible_recording_duplicates": possible, "matching_audio_groups": [pids for pids in by_audio.values() if len(pids) > 1],
		"generated_playlists": m3us, "working_files": staging, "blockers": blockers,
		"temporary_audio_bytes": sum(row.get("fingerprints", {}).get("size_bytes", 0) for row in rows if row["source"] != row["target"]),
		"backup_bytes": backup_bytes, "backup_space_included": True, "ready": not blockers}
	result["digest"] = digest(result)
	return result


def save_preview(paths: ManagedPaths, preview: dict[str, Any]) -> Path:
	target = paths.state_dir / "unification" / (preview["preview_id"] + ".json")
	atomic_json(target, preview)
	atomic_json(paths.state_dir / "unification-last.json", preview)
	return target


def atomic_text(path: Path, content: str) -> None:
	import tempfile
	with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
		handle.write(content)
		handle.flush()
		os.fsync(handle.fileno())
		temporary = Path(handle.name)
	try:
		os.replace(temporary, path)
	finally:
		temporary.unlink(missing_ok=True)


def rewrite_playlists(preview: dict[str, Any]) -> None:
	mapping = {row["source"]: row["target"] for row in preview["tracks"] if row.get("fingerprints")}
	for name, content in preview["generated_playlists"].items():
		path = Path(name)
		updated = "".join(mapping.get(line.rstrip("\r\n"), line.rstrip("\r\n")) + line[len(line.rstrip("\r\n")):] for line in content.splitlines(keepends=True))
		if path.read_text(encoding="utf-8") not in (content, updated):
			raise ValueError("Generated playlist changed since preview; not overwritten.")
		atomic_text(path, updated)


def _verify_file(path: Path, row: dict[str, Any], inspect: Callable, *, deep: bool = False) -> None:
	# Equal complete bytes preserve the previously verified audio exactly. Re-decode
	# new copies explicitly; do not decode every unchanged checkpoint repeatedly.
	if inspect is fingerprints and not deep:
		if path.is_symlink() or path.stat().st_size != row["fingerprints"]["size_bytes"] or file_sha256(path) != row["fingerprints"]["sha256"]:
			raise ValueError(f"File integrity changed: {path}")
		return
	actual = inspect(path)
	for key in ("sha256", "audio_sha256", "decoded_sha256", "size_bytes"):
		if actual.get(key) != row["fingerprints"].get(key):
			raise ValueError(f"File integrity changed: {path} ({key})")


def _copy(source: Path, target: Path, row: dict[str, Any], inspect: Callable, *, token: str, recovery_dir: Path) -> None:
	if target.exists():
		_verify_file(target, row, inspect)
		return
	target.parent.mkdir(parents=True, exist_ok=True)
	if target.parent.resolve() != target.parent:
		raise ValueError("Destination contains a symbolic link.")
	temporary = target.with_name(target.name + ".unification-" + token + ".partial")
	if temporary.exists():
		try:
			_verify_file(temporary, row, inspect, deep=True)
		except Exception:
			_verify_file(source, row, inspect)
			recovery_dir.mkdir(parents=True, exist_ok=True)
			shutil.move(str(temporary), str(recovery_dir / (uuid.uuid4().hex + ".interrupted-partial")))
	if not temporary.exists():
		with source.open("rb") as src, temporary.open("xb") as dst:
			shutil.copyfileobj(src, dst)
			dst.flush()
			os.fsync(dst.fileno())
		shutil.copystat(source, temporary)
		_verify_file(temporary, row, inspect, deep=True)
	# Hard-link publication is atomic and cannot overwrite an intervening file.
	os.link(temporary, target)
	temporary.unlink()


def trash_destination(source: Path, name: str) -> Path:
	"""Use the source volume's Trash so retirement remains one atomic rename."""
	home_trash = Path.home() / ".Trash"
	home_trash.mkdir(exist_ok=True)
	if source.stat().st_dev == home_trash.stat().st_dev:
		return home_trash / name
	mount = source.parent.resolve()
	while mount.parent != mount and mount.parent.stat().st_dev == source.stat().st_dev:
		mount = mount.parent
	trash = mount / ".Trashes" / str(os.getuid())
	trash.mkdir(parents=True, exist_ok=True, mode=0o700)
	return trash / name


def _package_inventory(root: Path) -> dict[str, str]:
	return {str(path.relative_to(root)): file_sha256(path) for path in root.rglob("*") if path.is_file()}


def backup_package(source: Path, target: Path) -> None:
	if not source.is_dir() or target.exists():
		raise ValueError("Music package backup source is unavailable or backup already exists.")
	before = _package_inventory(source)
	if not before:
		raise ValueError("Music library package is empty.")
	temporary = target.with_name(target.name + ".partial")
	shutil.copytree(source, temporary)
	if _package_inventory(source) != before or _package_inventory(temporary) != before:
		raise ValueError("Music library package changed during backup. Retry with Music idle.")
	os.rename(temporary, target)


def _commit_rows(paths: ManagedPaths, rows: list[dict[str, Any]]) -> None:
	def apply(manifest: dict[str, Any]) -> None:
		for row in rows:
			location = row["target"]
			track = {**row["metadata"], "location": location}
			catalog = register_tracks(manifest, [track])
			entry = catalog["entries"][row["library_id"]]
			entry["fingerprints"] = row["fingerprints"]
			entry["authority"]["organize"] = {"verified_music_id": row["persistent_id"], "verified_sha256": row["fingerprints"]["sha256"]}
			for recording in manifest.get("recordings", {}).values():
				if recording.get("library_id") != row["library_id"]:
					continue
				recording.setdefault("music", {}).update(location=location)
				managed = recording.get("managed_file") or {}
				if managed.get("relative_path") and location == row["target"]:
					managed["relative_path"] = str(Path(location).relative_to(paths.root))
					active = recording.get("active_reference") or {}
					if active.get("kind") == "managed_file":
						active["relative_path"] = managed["relative_path"]
	update_manifest(paths, apply)


def migrate(paths: ManagedPaths, preview: dict[str, Any], *, snapshot: Callable, relink: Callable, music_package: Path,
	inspect: Callable = fingerprints, batch_size: int = 10, rollback: bool = False, relink_batch: Callable | None = None) -> dict[str, Any]:
	"""Caller must obtain approval for this exact saved preview. Never empties Trash."""
	from crate_music_importer.ipod_import.dependency_lock import dependency_lock
	with dependency_lock(exclusive=True):
		return _migrate(paths, preview, snapshot=snapshot, relink=relink, music_package=music_package, inspect=inspect, batch_size=batch_size, rollback=rollback, relink_batch=relink_batch)


def _migrate(paths: ManagedPaths, preview: dict[str, Any], *, snapshot: Callable, relink: Callable, music_package: Path,
	inspect: Callable, batch_size: int, rollback: bool, relink_batch: Callable | None = None) -> dict[str, Any]:
	if preview.get("managed_root") != str(paths.root) or digest({k: v for k, v in preview.items() if k != "digest"}) != preview.get("digest"):
		raise ValueError("Invalid preview or managed root.")
	if preview.get("blockers") or not preview.get("ready"):
		raise ValueError("Preview has blocking prerequisites. Run a new preview after resolving them.")
	root = paths.state_dir / "unification" / preview["preview_id"]
	journal_path = root / "journal.json"
	rows = [row for row in preview["tracks"] if row.get("fingerprints")]
	for row in rows:
		target = Path(row["target"])
		if not target.is_absolute() or not target.is_relative_to(paths.root / "Music") or ".." in target.parts:
			raise ValueError("A planned destination escapes the canonical Music folder.")
	journal = json.loads(journal_path.read_text()) if journal_path.exists() else None
	if journal and not journal.get("backup_retained", True) and rollback:
		raise ValueError("Restore the accepted migration backup from Trash before automated rollback.")
	if journal and journal.get("state") == "accepted" and not rollback:
		return journal
	if journal and journal.get("state") == "rolled_back":
		raise ValueError("This migration was rolled back. Create a fresh preview.")
	if journal and journal["preview_digest"] != preview["digest"]:
		raise ValueError("Journal belongs to another preview.")
	actual = snapshot()
	locations = {track["persistent_id"]: track.get("location") for track in actual["tracks"]}
	if journal is None:
		if rollback:
			raise ValueError("There is no migration to roll back.")
		if digest(load_manifest(paths)) != preview["manifest_digest"] or inventory(paths.root) != preview["files"] or generated_playlists(paths, load_manifest(paths)) != preview["generated_playlists"]:
			raise ValueError("Preview is stale. Run a new scan.")
		assert_preserved(preview["baseline"], actual, {})
		for row in rows:
			_verify_file(Path(row["source"]), row, inspect)
		package_size = sum(path.stat().st_size for path in music_package.rglob("*") if path.is_file())
		if shutil.disk_usage(paths.root).free < preview["temporary_audio_bytes"] + package_size + 64 * 1024 * 1024:
			raise ValueError("Insufficient space for audio and recovery backups.")
		root.mkdir(parents=True, exist_ok=True)
		atomic_json(root / "manifest-backup.json", load_manifest(paths))
		atomic_json(root / "preview.json", preview)
		backup_package(music_package, root / "Music Library.musiclibrary")
		assert_preserved(preview["baseline"], snapshot(), {})
		journal = {"version": 1, "preview_digest": preview["digest"], "state": "running", "tracks": {}, "backup_retained": True}
		atomic_json(journal_path, journal)
	# Recover even if an OS relink completed just before its checkpoint was saved.
	for row in rows:
		pid = row["persistent_id"]
		if locations.get(pid) not in (row["source"], row["target"]):
			raise ValueError(f"Music location changed outside migration: {pid}")
		_verify_file(Path(locations[pid]), row, inspect)
	assert_preserved(preview["baseline"], actual, locations)
	if rollback:
		for row in reversed(rows):
			pid = row["persistent_id"]
			source, target = Path(row["source"]), Path(row["target"])
			if locations[pid] != str(source):
				_copy(Path(locations[pid]), source, row, inspect, token=preview["preview_id"], recovery_dir=paths.staging / "unification" / preview["preview_id"])
				relink(pid, source)
				locations[pid] = str(source)
				assert_preserved(preview["baseline"], snapshot.for_tracks([pid]) if hasattr(snapshot, "for_tracks") else snapshot(), locations)
			journal["tracks"].setdefault(pid, {})["rolled_back"] = True
			atomic_json(journal_path, journal)
		assert_preserved(preview["baseline"], snapshot(), {})
		for row in rows:
			target = Path(row["target"])
			if row["source"] != row["target"] and target.exists():
				_verify_file(target, row, inspect)
				trash = Path.home() / ".Trash" / (preview["preview_id"] + "-rollback-" + row["persistent_id"] + ".mp3")
				if trash.exists():
					raise ValueError("Rollback recovery path already exists.")
				trash.parent.mkdir(exist_ok=True)
				os.rename(target, trash)
		# Restore only Crate bookkeeping after the original Music references verify.
		backup = json.loads((root / "manifest-backup.json").read_text())
		update_manifest(paths, lambda value: (value.clear(), value.update(backup)))
		for path, content in preview["generated_playlists"].items():
			atomic_text(Path(path), content)
		journal["state"] = "rolled_back"
		atomic_json(journal_path, journal)
		return journal
	for start in range(0, len(rows), max(1, batch_size)):
		batch = rows[start:start + max(1, batch_size)]
		batch = [row for row in batch if not journal["tracks"].get(row["persistent_id"], {}).get("retired")]
		if not batch:
			continue
		if relink_batch is not None:
			from concurrent.futures import ThreadPoolExecutor
			pending = [row for row in batch if locations[row["persistent_id"]] != row["target"]]
			def prepare(row: dict[str, Any]) -> None:
				_verify_file(Path(row["source"]), row, inspect)
				_copy(Path(row["source"]), Path(row["target"]), row, inspect, token=preview["preview_id"], recovery_dir=paths.staging / "unification" / preview["preview_id"])
			with ThreadPoolExecutor(max_workers=4) as pool:
				list(pool.map(prepare, pending))
			for row in batch:
				journal["tracks"].setdefault(row["persistent_id"], {})["copied"] = True
			atomic_json(journal_path, journal)
			if pending:
				relink_batch({row["persistent_id"]: Path(row["target"]) for row in pending})
			for row in pending:
				locations[row["persistent_id"]] = row["target"]
				journal["tracks"][row["persistent_id"]]["relinked"] = True
			atomic_json(journal_path, journal)
		else:
			for row in batch:
				pid = row["persistent_id"]
				entry = journal["tracks"].setdefault(pid, {})
				if entry.get("retired"):
					continue
				source, target = Path(row["source"]), Path(row["target"])
				if locations[pid] != str(target):
					_verify_file(source, row, inspect)
					_copy(source, target, row, inspect, token=preview["preview_id"], recovery_dir=paths.staging / "unification" / preview["preview_id"])
					entry["copied"] = True
					atomic_json(journal_path, journal)
					relink(pid, target)
					locations[pid] = str(target)
					entry["relinked"] = True
					atomic_json(journal_path, journal)
		batch_snapshot = snapshot.for_tracks([row["persistent_id"] for row in batch]) if hasattr(snapshot, "for_tracks") else snapshot()
		assert_preserved(preview["baseline"], batch_snapshot, locations)
		for row in batch:
			_verify_file(Path(row["target"]), row, inspect)
		_commit_rows(paths, batch)
		for row in batch:
			pid = row["persistent_id"]
			entry = journal["tracks"][pid]
			entry["verified"] = True
			atomic_json(journal_path, journal)
			if row["source"] != row["target"] and not entry.get("retired"):
				source = Path(row["source"])
				# Persist the exact recovery path BEFORE the move to survive interruption.
				trash = Path(entry["trash"]) if entry.get("trash") else trash_destination(source, preview["preview_id"] + "-" + pid + "-" + source.name)
				entry["trash"] = str(trash)
				atomic_json(journal_path, journal)
				if source.exists():
					_verify_file(source, row, inspect)
					if trash.exists():
						raise ValueError("Trash recovery path is already occupied.")
					trash.parent.mkdir(exist_ok=True)
					os.rename(source, trash)
				else:
					_verify_file(trash, row, inspect)
			entry["retired"] = True
			atomic_json(journal_path, journal)
	final_snapshot = snapshot()
	assert_preserved(preview["baseline"], final_snapshot, locations)
	update_manifest(paths, lambda value: register_tracks(value, final_snapshot["tracks"], complete=True))
	rewrite_playlists(preview)
	from crate_music_importer.ipod_import.reconciliation import _verify_generated_playlists
	_verify_generated_playlists(paths, load_manifest(paths))
	assert_preserved(preview["baseline"], final_snapshot, locations)
	deferred = preview.get("deferred_orphans", {})
	for name, expected in deferred.items():
		if file_sha256(Path(name)) != expected:
			raise ValueError("An explicitly deferred orphan changed during migration.")
	wanted = sorted([row["target"] for row in rows] + list(deferred))
	if inventory(paths.root) != wanted:
		raise ValueError("Final orphan check failed; backups and journal retained.")
	for row in rows:
		_verify_file(Path(row["target"]), row, inspect)
	from crate_music_importer.ipod_import.health import save_health_report
	# Already holding the exclusive dependency lock; call the internal read-only
	# implementation to avoid reacquiring it with a separate file descriptor.
	from crate_music_importer.ipod_import.health import _build_health_report
	final_health = _build_health_report(paths, load_manifest(paths), final_snapshot["tracks"], on_progress=None, deep_all=True)
	save_health_report(paths, final_health)
	journal["final_health"] = {"status": final_health["status"], "summary": final_health["summary"]}
	if final_health["status"] == "failed" or final_health["summary"].get("critical"):
		atomic_json(journal_path, journal)
		raise ValueError("Final deep health audit requires attention; backups retained.")
	assert_preserved(preview["baseline"], snapshot(), locations)
	journal["state"] = "verified_awaiting_user_acceptance"
	atomic_json(journal_path, journal)
	return journal


def release_backup(paths: ManagedPaths, preview_id: str) -> dict[str, Any]:
	"""Explicit acceptance action. Recovery journal and original audio Trash survive."""
	import re
	from crate_music_importer.ipod_import.dependency_lock import dependency_lock
	if not re.fullmatch(r"[a-f0-9]{32}", preview_id):
		raise ValueError("Invalid preview ID.")
	with dependency_lock(exclusive=True):
		root = paths.state_dir / "unification" / preview_id
		path = root / "journal.json"
		journal = json.loads(path.read_text())
		if journal.get("state") != "verified_awaiting_user_acceptance":
			raise ValueError("Only a verified migration can be accepted.")
		for name in ("Music Library.musiclibrary", "manifest-backup.json"):
			source = root / name
			if source.exists():
				target = trash_destination(source, "crate-backup-" + preview_id + "-" + name)
				if target.exists():
					raise ValueError("Backup Trash destination already exists.")
				journal.setdefault("backup_trash", {})[name] = str(target)
				atomic_json(path, journal)
				source.rename(target)
		journal.update(backup_retained=False, state="accepted")
		atomic_json(path, journal)
		return journal
