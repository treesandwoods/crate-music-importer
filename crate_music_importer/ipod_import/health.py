"""Read-only, best-effort library diagnostics; never reuse the migration audit."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from crate_music_importer.ipod_import.constants import MUSIC_CONFIDENCE_MIN
from crate_music_importer.ipod_import.dependency_lock import dependency_lock
from crate_music_importer.ipod_import.identity import normalize_text, normalized_artists, normalize_recording_title, version_markers, score_music_candidate
from crate_music_importer.ipod_import.manifest import ManagedPaths, file_sha256
from crate_music_importer.ipod_import.media import MediaError, _duration_matches, _tool, probe_duration_ms
from crate_music_importer.ipod_import.music_cache import load_music_cache


CHECKS = ("musicScan", "fileIntegrity", "duplicates", "manifestConsistency", "metadata", "artwork")


def failed_health_report(error: str) -> dict[str, Any]:
	return {
		"schemaVersion": 1, "checkedAt": datetime.now(timezone.utc).isoformat(),
		"status": "failed", "error": error, "summary": {},
		"checks": {name: "failed" if name == "musicScan" else "not_checked" for name in CHECKS}, "issues": [],
	}


def save_health_report(paths: ManagedPaths, report: dict[str, Any]) -> Path:
	"""Only the latest diagnostic is replaced; even failed writes preserve the prior report."""
	target = paths.state_dir / "health-last-result.json"
	target.parent.mkdir(parents=True, exist_ok=True)
	temporary = None
	try:
		with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, prefix="health-", suffix=".tmp", delete=False) as handle:
			temporary = handle.name
			json.dump(report, handle, ensure_ascii=False, indent=2)
			handle.write("\n")
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary, target)
		temporary = None
	finally:
		if temporary:
			Path(temporary).unlink(missing_ok=True)
	return target


def _resolved(value: str | Path) -> str:
	try:
		return str(Path(value).resolve())
	except (OSError, RuntimeError):
		# Keep inaccessible paths in the report; the file phase records the failure.
		return os.path.abspath(value)


def _inside(path: Path, root: Path) -> bool:
	try:
		return path.resolve().is_relative_to(root.resolve())
	except (OSError, RuntimeError):
		return False


def _decode(path: Path) -> None:
	result = subprocess.run([_tool("ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0", "-f", "null", "-"], capture_output=True, timeout=1800)
	if result.returncode or result.stderr.strip():
		raise ValueError(result.stderr.decode("utf-8", errors="replace")[:2000] or "Audio stream could not be decoded.")


def _tags_and_artwork(path: Path) -> tuple[dict[str, Any], list[str]]:
	from mutagen.id3 import ID3, ID3NoHeaderError
	try:
		tags = ID3(path)
	except ID3NoHeaderError:
		return {}, ["Embedded artwork is missing."]
	def value(key: str) -> str:
		frame = tags.get(key)
		return str(frame.text[0]) if frame and getattr(frame, "text", None) else ""
	values = {field: value(key) for field, key in {"title": "TIT2", "artist": "TPE1", "album": "TALB", "track": "TRCK", "disc": "TPOS", "compilation": "TCMP"}.items()}
	art = tags.getall("APIC")
	problems = [] if art else ["Embedded artwork is missing."]
	for frame in art:
		result = subprocess.run([_tool("ffmpeg"), "-v", "error", "-xerror", "-i", "pipe:0", "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"], input=frame.data, capture_output=True, timeout=60)
		if result.returncode or result.stderr.strip():
			problems.append("Embedded artwork data could not be parsed: " + result.stderr.decode("utf-8", errors="replace")[:500])
	return values, problems


def build_health_report(
	paths: ManagedPaths, manifest: dict[str, Any], music_tracks: list[dict[str, Any]], *,
	on_progress: Callable[[dict[str, Any]], None] | None = None, deep_all: bool = False,
) -> dict[str, Any]:
	with dependency_lock():
		return _build_health_report(paths, manifest, music_tracks, on_progress=on_progress, deep_all=deep_all)


def _build_health_report(
	paths: ManagedPaths, manifest: dict[str, Any], music_tracks: list[dict[str, Any]], *,
	on_progress: Callable[[dict[str, Any]], None] | None, deep_all: bool,
) -> dict[str, Any]:
	report = failed_health_report("")
	report.pop("error")
	report["checks"] = {name: "passed" for name in CHECKS}
	report["deepCheckAll"] = deep_all
	issues = report["issues"]
	recordings = manifest.get("recordings") or {}
	by_pid: dict[str, list[dict[str, Any]]] = defaultdict(list)
	refs: dict[str, set[str]] = defaultdict(set)
	registered: dict[str, set[str]] = defaultdict(set)
	for rid, recording in recordings.items():
		for reference in (recording.get("music") or {}, recording.get("active_reference") or {}):
			if reference.get("persistent_id"):
				refs[str(reference["persistent_id"])].add(rid)
		relative = (recording.get("managed_file") or {}).get("relative_path")
		if relative:
			registered[_resolved(paths.root / relative)].add(rid)
	entries = []
	for track in music_tracks:
		pid = str(track.get("persistent_id") or "")
		location = str(track.get("location") or "")
		if location == "missing value":
			location = ""
		path = _resolved(location) if location else ""
		marker = re.search(r"(?:^|[;\s])recording_id=([^\s;,]+)", str(track.get("comment") or ""))
		marked = marker.group(1) if marker else ""
		owned_marker = bool(marked or "Managed by Crate Music Importer" in str(track.get("comment") or ""))
		rids = set(refs.get(pid, set())) | set(registered.get(path, set()))
		if marked in recordings:
			rids.add(marked)
		inside = bool(path and _inside(Path(path), paths.root))
		ownership = "importer_referenced" if rids else "user_owned"
		if inside or owned_marker:
			ownership = "importer_owned" if rids else "untracked"
		if len(rids) > 1 or (marked and marked not in recordings and rids) or (path in registered and not inside):
			ownership = "ambiguous"
		entry = {"track": dict(track), "musicPresent": True, "persistent_id": pid, "path": path, "recordingIds": sorted(rids), "ownership": ownership,
			"evidence": {"managedRoot": inside, "registeredPath": path in registered, "ownershipMarker": marked or owned_marker, "manifestPersistentId": bool(refs.get(pid))}}
		entries.append(entry)
		if pid:
			by_pid[pid].append(entry)

	def add(category: str, detail: str, group: list[dict[str, Any]], *, check: str = "manifestConsistency", severity: str = "warning", evidence: Any = None, next_step: str = "Review these diagnostics and the original sources before making any manual changes.") -> None:
		ownerships = {entry["ownership"] for entry in group}
		ownership = next(iter(ownerships)) if len(ownerships) == 1 else "ambiguous"
		issue = {
			"severity": severity, "category": category, "ownership": ownership, "title": category.replace("_", " ").capitalize(), "detail": detail,
			"persistentIds": sorted({entry["persistent_id"] for entry in group if entry["persistent_id"]}),
			"recordingIds": sorted({rid for entry in group for rid in entry["recordingIds"]}),
			"paths": sorted({entry["path"] for entry in group if entry["path"]}),
			"tracks": [{key: entry["track"].get(key, "") for key in ("artist", "title", "album", "persistent_id")} for entry in group],
			"evidence": evidence if evidence is not None else [entry["evidence"] for entry in group],
			"suggestedAction": "review", "suggestedNextStep": next_step,
		}
		issue["id"] = hashlib.sha256(json.dumps(issue, sort_keys=True).encode()).hexdigest()[:20]
		issues.append(issue)
		if (severity != "informational" or category == "semantic_duplicate") and report["checks"][check] != "failed":
			report["checks"][check] = "attention"

	for pid, group in by_pid.items():
		if len(group) > 1:
			add("duplicate_persistent_id", "Music returned the same persistent ID more than once.", group)
	for entry in entries:
		if not entry["persistent_id"]:
			add("missing_persistent_id", "Music returned a track without a persistent ID.", [entry])
		if entry["ownership"] == "untracked":
			add("untracked_managed_track", "Music track has Crate storage or ownership evidence but no manifest recording.", [entry])
		if entry["ownership"] == "ambiguous":
			add("conflicting_recording_references", "Ownership or manifest recording references conflict.", [entry])

	known_locations = {entry["path"] for entry in entries if entry["path"]}
	# Include registered files even when Music has no matching item.
	for rid, recording in recordings.items():
		managed = recording.get("managed_file") or {}
		music = recording.get("music") or {}
		active = recording.get("active_reference") or {}
		pids = {str(ref["persistent_id"]) for ref in (music, active) if ref.get("persistent_id")}
		path = _resolved(paths.root / managed["relative_path"]) if managed.get("relative_path") else ""
		meta = recording.get("source_metadata") or {}
		entry = {"track": {"title": meta.get("title", ""), "artist": meta.get("artists", ""), "album": meta.get("original_album", "")},
			"musicPresent": False, "persistent_id": str(music.get("persistent_id") or active.get("persistent_id") or ""), "path": path, "recordingIds": [rid],
			"ownership": "importer_owned" if path and _inside(Path(path), paths.root) else "importer_referenced", "evidence": {"manifestRecording": rid, "registeredPath": path}}
		if len(pids) > 1:
			add("conflicting_music_ids", "One recording refers to conflicting Music persistent IDs.", [entry], evidence=sorted(pids))
		for pid in sorted(pids):
			if pid not in by_pid:
				add("manifest_id_absent", "Manifest persistent ID is absent from the full Music scan.", [{**entry, "persistent_id": pid}])
			elif path and any(item["path"] != path for item in by_pid[pid]):
				add("music_location_mismatch", "Music location differs from the registered managed file.", [entry, *by_pid[pid]])
		if active.get("kind") == "existing_music" and meta.get("title") and meta.get("artists"):
			for actual in by_pid.get(str(active.get("persistent_id") or ""), []):
				score, reasons = score_music_candidate(meta, actual["track"])
				if score < MUSIC_CONFIDENCE_MIN:
					add("active_reference_mismatch", "Active Music reference no longer matches its manifest recording identity.", [actual], evidence={"score": score, "reasons": reasons, "sourceMetadata": meta})
		if active.get("recording_id") and active["recording_id"] != rid:
			add("active_reference_mismatch", "Active reference points to a different recording.", [entry], evidence=active)
		if active.get("kind") == "managed_file" and (not path or (active.get("relative_path") and _resolved(paths.root / active["relative_path"]) != path)):
			add("active_reference_mismatch", "Active managed reference does not match its registered file.", [entry], evidence=active)
		if active.get("kind") == "existing_music" and not active.get("persistent_id"):
			add("active_reference_mismatch", "Active Music reference has no persistent ID.", [entry], evidence=active)
		if path and not _inside(Path(path), paths.root):
			entry["ownership"] = "ambiguous"
			add("managed_path_outside_root", "Registered managed path escapes configured storage; no ownership is assumed.", [entry])
		if path and path not in known_locations:
			entries.append(entry)
			known_locations.add(path)

	try:
		cache = load_music_cache(paths, require_complete=False)
		if not cache.get("initial_scan_completed"):
			add("incomplete_cache", "The persistent Music cache has not completed its initial scan.", [], next_step="Run Rebuild Music Library Cache.")
		for pid, cached in cache["tracks"].items():
			live = by_pid.get(pid, [])
			if cached.get("stale") or cached.get("validation_required") or not live or any(str(item["track"].get("location") or "") != str(cached.get("location") or "") for item in live):
				add("stale_cache_reference", "Cached reference is stale, incomplete, or differs from the full Music scan.", live or [{"track": cached, "persistent_id": pid, "path": str(cached.get("location") or ""), "recordingIds": sorted(refs.get(pid, [])), "ownership": "importer_referenced" if refs.get(pid) else "user_owned", "evidence": {}}], evidence=cached, next_step="Run Rebuild Music Library Cache.")
		migration = cache.get("migration") or {}
		pending = sorted(set(migration.get("incomplete_persistent_ids") or []) | set(migration.get("entries_requiring_validation") or {}))
		if pending:
			add("incomplete_cache", "Cache migration references still require validation.", [], evidence={"persistentIds": pending}, next_step="Run Rebuild Music Library Cache.")
		missing = sorted(set(by_pid) - set(cache["tracks"]))
		if missing:
			add("incomplete_cache", "Music IDs are absent from the persistent cache.", [], evidence={"missingPersistentIds": missing}, next_step="Run Rebuild Music Library Cache.")
	except Exception as exc:
		add("cache_unavailable", str(exc), [], next_step="Run Rebuild Music Library Cache.")

	files: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for entry in entries:
		if entry["path"]:
			files[entry["path"]].append(entry)
		else:
			add("cloud_only", "No local location was returned; this may be a cloud-only track. Local integrity was not checked.", [entry], check="fileIntegrity", severity="informational")
	by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
	semantic: dict[Any, list[dict[str, Any]]] = defaultdict(list)
	deep_count = 0
	for index, (location, group) in enumerate(files.items(), 1):
		if on_progress:
			on_progress({"phase": "health_files", "checked": index, "total": len(files)})
		path = Path(location)
		try:
			if not path.is_file():
				raise OSError("Path is missing or is not a regular file.")
			with path.open("rb") as handle:
				handle.read(1)
			if path.stat().st_size == 0:
				add("empty_file", "Local audio file has zero bytes.", group, check="fileIntegrity", severity="critical")
				continue
			digest = file_sha256(path)
		except OSError as exc:
			add("missing_or_unreadable_file", str(exc), group, check="fileIntegrity", severity="critical")
			continue
		by_hash[digest].extend(group)
		try:
			duration = probe_duration_ms(path)
			if duration <= 0:
				raise ValueError("Actual audio duration is unavailable.")
		except Exception as exc:
			add("probe_failed", str(exc), group, check="fileIntegrity")
			report["checks"]["fileIntegrity"] = "failed"
			continue
		managed_group = any(entry["ownership"] == "importer_owned" for entry in group)
		if managed_group or deep_all:
			deep_count += 1
			try:
				_decode(path)
			except (MediaError, OSError, subprocess.TimeoutExpired) as exc:
				add("decode_unavailable", str(exc), group, check="fileIntegrity")
				report["checks"]["fileIntegrity"] = "failed"
			except Exception as exc:
				add("decode_failed", str(exc), group, check="fileIntegrity", severity="critical")
		for entry in group:
			track = entry["track"]
			key = (normalize_recording_title(track.get("title")), normalized_artists(track.get("artist")), version_markers(track.get("title")))
			if key[0] and key[1]:
				semantic[key].append({**entry, "sha256": digest, "duration": duration})
			if entry["ownership"] != "importer_owned" or len(entry["recordingIds"]) != 1:
				continue
			recording = recordings[entry["recordingIds"][0]]
			managed = recording.get("managed_file") or {}
			if managed.get("sha256") and managed["sha256"] != digest:
				add("managed_hash_mismatch", "File hash differs from the registered managed hash; metadata comparisons skipped.", [entry], evidence={"expected": managed["sha256"], "actual": digest})
				continue
			try:
				_metadata_checks(path, entry, recording, manifest, duration, add)
			except Exception as exc:
				add("metadata_check_failed", str(exc), [entry], check="metadata")
				report["checks"]["metadata"] = "failed"

	for digest, group in sorted(by_hash.items()):
		if len(group) > 1:
			add("exact_duplicate", "Identical SHA-256 bytes occur in multiple Music items or files.", group, check="duplicates", evidence={"sha256": digest})
	for key, group in semantic.items():
		# A tight fixed duration window preserves version identity and avoids bucket-edge misses.
		ordered = sorted(group, key=lambda entry: entry["duration"])
		clusters: list[list[dict[str, Any]]] = []
		for entry in ordered:
			if not clusters or entry["duration"] - clusters[-1][0]["duration"] > 2000:
				clusters.append([])
			clusters[-1].append(entry)
		for cluster in clusters:
			if len({entry["sha256"] for entry in cluster}) > 1:
				add("semantic_duplicate", "Normalized artist, title, version markers and duration match, but file hashes differ. This is a review candidate, not proof of identical audio.", cluster, check="duplicates", severity="informational", evidence={"identity": key, "hashes": [entry["sha256"] for entry in cluster], "durationsMs": [entry["duration"] for entry in cluster]})
	spotify: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for entry in entries:
		for rid in entry["recordingIds"]:
			for sid in recordings[rid].get("spotify_ids") or []:
				spotify[sid].append(entry)
	for sid, group in spotify.items():
		if len({entry["path"] for entry in group if entry["path"]}) > 1:
			add("spotify_duplicate", "The same Spotify recording ID references multiple files.", group, check="duplicates", evidence={"spotifyId": sid})
	counts = {severity: sum(issue["severity"] == severity for issue in issues) for severity in ("critical", "warning", "informational")}
	report["summary"] = {"musicTracks": len(music_tracks), "localFiles": len(files), "managedTracks": sum(entry["ownership"] == "importer_owned" for entry in entries[:len(music_tracks)]),
		"deepDecodedFiles": deep_count, "critical": counts["critical"], "warnings": counts["warning"], "informational": counts["informational"],
		"missingFiles": sum(issue["category"] == "missing_or_unreadable_file" for issue in issues),
		"corruptFiles": sum(issue["category"] in ("empty_file", "decode_failed") for issue in issues),
		"exactDuplicateGroups": sum(issue["category"] == "exact_duplicate" for issue in issues),
		"possibleRecordingDuplicates": sum(issue["category"] in ("semantic_duplicate", "spotify_duplicate") for issue in issues),
		"manifestInconsistencies": sum(issue["category"] in ("duplicate_persistent_id", "missing_persistent_id", "untracked_managed_track", "conflicting_recording_references", "conflicting_music_ids", "manifest_id_absent", "music_location_mismatch", "active_reference_mismatch", "managed_path_outside_root", "managed_hash_mismatch", "incomplete_cache", "stale_cache_reference", "cache_unavailable") for issue in issues),
		"metadataDiscrepancies": sum(issue["category"] in ("duration_mismatch", "tag_mismatch", "artwork_discrepancy", "metadata_check_failed") for issue in issues)}
	report["status"] = "failed" if "failed" in report["checks"].values() else "attention" if counts["critical"] or counts["warning"] or any(issue["category"] == "semantic_duplicate" for issue in issues) else "healthy"
	return report


def _metadata_checks(path: Path, entry: dict[str, Any], recording: dict[str, Any], manifest: dict[str, Any], duration: int, add: Callable[..., None]) -> None:
	meta = recording.get("source_metadata") or {}
	managed = recording.get("managed_file") or {}
	album = (recording.get("album_metadata") or {}) if managed.get("metadata_profile") == "album" else {}
	for label, expected, ratio, minimum in (("Spotify", meta.get("duration_ms"), 0.05, 5000), ("downloaded source", managed.get("source_duration_ms"), 0.01, 500)):
		if expected and not _duration_matches(duration, int(expected), ratio=ratio, minimum_tolerance_ms=minimum):
			add("duration_mismatch", f"Actual duration differs from {label} beyond the existing importer tolerance.", [entry], check="metadata", evidence={"actualMs": duration, "expectedMs": expected, "ratio": ratio, "minimumToleranceMs": minimum})
	if path.suffix.casefold() != ".mp3":
		return
	tags, artwork_problems = _tags_and_artwork(path)
	artwork_problems = list(artwork_problems)
	expected = {"title": meta.get("title", ""), "artist": meta.get("artists", ""), "album": album.get("album", "") if album else "Playlist Imports"}
	differences = []
	for field, wanted in expected.items():
		for source, actual in (("MP3", tags.get(field, "")), ("Music", entry["track"].get(field, ""))):
			if source == "Music" and not entry.get("musicPresent", True):
				continue
			if normalize_text(actual) != normalize_text(wanted):
				differences.append({"source": source, "field": field, "expected": wanted, "actual": actual})
	for field, tag in (("track_no", "track"), ("disc_no", "disc")):
		wanted = int(album.get(field) or 0)
		actual = str(tags.get(tag) or "0").split("/")[0]
		if actual != str(wanted):
			differences.append({"source": "MP3", "field": field, "expected": wanted, "actual": actual})
		if entry.get("musicPresent", True) and int(entry["track"].get(field) or 0) != wanted:
			differences.append({"source": "Music", "field": field, "expected": wanted, "actual": entry["track"].get(field)})
	compilation = bool(album.get("is_compilation")) if album else True
	if (str(tags.get("compilation") or "0") == "1") != compilation:
		differences.append({"source": "MP3", "field": "compilation", "expected": compilation, "actual": tags.get("compilation")})
	if entry.get("musicPresent", True) and bool(entry["track"].get("compilation")) != compilation:
		differences.append({"source": "Music", "field": "compilation", "expected": compilation, "actual": entry["track"].get("compilation")})
	if differences:
		add("tag_mismatch", "Music or MP3 tags differ from the manifest's intended metadata profile.", [entry], check="metadata", evidence={"differences": differences, "sourceMetadata": meta, "albumMetadata": album})
	cover = meta.get("cover_url")
	stored_cover = managed.get("artwork_source_url")
	if not stored_cover:
		artwork_problems.append("Managed file has no saved per-track artwork source.")
	elif stored_cover != cover:
		artwork_problems.append("Managed file artwork source differs from current per-track source metadata.")
	if not cover:
		artwork_problems.append("Manifest has no per-track artwork source.")
	if not album and cover and any(playlist.get("cover_url") == cover for playlist in (manifest.get("playlists") or {}).values()):
		artwork_problems.append("Per-track artwork source matches a shared playlist cover; review its source.")
	if artwork_problems:
		add("artwork_discrepancy", " ".join(artwork_problems), [entry], check="artwork", evidence={"coverUrl": cover, "storedArtworkSource": stored_cover})
