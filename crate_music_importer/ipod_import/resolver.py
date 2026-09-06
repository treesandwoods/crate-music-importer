"""Structured problem review and deliberate resolution for the Raycast UI."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable

from crate_music_importer.ipod_import.constants import MUSIC_CONFIDENCE_MIN
from crate_music_importer.ipod_import.identity import clean_release_labels, normalize_text, score_music_candidate
from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest, save_manifest, update_manifest
from crate_music_importer.ipod_import.media import recover_moved_managed_file
from crate_music_importer.ipod_import.music import MusicIndex, lookup_music_track
from crate_music_importer.ipod_import.music_cache import (
	load_music_cache,
	mark_music_cache_entry_stale,
	music_cache_tracks,
	upsert_music_cache_track,
	validate_exact_track,
)
from crate_music_importer.ipod_import.youtube import inspect_manual_url, score_candidate, search_candidates, rank_candidates


BLOCKED_REVIEW_KINDS = {"album_conflict", "album_release_conflict"}
CHOICE_REVIEW_KINDS = {"youtube_missing", "youtube_ambiguity", "music_ambiguity", "album_conflict", "album_duplicate_conflict"}
STALE_AUTOMATCH_ERROR = "No YouTube result scored strictly above 0.87."


def _youtube_choice_can_resolve(recording: dict[str, Any]) -> bool:
	review_kind = str((recording.get("review") or {}).get("kind") or "")
	if review_kind in ("youtube_missing", "youtube_ambiguity"):
		return True
	error = str(recording.get("last_error") or "").casefold()
	if not error and not (recording.get("youtube") or {}).get("url"):
		return True
	return bool(error) and (
		error.startswith("youtube rejected the download")
		or error.startswith("the selected youtube video")
		or error.startswith("youtube requested sign-in")
		or error.startswith("yt-dlp failed")
		or error == "no reviewed youtube source is available for this recording."
		or error == "yt-dlp finished but no source audio was found."
		or error.startswith("generated mp3 duration")
	)


def _recording_key(manifest: dict[str, Any], value: str) -> str:
	text = str(value or "").strip()
	if text in manifest["recordings"]:
		return text
	for key, recording in manifest["recordings"].items():
		if text in recording.get("spotify_ids", []):
			return key
	raise ValueError(f"Unknown recording or Spotify track ID: {value}")


def _source_url(source_type: str, source_id: str, source: dict[str, Any]) -> str:
	url = str(source.get("spotify_url") or "").strip()
	if url:
		return url
	return f"https://open.spotify.com/{source_type}/{source_id}"


def _source_entry(source_type: str, source_id: str, source: dict[str, Any]) -> dict[str, Any]:
	return {
		"type": source_type,
		"id": source_id,
		"name": str(source.get("name") or ("Spotify Album" if source_type == "album" else "Spotify Playlist")),
		"url": _source_url(source_type, source_id, source),
		"itemCount": len(source.get("items") or []),
	}


def _recording_sources(manifest: dict[str, Any], recording: dict[str, Any]) -> list[dict[str, Any]]:
	sources: list[dict[str, Any]] = []
	for album_id in recording.get("album_memberships", {}):
		album = manifest.get("albums", {}).get(album_id)
		if album:
			sources.append(_source_entry("album", album_id, album))
	for playlist_id in recording.get("playlist_memberships", {}):
		playlist = manifest.get("playlists", {}).get(playlist_id)
		if playlist:
			sources.append(_source_entry("playlist", playlist_id, playlist))
	return sources


def _is_importer_owned(recording: dict[str, Any], candidate: dict[str, Any]) -> bool:
	comment = str(candidate.get("comment") or "")
	if f"recording_id={recording['recording_id']}" in comment:
		return True
	music = recording.get("music") or {}
	return (
		str(candidate.get("persistent_id") or "") == str(music.get("persistent_id") or "")
		and str(music.get("source") or "") in ("managed_import", "managed_album_import", "managed_album_upgrade")
	)


def _candidate_matches_album(candidate: dict[str, Any], album_names: set[str]) -> bool:
	album = normalize_text(clean_release_labels(candidate.get("album")))
	return bool(album and album in album_names)


def _youtube_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
	return {
		"kind": "youtube",
		"id": str(candidate.get("video_id") or ""),
		"title": str(candidate.get("title") or ""),
		"uploader": str(candidate.get("uploader") or ""),
		"durationSeconds": float(candidate.get("duration_s") or 0),
		"score": candidate.get("score"),
		"url": str(candidate.get("url") or ""),
		"reasons": list(candidate.get("reasons") or []),
		"selectable": bool(candidate.get("url")),
		"matchingEvidence": candidate.get("matching_evidence"),
		"automaticEligible": candidate.get("automatic_eligible"),
	}


def _music_candidate(
	recording: dict[str, Any],
	candidate: dict[str, Any],
	*,
	review_kind: str,
	album_names: set[str],
	paths: ManagedPaths,
) -> dict[str, Any]:
	scored = dict(candidate)
	if scored.get("score") is None:
		candidate_score, score_reasons = score_music_candidate(recording["source_metadata"], scored)
		scored["score"] = round(candidate_score, 4)
		scored["reasons"] = list(scored.get("reasons") or []) + score_reasons
	candidate = scored
	owned = _is_importer_owned(recording, candidate)
	album_match = not album_names or _candidate_matches_album(candidate, album_names)
	selectable = bool(candidate.get("persistent_id")) and float(candidate.get("score") or 0) >= MUSIC_CONFIDENCE_MIN
	promotion_eligible = bool(
		review_kind == "album_conflict"
		and owned
		and normalize_text(candidate.get("album")) == "playlist imports"
		and recover_moved_managed_file(recording, candidate, paths)
	)
	if album_names:
		selectable = selectable and (album_match or promotion_eligible)
	if review_kind == "album_duplicate_conflict":
		selectable = selectable and not owned and album_match
	return {
		"kind": "music",
		"id": str(candidate.get("persistent_id") or ""),
		"title": str(candidate.get("title") or candidate.get("name") or ""),
		"artist": str(candidate.get("artist") or ""),
		"album": str(candidate.get("album") or ""),
		"durationSeconds": float(candidate.get("duration_s") or candidate.get("duration") or 0),
		"score": candidate.get("score"),
		"location": candidate.get("location"),
		"reasons": list(candidate.get("reasons") or []),
		"importerOwned": owned,
		"albumMatch": album_match,
		"promotionEligible": promotion_eligible,
		"selectable": selectable,
	}


def _stale_error_after_manual_choice(recording: dict[str, Any]) -> bool:
	if recording.get("review"):
		return False
	youtube = recording.get("youtube") or {}
	return bool(
		recording.get("last_error") in (STALE_AUTOMATCH_ERROR, "No verified YouTube recording met the automatic identity and duration requirements.")
		and str(youtube.get("selected_by") or "").startswith("manual")
		and youtube.get("url")
	)


def _problem_row(manifest: dict[str, Any], key: str, recording: dict[str, Any], paths: ManagedPaths) -> dict[str, Any] | None:
	review = recording.get("review") or {}
	last_error = str(recording.get("last_error") or "")
	if not review and (not last_error or _stale_error_after_manual_choice(recording)):
		return None
	kind = str(review.get("kind") or "retryable_failure")
	sources = _recording_sources(manifest, recording)
	failure_source = recording.get("last_error_source") or {}
	if kind == "retryable_failure" and failure_source.get("type") and failure_source.get("id"):
		scoped_sources = [
			source
			for source in sources
			if source["type"] == failure_source["type"] and source["id"] == failure_source["id"]
		]
		if scoped_sources:
			sources = scoped_sources
	if kind in ("album_conflict", "album_release_conflict", "album_duplicate_conflict"):
		sources = [source for source in sources if source["type"] == "album"]
	album_names = {
		normalize_text(clean_release_labels(source["name"]))
		for source in sources
		if source["type"] == "album"
	}
	candidates: list[dict[str, Any]] = []
	review_candidates = review.get("candidates") or []
	if kind in ("youtube_missing", "youtube_ambiguity"):
		review_candidates = rank_candidates(recording.get("source_metadata") or {}, review_candidates)
	for candidate in review_candidates:
		if kind in ("youtube_missing", "youtube_ambiguity"):
			candidates.append(_youtube_candidate(candidate))
		else:
			candidates.append(
				_music_candidate(recording, candidate, review_kind=kind, album_names=album_names, paths=paths)
			)
	if kind in ("youtube_missing", "youtube_ambiguity"):
		# The candidate screen can run a fresh search or accept a direct URL even
		# when the automatic search saved no candidates.
		state = "needs_choice"
	elif kind in BLOCKED_REVIEW_KINDS and not any(candidate.get("promotionEligible") for candidate in candidates):
		state = "blocked"
	elif kind in CHOICE_REVIEW_KINDS:
		state = "needs_choice" if any(candidate.get("selectable") for candidate in candidates) else "blocked"
	else:
		state = "retryable"
	metadata = recording.get("source_metadata") or {}
	return {
		"recordingId": key,
		"title": str(metadata.get("title") or ""),
		"artists": str(metadata.get("artists") or ""),
		"album": str(metadata.get("original_album") or ""),
		"durationSeconds": float(metadata.get("duration_ms") or 0) / 1000.0,
		"coverUrl": metadata.get("cover_url"),
		"kind": kind,
		"state": state,
		"message": str(review.get("message") or last_error or "This recording needs attention."),
		"lastError": last_error or None,
		"candidates": candidates,
		"sources": sources,
		"searchSummary": review.get("search_summary"),
		"defaultSearchQuery": f"{metadata.get('artists', '')} {metadata.get('title', '')}".strip(),
		"hasChosenYouTube": bool((recording.get("youtube") or {}).get("url")),
		"canChooseDifferentYouTube": _youtube_choice_can_resolve(recording),
	}


def _recording_complete(recording: dict[str, Any], source_type: str, source_id: str) -> bool:
	active = recording.get("active_reference") or {}
	if active.get("kind") == "existing_music":
		return True
	if active.get("kind") != "managed_file" or not (recording.get("music") or {}).get("persistent_id"):
		return False
	if source_type == "album":
		managed = recording.get("managed_file") or {}
		return bool(managed.get("metadata_profile") == "album" and str(managed.get("spotify_album_id") or "") == source_id)
	return True


def _active_job(paths: ManagedPaths, source_type: str, source_id: str) -> dict[str, Any] | None:
	jobs_dir = paths.state_dir / "jobs"
	matches: list[dict[str, Any]] = []
	for path in jobs_dir.glob("*.json") if jobs_dir.is_dir() else []:
		try:
			data = json.loads(path.read_text(encoding="utf-8"))
		except (OSError, ValueError):
			continue
		if not isinstance(data, dict) or data.get("status") not in ("queued", "running"):
			continue
		source = data.get("source") or {}
		if source.get("type") == source_type and source.get("id") == source_id:
			matches.append(data)
	if not matches:
		return None
	job = max(matches, key=lambda value: str(value.get("createdAt") or ""))
	return {
		"jobId": job.get("jobId"),
		"pid": job.get("pid") or job.get("runnerPid"),
		"log": job.get("logPath"),
		"started_at": job.get("startedAt"),
		"status": job.get("status"),
		"phase": job.get("phase"),
		"counts": job.get("counts"),
	}


def _source_rows(manifest: dict[str, Any], paths: ManagedPaths, problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
	problem_by_recording = {problem["recordingId"]: problem for problem in problems}
	rows: list[dict[str, Any]] = []
	for source_type, sources in (("album", manifest.get("albums", {})), ("playlist", manifest.get("playlists", {}))):
		for source_id, source in sources.items():
			items = source.get("items") or []
			item_problems = [
				problem_by_recording[item["recording_id"]]
				for item in items
				if item["recording_id"] in problem_by_recording
				and any(
					reference["type"] == source_type and reference["id"] == source_id
					for reference in problem_by_recording[item["recording_id"]]["sources"]
				)
			]
			states = {problem["state"] for problem in item_problems}
			if "blocked" in states:
				state = "blocked"
			elif "needs_choice" in states:
				state = "needs_choice"
			elif "retryable" in states:
				state = "retryable"
			elif items and all(
				_recording_complete(manifest["recordings"][item["recording_id"]], source_type, source_id)
				for item in items
			):
				state = "complete"
			else:
				state = "ready"
			entry = _source_entry(source_type, source_id, source)
			entry.update({
				"state": state,
				"problemCount": len(item_problems),
				"problemRecordingIds": [problem["recordingId"] for problem in item_problems],
				"activeJob": _active_job(paths, source_type, source_id),
			})
			rows.append(entry)
	return rows


def resolver_snapshot(paths: ManagedPaths | None = None) -> dict[str, Any]:
	paths = paths or ManagedPaths()
	manifest = load_manifest(paths)
	problems = [
		problem
		for key, recording in manifest["recordings"].items()
		if (problem := _problem_row(manifest, key, recording, paths)) is not None
	]
	sources = _source_rows(manifest, paths, problems)
	return {
		"version": 1,
		"managedRoot": str(paths.root),
		"problems": problems,
		"sources": sources,
		"readySources": [source for source in sources if source["state"] == "ready" and not source.get("activeJob")],
	}


def search_youtube(
	recording_id: str,
	query: str | None = None,
	*,
	more: bool = False,
	paths: ManagedPaths | None = None,
	search: Callable[..., list[dict[str, Any]]] = search_candidates,
) -> dict[str, Any]:
	paths = paths or ManagedPaths()
	manifest = load_manifest(paths)
	key = _recording_key(manifest, recording_id)
	recording = manifest["recordings"][key]
	metadata = recording.get("source_metadata") or {}
	effective_query = str(query or "").strip() or f"{metadata.get('artists', '')} {metadata.get('title', '')}".strip()
	candidates = search(metadata, query=effective_query, **({"more": True} if more else {}))
	summary = getattr(candidates, "summary", {})
	if more:
		rejected = set(summary.get("rejected_video_ids") or [])
		cached = [c for c in (recording.get("review") or {}).get("candidates") or [] if c.get("video_id") not in rejected]
		candidates = rank_candidates(metadata, cached + list(candidates))
	return {
		"recordingId": key,
		"query": effective_query,
		"searchSummary": summary,
		"candidates": [_youtube_candidate(candidate) for candidate in candidates],
	}


def resolve_youtube(
	recording_id: str,
	youtube_url: str,
	*,
	paths: ManagedPaths | None = None,
	inspect: Callable[[str], dict[str, Any]] = inspect_manual_url,
	allow_music_override: bool = False,
) -> dict[str, Any]:
	paths = paths or ManagedPaths()
	manifest = load_manifest(paths)
	key = _recording_key(manifest, recording_id)
	recording = manifest["recordings"][key]
	previous_kind = str((recording.get("review") or {}).get("kind") or "")
	if not _youtube_choice_can_resolve(recording):
		raise ValueError("Choosing another YouTube recording cannot resolve this managed-file failure.")
	if previous_kind in BLOCKED_REVIEW_KINDS or previous_kind == "album_duplicate_conflict":
		raise ValueError("This album conflict cannot be resolved with a YouTube recording under the no-duplicate policy.")
	if previous_kind == "music_ambiguity" and not allow_music_override:
		raise ValueError("Choose one of the displayed Music tracks; a new duplicate download is not allowed.")
	candidate = inspect(youtube_url)
	score, reasons = score_candidate(recording["source_metadata"], candidate)
	candidate["metadata_score"] = round(score, 4)
	candidate["reasons"] = list(candidate.get("reasons") or []) + reasons
	candidate["selected_by"] = "manual_candidate"
	candidate["override_music_ambiguity"] = bool(allow_music_override and previous_kind == "music_ambiguity")

	def apply(latest: dict[str, Any]) -> None:
		latest_key = _recording_key(latest, key)
		latest_recording = latest["recordings"][latest_key]
		latest_kind = str((latest_recording.get("review") or {}).get("kind") or "")
		if not _youtube_choice_can_resolve(latest_recording):
			raise ValueError("Choosing another YouTube recording cannot resolve this managed-file failure.")
		if latest_kind in BLOCKED_REVIEW_KINDS or latest_kind == "album_duplicate_conflict":
			raise ValueError("This album conflict cannot be resolved with a YouTube recording under the no-duplicate policy.")
		if latest_kind == "music_ambiguity" and not allow_music_override:
			raise ValueError("Choose one of the displayed Music tracks; a new duplicate download is not allowed.")
		latest_recording["youtube"] = copy.deepcopy(candidate)
		latest_recording.pop("review", None)
		latest_recording.pop("last_error", None)
		latest_recording.pop("last_error_source", None)
		latest.setdefault("manual_youtube_overrides", {})[latest_key] = copy.deepcopy(candidate)

	update_manifest(paths, apply)
	return {"recordingId": key, "candidate": _youtube_candidate(candidate)}


def resolve_music(
	recording_id: str,
	persistent_id: str,
	*,
	paths: ManagedPaths | None = None,
	scan: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
	paths = paths or ManagedPaths()
	manifest = load_manifest(paths)
	key = _recording_key(manifest, recording_id)
	recording = manifest["recordings"][key]
	review = recording.get("review") or {}
	review_kind = str(review.get("kind") or "")
	if review_kind not in ("music_ambiguity", "album_conflict", "album_duplicate_conflict"):
		raise ValueError("This recording does not currently have a selectable Music-library problem.")
	displayed_ids = {str(candidate.get("persistent_id") or "") for candidate in review.get("candidates") or []}
	if persistent_id not in displayed_ids:
		raise ValueError("The selected Music track is no longer one of the displayed candidates.")
	index = MusicIndex(scan() if scan else music_cache_tracks(load_music_cache(paths)))
	cached = index.by_persistent_id.get(persistent_id)
	if not cached:
		raise ValueError("The selected Music track is no longer present in the Music library cache. Run ‘Rebuild Music Library Cache’.")
	candidate = cached if scan else lookup_music_track(persistent_id)
	valid, reason = validate_exact_track(cached, candidate)
	if not valid:
		if not scan:
			mark_music_cache_entry_stale(paths, persistent_id, reason)
		raise ValueError(f"The selected Music track changed: {reason}. Run ‘Rebuild Music Library Cache’.")
	assert candidate is not None
	score, reasons = score_music_candidate(recording["source_metadata"], candidate)
	if score < MUSIC_CONFIDENCE_MIN:
		raise ValueError("The selected Music track is no longer a confident metadata, duration, and version match.")
	album_names = {
		normalize_text(clean_release_labels(manifest["albums"][album_id].get("name")))
		for album_id in recording.get("album_memberships", {})
		if album_id in manifest.get("albums", {})
	}
	promotion_candidate = dict(candidate)
	if not promotion_candidate.get("location") and cached.get("location"):
		# Exact Music lookups can omit a cloud-managed file location even when the
		# full cache scan captured it. The cache path is accepted only after the
		# exact persistent ID/metadata check above and the managed SHA check below.
		promotion_candidate["location"] = cached["location"]
	promotion_path = recover_moved_managed_file(recording, promotion_candidate, paths) if review_kind == "album_conflict" else None
	if review_kind == "album_conflict" and not (
		promotion_path
		and _is_importer_owned(recording, candidate)
		and normalize_text(candidate.get("album")) == "playlist imports"
	):
		raise ValueError("Only the exact importer-owned Playlist Imports track can be promoted to this album.")
	if album_names and not _candidate_matches_album(candidate, album_names) and not promotion_path:
		raise ValueError("The selected Music track does not carry the requested album identity; choosing it would not safely resolve this album.")
	if review_kind == "album_duplicate_conflict" and _is_importer_owned(recording, candidate):
		raise ValueError("Choose the proper existing album copy, not the importer-owned duplicate.")
	recording["music"] = {
		"source": "existing_album_library" if album_names else "existing_library",
		"persistent_id": candidate.get("persistent_id"),
		"database_id": candidate.get("database_id"),
		"location": candidate.get("location") or cached.get("location"),
		"matched_score": round(score, 4),
	}
	if promotion_path:
		recording["music"]["source"] = "managed_import"
		recording["active_reference"] = {"kind": "managed_file", "relative_path": promotion_path}
	else:
		recording["active_reference"] = {"kind": "existing_music", "persistent_id": persistent_id}
	managed = recording.get("managed_file") or {}
	if review_kind == "album_duplicate_conflict" and managed.get("tool_owned"):
		managed["retirement_candidate"] = True
		promotion = recording.setdefault("promotion", {})
		promotion["superseded_by_music_persistent_id"] = persistent_id
		promotion["tool_owned_loose_file_retirement_candidate"] = True
	recording.pop("review", None)
	recording.pop("last_error", None)
	if not scan:
		upsert_music_cache_track(paths, candidate)
	save_manifest(paths, manifest)
	resolved = dict(candidate, score=round(score, 4), reasons=reasons)
	return {
		"recordingId": key,
		"candidate": _music_candidate(recording, resolved, review_kind=review_kind, album_names=album_names, paths=paths),
	}
