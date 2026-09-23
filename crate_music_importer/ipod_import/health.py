"""Library diagnostics for real Music bindings and managed-media integrity."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Callable

from crate_music_importer.ipod_import.dependency_lock import dependency_lock
from crate_music_importer.ipod_import.identity import normalize_text, normalized_artists, normalize_recording_title, version_markers
from crate_music_importer.ipod_import.manifest import ManagedPaths, file_sha256, save_manifest
from crate_music_importer.ipod_import.media import _duration_matches, _tool, audio_sha256, probe_duration_ms
from crate_music_importer.ipod_import.music import MusicIndex, music_binding_id, reconcile_recording_music


CHECKS = ("musicScan", "fileIntegrity", "duplicates", "manifestConsistency", "metadata", "artwork")
DUPLICATE_DISMISSALS = "health-duplicate-dismissals.json"


def duplicate_pair(first: str, second: str) -> tuple[str, str]:
	return tuple(sorted((first, second)))


def load_duplicate_dismissals(paths: ManagedPaths) -> set[tuple[str, str]]:
	try:
		data = json.loads((paths.state_dir / DUPLICATE_DISMISSALS).read_text(encoding="utf-8"))
	except FileNotFoundError:
		return set()
	if data.get("schemaVersion") != 1 or not isinstance(data.get("pairs"), list):
		raise ValueError("Invalid duplicate dismissal state.")
	return {tuple(pair) for pair in data["pairs"] if isinstance(pair, list) and len(pair) == 2 and all(isinstance(pid, str) and pid for pid in pair) and pair[0] < pair[1]}


def _eligible_duplicate_pairs(tracks: list[dict[str, Any]]) -> list[tuple[int, int]]:
	pairs = []
	for left, right in combinations(range(len(tracks)), 2):
		first, second = tracks[left], tracks[right]
		first_id, second_id = str(first.get("persistent_id") or ""), str(second.get("persistent_id") or "")
		first_duration, second_duration = first.get("duration"), second.get("duration")
		if not first_id or not second_id or first_id == second_id or first.get("sha256") == second.get("sha256"):
			continue
		if not isinstance(first_duration, (int, float)) or not isinstance(second_duration, (int, float)) or first_duration <= 0 or second_duration <= 0:
			continue
		if abs(first_duration - second_duration) <= max(2000, 0.02 * max(first_duration, second_duration)):
			pairs.append((left, right))
	return pairs


def apply_duplicate_dismissals(report: dict[str, Any], dismissed: set[tuple[str, str]]) -> dict[str, Any]:
	"""Filter possible recording pairs, including groups in older saved reports."""
	if not any(issue.get("category") == "possible_recording_duplicate" for issue in report.get("issues", [])):
		return report
	issues = []
	for issue in report.get("issues", []):
		if issue.get("category") != "possible_recording_duplicate":
			issues.append(issue)
			continue
		tracks = issue.get("tracks") or []
		pairs = [(left, right) for left, right in _eligible_duplicate_pairs(tracks)
			if duplicate_pair(str(tracks[left]["persistent_id"]), str(tracks[right]["persistent_id"])) not in dismissed]
		adjacent: dict[int, set[int]] = defaultdict(set)
		for left, right in pairs:
			adjacent[left].add(right)
			adjacent[right].add(left)
		while adjacent:
			start = next(iter(adjacent))
			members, pending = set(), [start]
			while pending:
				current = pending.pop()
				if current in members:
					continue
				members.add(current)
				pending.extend(adjacent[current] - members)
			for member in members:
				adjacent.pop(member, None)
			group = [tracks[index] for index in sorted(members)]
			updated = {**issue, "tracks": group,
				"persistentIds": sorted({str(track["persistent_id"]) for track in group}),
				"paths": sorted({str(track.get("location") or "") for track in group if track.get("location")})}
			updated.pop("id", None)
			updated["id"] = hashlib.sha256(json.dumps(updated, sort_keys=True).encode()).hexdigest()[:20]
			issues.append(updated)
	report["issues"] = issues
	if report.get("status") != "failed" and report.get("summary") is not None:
		report["summary"]["possibleRecordingDuplicates"] = sum(issue["category"] == "possible_recording_duplicate" for issue in issues)
		report["checks"]["duplicates"] = "attention" if any(issue["category"] in ("possible_recording_duplicate", "exact_file_duplicate", "duplicate_persistent_id") and issue["severity"] != "informational" for issue in issues) else "passed"
		report["status"] = "attention" if any(issue["severity"] != "informational" for issue in issues) else "healthy"
	return report


def failed_health_report(error: str) -> dict[str, Any]:
	return {
		"schemaVersion": 2, "checkedAt": datetime.now(timezone.utc).isoformat(),
		"status": "failed", "error": error, "summary": {},
		"checks": {name: "failed" if name == "musicScan" else "not_checked" for name in CHECKS}, "issues": [],
	}


def save_health_report(paths: ManagedPaths, report: dict[str, Any]) -> Path:
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

	def add(category: str, detail: str, *, check: str = "manifestConsistency", severity: str = "warning", persistent_ids: list[str] | None = None, recording_ids: list[str] | None = None, paths_value: list[str] | None = None, tracks: list[dict[str, Any]] | None = None, evidence: Any = None, next_step: str = "Review the evidence before making manual changes.") -> None:
		issue = {
			"severity": severity, "category": category, "title": category.replace("_", " ").capitalize(), "detail": detail,
			"persistentIds": sorted(set(persistent_ids or [])), "recordingIds": sorted(set(recording_ids or [])),
			"paths": sorted(set(paths_value or [])), "tracks": tracks or [], "evidence": evidence or {},
			"suggestedAction": "review", "suggestedNextStep": next_step,
		}
		issue["id"] = hashlib.sha256(json.dumps(issue, sort_keys=True).encode()).hexdigest()[:20]
		issues.append(issue)
		if severity != "informational" and report["checks"][check] != "failed":
			report["checks"][check] = "attention"

	by_pid: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for track in music_tracks:
		pid = str(track.get("persistent_id") or "")
		if not pid:
			add("missing_persistent_id", "Music returned a track without a persistent ID.", tracks=[track])
		else:
			by_pid[pid].append(track)
	for pid, group in by_pid.items():
		if len(group) > 1:
			add("duplicate_persistent_id", "Music returned the same persistent ID more than once.", persistent_ids=[pid], tracks=group, check="duplicates")

	index = MusicIndex(music_tracks)
	binding_repairs = 0
	resolutions: dict[str, dict[str, Any]] = {}
	for recording_id, recording in recordings.items():
		result = reconcile_recording_music(recording, index)
		resolutions[recording_id] = result
		binding_repairs += int(result.get("binding_changed", False))
		if result["status"] == "ambiguous":
			add("ambiguous_music_match", "Multiple Music tracks plausibly represent this recording.", recording_ids=[recording_id], persistent_ids=[str(item.get("persistent_id") or "") for item in result["candidates"]], tracks=result["candidates"], next_step="Choose the correct Music track in Import Activity & Problems.")
		elif result["status"] == "missing":
			metadata = recording.get("source_metadata") or {}
			bound_id = music_binding_id(recording)
			bound_track = index.by_persistent_id.get(bound_id)
			if bound_track:
				add("bound_music_identity_mismatch", "The saved Music track exists, but its metadata does not safely identify this recording.", recording_ids=[recording_id], persistent_ids=[bound_id], tracks=[bound_track], evidence={"source": {"title": metadata.get("title"), "artists": metadata.get("artists"), "album": metadata.get("original_album"), "durationMs": metadata.get("duration_ms")}}, next_step="Review the saved Music track and recording identity before changing the binding.")
			else:
				add("recording_missing_from_music", "This recording cannot be found safely in Music.app.", recording_ids=[recording_id], tracks=[{"artist": metadata.get("artists", ""), "title": metadata.get("title", ""), "album": metadata.get("original_album", ""), "persistent_id": ""}], next_step="Retry the source to download or add the recording once.")
	if binding_repairs:
		save_manifest(paths, manifest)

	bound: dict[str, list[str]] = defaultdict(list)
	for recording_id, recording in recordings.items():
		if persistent_id := music_binding_id(recording):
			bound[persistent_id].append(recording_id)
	for persistent_id, recording_ids in bound.items():
		if len(recording_ids) < 2:
			continue
		identities = {json.dumps((recordings[recording_id].get("normalized_identity") or {}), sort_keys=True) for recording_id in recording_ids}
		if len(identities) > 1:
			add("incompatible_shared_binding", "Different recordings are bound to the same Music track.", persistent_ids=[persistent_id], recording_ids=recording_ids)

	managed_paths: dict[str, str] = {}
	for recording_id, recording in recordings.items():
		managed = recording.get("managed_file") or {}
		if managed.get("managed_by_crate") and managed.get("relative_path"):
			path = _resolved(paths.root / str(managed["relative_path"]))
			managed_paths[path] = recording_id
			if not _inside(Path(path), paths.root):
				add("managed_path_outside_root", "A registered managed file escapes the configured managed directory.", recording_ids=[recording_id], paths_value=[path])

	files: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for track in music_tracks:
		location = str(track.get("location") or "")
		if location and location != "missing value":
			files[_resolved(location)].append(track)
		else:
			add("cloud_only", "Music returned no local file location; local integrity was not checked.", severity="informational", check="fileIntegrity", persistent_ids=[str(track.get("persistent_id") or "")], tracks=[track])
	for path in managed_paths:
		files.setdefault(path, [])

	by_hash: dict[str, list[str]] = defaultdict(list)
	semantic: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
	deep_count = 0
	for position, (location, music_group) in enumerate(files.items(), start=1):
		if on_progress:
			on_progress({"phase": "health_files", "checked": position - 1, "total": len(files)})
		path = Path(location)
		recording_id = managed_paths.get(location)
		try:
			file_stat = path.stat()
			if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size == 0:
				raise OSError("Path is not a nonempty regular file.")
			with path.open("rb") as handle:
				handle.read(1)
			digest = file_sha256(path)
		except OSError as exc:
			add("missing_or_unreadable_file", str(exc), check="fileIntegrity", severity="critical" if recording_id else "warning", recording_ids=[recording_id] if recording_id else [], paths_value=[location], tracks=music_group)
			continue
		by_hash[digest].append(location)
		try:
			duration = probe_duration_ms(path)
			if duration <= 0:
				raise ValueError("Actual audio duration is unavailable.")
		except Exception as exc:
			add("probe_failed", str(exc), check="fileIntegrity", recording_ids=[recording_id] if recording_id else [], paths_value=[location], tracks=music_group)
			continue
		if recording_id or deep_all:
			deep_count += 1
			try:
				_decode(path)
			except Exception as exc:
				add("decode_failed", str(exc), check="fileIntegrity", severity="critical", recording_ids=[recording_id] if recording_id else [], paths_value=[location], tracks=music_group)
		if recording_id:
			recording = recordings[recording_id]
			managed = recording.get("managed_file") or {}
			if managed.get("sha256") and managed["sha256"] != digest:
				try:
					actual_audio = audio_sha256(path)
				except Exception as exc:
					add("audio_fingerprint_unavailable", str(exc), check="fileIntegrity", recording_ids=[recording_id], paths_value=[location])
				else:
					if not managed.get("audio_sha256") or actual_audio != managed.get("audio_sha256"):
						add("audio_content_changed", "The encoded audio differs from its saved managed-file baseline.", check="fileIntegrity", recording_ids=[recording_id], paths_value=[location], evidence={"expected": managed.get("audio_sha256"), "actual": actual_audio})
			try:
				_metadata_checks(path, music_group[0] if music_group else {}, recording, manifest, duration, add)
			except Exception as exc:
				add("metadata_check_failed", str(exc), check="metadata", recording_ids=[recording_id], paths_value=[location])
		for track in music_group:
			key = (normalize_recording_title(track.get("title")), normalized_artists(track.get("artist")), version_markers(track.get("title")))
			if key[0] and key[1]:
				semantic[key].append({**track, "sha256": digest, "duration": duration, "location": location})

	for digest, locations in by_hash.items():
		if len(set(locations)) > 1:
			add("exact_file_duplicate", "Multiple local files have identical bytes.", check="duplicates", paths_value=locations, evidence={"sha256": digest})
	for group in semantic.values():
		if len(group) > 1 and len({item["sha256"] for item in group}) > 1:
			add("possible_recording_duplicate", "Multiple Music items plausibly contain the same recording.", check="duplicates", persistent_ids=[str(item.get("persistent_id") or "") for item in group], paths_value=[str(item.get("location") or "") for item in group], tracks=group)

	if on_progress:
		on_progress({"phase": "health_finalizing", "checked": len(files), "total": len(files)})
	report["summary"] = {
		"musicTracks": len(music_tracks), "localFiles": len(files), "managedFiles": len(managed_paths),
		"bindingRepairs": binding_repairs,
		"missingFiles": sum(issue["category"] == "missing_or_unreadable_file" for issue in issues),
		"corruptFiles": sum(issue["category"] in ("probe_failed", "decode_failed", "audio_content_changed") for issue in issues),
		"exactDuplicateGroups": sum(issue["category"] == "exact_file_duplicate" for issue in issues),
		"possibleRecordingDuplicates": sum(issue["category"] == "possible_recording_duplicate" for issue in issues),
		"manifestInconsistencies": sum(issue["category"] in ("ambiguous_music_match", "bound_music_identity_mismatch", "recording_missing_from_music", "incompatible_shared_binding", "managed_path_outside_root") for issue in issues),
		"metadataDiscrepancies": sum(issue["category"] in ("duration_mismatch", "tag_mismatch", "artwork_discrepancy") for issue in issues),
		"deepDecodedFiles": deep_count,
	}
	report["status"] = "attention" if any(issue["severity"] != "informational" for issue in issues) else "healthy"
	return apply_duplicate_dismissals(report, load_duplicate_dismissals(paths))


def _metadata_checks(path: Path, track: dict[str, Any], recording: dict[str, Any], manifest: dict[str, Any], duration: int, add: Callable[..., None]) -> None:
	meta = recording.get("source_metadata") or {}
	managed = recording.get("managed_file") or {}
	album = (recording.get("album_metadata") or {}) if managed.get("metadata_profile") == "album" else {}
	preferences = recording.get("local_preferences") or {}
	accepted = preferences.get("accepted_duration") or {}
	checks = (("your accepted file", accepted.get("duration_ms"), 0.01, 500),) if accepted.get("audio_sha256") == managed.get("audio_sha256") else (("Spotify", meta.get("duration_ms"), 0.05, 5000), ("downloaded source", managed.get("source_duration_ms"), 0.01, 500))
	for label, expected, ratio, minimum in checks:
		if expected and not _duration_matches(duration, int(expected), ratio=ratio, minimum_tolerance_ms=minimum):
			add("duration_mismatch", f"Actual duration differs from {label} beyond tolerance.", check="metadata", recording_ids=[recording["recording_id"]], paths_value=[str(path)], evidence={"actualMs": duration, "expectedMs": expected})
	if path.suffix.casefold() != ".mp3":
		return
	tags, artwork_problems = _tags_and_artwork(path)
	expected = {"title": preferences.get("title") or meta.get("title", ""), "artist": meta.get("artists", "")}
	if managed.get("metadata_profile") in ("playlist", "album"):
		expected["album"] = album.get("album", "") if album else "Playlist Imports"
	differences = []
	for field, wanted in expected.items():
		for source, actual in (("MP3", tags.get(field, "")), ("Music", track.get(field, ""))):
			if source == "Music" and not track:
				continue
			if normalize_text(actual) != normalize_text(wanted):
				differences.append({"source": source, "field": field, "expected": wanted, "actual": actual})
	if differences:
		add("tag_mismatch", "Music or MP3 tags differ from the intended managed-file metadata.", check="metadata", recording_ids=[recording["recording_id"]], paths_value=[str(path)], evidence={"differences": differences})
	if artwork_problems:
		add("artwork_discrepancy", " ".join(artwork_problems), check="artwork", recording_ids=[recording["recording_id"]], paths_value=[str(path)])
