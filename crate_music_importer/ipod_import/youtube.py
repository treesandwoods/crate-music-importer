"""High-confidence YouTube search using the installed yt-dlp executable."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from crate_music_importer.ipod_import.constants import YOUTUBE_CONFIDENCE_MIN
from crate_music_importer.ipod_import.identity import clean_release_labels, duration_score, normalize_recording_title, normalize_text, normalized_artists, version_markers


class YouTubeError(RuntimeError):
	pass


_BAD_TERMS = ("cover", "tribute", "karaoke", "nightcore", "sped up", "slowed", "reverb", "8d audio", "reaction", "mashup", "medley")


def ytdlp_path() -> str:
	for candidate in (shutil.which("yt-dlp"), "/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp"):
		if candidate and Path(candidate).is_file():
			return str(candidate)
	raise YouTubeError("yt-dlp is required. Install the prerequisites in the README, then run Update Dependencies in Raycast.")


def _run_json(arguments: list[str], *, timeout: float = 120) -> dict[str, Any]:
	command = [ytdlp_path(), "--no-warnings", "--dump-single-json", *arguments]
	try:
		result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
	except subprocess.TimeoutExpired as exc:
		raise YouTubeError("YouTube search timed out.") from exc
	if result.returncode != 0:
		# yt-dlp can emit usable entries alongside an individual extraction failure.
		try:
			partial = json.loads(result.stdout)
			if isinstance(partial, dict) and partial.get("entries"):
				partial["_partial_error"] = (result.stderr or "Some YouTube entries could not be retrieved").strip().splitlines()[-1]
				return partial
		except (json.JSONDecodeError, TypeError):
			pass
		detail = (result.stderr or result.stdout or "unknown yt-dlp error").strip().splitlines()[-1]
		lower = detail.casefold()
		if "age" in lower:
			raise YouTubeError("The YouTube result is age-restricted and cannot be used without approved browser cookies.")
		if "not available in your country" in lower or "geo" in lower:
			raise YouTubeError("The YouTube result is not available in this region.")
		if "private" in lower:
			raise YouTubeError("The YouTube result is private.")
		raise YouTubeError(f"yt-dlp failed: {detail}")
	try:
		return json.loads(result.stdout)
	except json.JSONDecodeError as exc:
		raise YouTubeError("yt-dlp returned invalid search metadata.") from exc


def _clean_candidate_title(title: str, artists: str) -> str:
	# A presentation label followed by a pipe commonly introduces release or
	# channel branding. Version/altered-audio checks still inspect the raw title.
	text = re.sub(r"([\[(](?:official\s+)?(?:audio|music video|video)[\])])\s*\|.*$", r"\1", str(title or ""), flags=re.I)
	text = re.sub(r"\s*[-–—]?\s*(?:deluxe|expanded|special) edition version\s*$", "", text, flags=re.I)
	text = clean_release_labels(text)
	text = re.sub(r"[\[(]\s*(?:with|feat\.?|ft\.?|featuring)\s+[^)\]]+[)\]]", "", text, flags=re.I)
	text = re.sub(r"[\[(]\s*(?:official\s+)?(?:music\s+video|audio|video|lyrics?|lyric video|visuali[sz]er|hd|hq|4k)\s*[\])]", " ", text, flags=re.I)
	text = re.sub(r"\s+(?:official\s+(?:music\s+)?(?:audio|video)|lyric video|lyrics|visuali[sz]er|HD|HQ|4K)\s*$", "", text, flags=re.I)
	parts = re.split(r"\s*[-–—:|]\s*", text)
	def is_credit(value: str) -> bool:
		value = normalize_text(value)
		found = False
		for artist in sorted(normalized_artists(artists), key=len, reverse=True):
			if _contains(value, artist):
				value = re.sub(rf"(?<!\w){re.escape(artist)}(?!\w)", " ", value)
				found = True
		return found and not re.sub(r"\b(?:and|feat|ft|featuring|with|x)\b", "", value).strip()
	if len(parts) > 1 and is_credit(parts[0]): text = " - ".join(parts[1:])
	elif len(parts) > 1 and is_credit(parts[-1]): text = " - ".join(parts[:-1])
	# Strip artist credits only at title boundaries; never change shared identity.
	for artist in sorted(normalized_artists(artists), key=len, reverse=True):
		pattern = r"[\W_]+".join(re.escape(word) for word in artist.split())
		text = re.sub(rf"^\s*{pattern}\s*[-–—:|]\s*|\s*[-–—:|]\s*{pattern}\s*$", "", text, flags=re.I)
	text = re.sub(r"\s*[\[(]?\b(?:feat\.?|ft\.?|featuring)\s+[^)\]]+[)\]]?", "", text, flags=re.I)
	return clean_release_labels(re.sub(r"\s+", " ", text).strip(" -–—:|"))


def _similarity(left: str, right: str, *, recording_title: bool = False) -> float:
	from difflib import SequenceMatcher
	normalize = normalize_recording_title if recording_title else normalize_text
	left_norm, right_norm = normalize(left), normalize(right)
	return SequenceMatcher(None, left_norm, right_norm).ratio() if left_norm and right_norm else 0.0


def _contains(text: str, phrase: str) -> bool:
	return bool(phrase and f" {phrase} " in f" {text} ")


def evaluate_candidate(track: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
	item = dict(candidate)
	wanted = _clean_candidate_title(str(track.get("title") or ""), "")
	raw = str(item.get("title") or "")
	clean = _clean_candidate_title(str(item.get("track") or raw), str(track.get("artists") or ""))
	title = _similarity(wanted, clean, recording_title=True)
	artists = normalized_artists(track.get("artists"))
	uploader = normalize_text(re.sub(r"\s*(?:-\s*topic|vevo|official)\s*$", "", str(item.get("uploader") or ""), flags=re.I))
	structured = normalized_artists(item.get("artist"))
	primary = normalize_text(re.split(r",|;|\bfeat\.?\s", str(track.get("artists") or ""), maxsplit=1)[0])
	def artist_evidence(a: str) -> float:
		if a in structured: value = 1.0
		elif _contains(normalize_text(raw), a): value = 0.94
		elif a == uploader: value = 0.90
		else:
			# Group-name variants support review, never automatic selection.
			alias = re.sub(r"^(?:the )| (?:band|quartet)$", "", a).strip()
			value = 0.85 if len(alias.split()) >= 2 and alias == uploader else 0.0
		return value if a == primary else min(value, 0.82)
	artist = max((artist_evidence(a) for a in artists), default=0.0)
	# Remove the actual requested name first: 'Live and Let Die' and artist
	# names such as Live must not manufacture a recording-version conflict.
	context = normalize_text(_clean_candidate_title(raw, str(track.get("artists") or "")))
	phrase = normalize_text(wanted)
	if phrase:
		context = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", context, count=1)
	presentation_tail = re.search(r"[\[(](?:official\s+)?(?:audio|music video|video)[\])]\s*\|(.+)$", raw, re.I)
	if presentation_tail:
		context += " " + normalize_text(presentation_tail.group(1))
	# Only marked version clauses in the request establish a desired version.
	# Bare words in the song name are removed from upload context above.
	clauses = re.findall(r"[\[(]([^\])]+)[\])]|\s[-–—|]\s(.+)$", wanted)
	version_context = " ".join(part for clause in clauses for part in clause if part)
	wanted_versions = set(version_markers(version_context))
	wanted_versions |= {term for term in _BAD_TERMS if _contains(normalize_text(version_context), term)}
	found_versions = set(version_markers(context))
	conflict = bool(found_versions - wanted_versions)
	# An explicitly requested version must also be evidenced by the upload.
	clean_versions = set(version_markers(clean)) | {term for term in _BAD_TERMS if _contains(normalize_text(clean), term)}
	conflict |= bool(wanted_versions - clean_versions)
	bad = [term for term in _BAD_TERMS if _contains(normalize_text(context), term) and term not in wanted_versions]
	uploader_context = normalize_text(str(item.get("uploader") or ""))
	for a in artists:
		uploader_context = uploader_context.replace(a, " ")
	bad += [term for term in _BAD_TERMS if _contains(normalize_text(uploader_context), term) and term not in wanted_versions]
	seconds = float(item.get("duration_s") or 0)
	expected = float(track.get("duration_ms") or 0) / 1000
	delta = seconds - expected if seconds > 0 and expected > 0 else None
	duration = duration_score(int(expected * 1000), seconds)
	score = min(0.99, title * 0.55 + artist * 0.30 + duration * 0.15) if not conflict and not bad else 0.0
	short = len(normalize_recording_title(wanted).split()) <= 2
	relevant = title >= 0.80 and (not short or title == 1.0) and artist >= 0.80 and not conflict and not bad
	verified = bool(item.get("metadata_verified") and seconds > 0 and raw and item.get("video_id"))
	automatic = relevant and score > YOUTUBE_CONFIDENCE_MIN and title >= 0.92 and artist >= 0.90 and verified and delta is not None and abs(delta) <= 7
	reasons = []
	if title < 0.92 or (short and title != 1): reasons.append(f"title similarity {title:.2f}" + ("; short titles require an exact match" if short else ""))
	if artist < 0.80: reasons.append("no meaningful artist evidence")
	elif artist < 0.90: reasons.append("partial artist/group-name evidence; review only")
	if conflict: reasons.append("incompatible recording version")
	if bad: reasons.append("undesired cover/altered recording: " + ", ".join(bad))
	if delta is None: reasons.append("duration missing")
	elif abs(delta) > 7: reasons.append(f"duration differs by {delta:+.1f}s; review only")
	if not verified: reasons.append("full metadata unverified or incomplete; review only")
	item.update(score=round(score, 4), reasons=reasons, automatic_eligible=automatic, review_relevant=relevant,
		matching_evidence={"title_similarity": title, "artist_evidence": artist, "duration_difference_s": delta, "version_agreement": not conflict, "metadata_verified": verified})
	return item


def score_candidate(track: dict[str, Any], candidate: dict[str, Any]) -> tuple[float, list[str]]:
	item = evaluate_candidate(track, candidate)
	return item["score"], item["reasons"]


def rank_candidates(track: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
	unique = {str(c.get("video_id") or c.get("url")): evaluate_candidate(track, c) for c in candidates}
	relevant = [c for c in unique.values() if c["review_relevant"]]
	return sorted(relevant, key=lambda c: (c["matching_evidence"]["title_similarity"], c["matching_evidence"]["artist_evidence"], -abs(c["matching_evidence"]["duration_difference_s"] if c["matching_evidence"]["duration_difference_s"] is not None else 1e9)), reverse=True)[:10]


class SearchResults(list):
	def __init__(self, candidates=(), summary=None):
		super().__init__(candidates)
		self.summary = summary or {}


def _entry(entry: dict[str, Any], *, verified: bool = False) -> dict[str, Any]:
	return {"video_id": str(entry["id"]), "url": entry.get("webpage_url") or f"https://www.youtube.com/watch?v={entry['id']}",
		"title": entry.get("title") or "", "uploader": entry.get("uploader") or entry.get("channel") or "",
		"duration_s": float(entry.get("duration") or 0), "track": entry.get("track"),
		"artist": entry.get("artist") or ", ".join(entry.get("artists") or []), "metadata_verified": verified}


def fallback_queries(track: dict[str, Any], *, more: bool = False) -> list[str]:
	title = _clean_candidate_title(str(track.get("title") or ""), "")
	artist = re.split(r",|;|\bfeat\.?\s", str(track.get("artists") or ""), maxsplit=1)[0].strip()
	album = clean_release_labels(track.get("album") or track.get("original_album"))
	if more: title = f'"{title}"' if title else ""
	return list(dict.fromkeys(" ".join(filter(None, parts)) for parts in ((title, artist), (artist, title, "official audio"), (title, artist, album))))


def search_candidates(track: dict[str, Any], *, limit: int = 8, query: str | None = None, more: bool = False) -> list[dict[str, Any]]:
	started = time.monotonic()
	pool: dict[str, dict[str, Any]] = {}
	errors: list[str] = []
	queries: list[str] = []
	def search(q: str, count: int, timeout: float) -> list[dict[str, Any]]:
		data = _run_json(["--ignore-errors", "--flat-playlist", "--playlist-end", str(count), f"ytsearch{count}:{q}"], timeout=timeout)
		if data.get("_partial_error"): errors.append(data["_partial_error"])
		return [_entry(e) for e in (data.get("entries") or []) if isinstance(e, dict) and e.get("id")]
	def merge(items: list[dict[str, Any]]) -> None:
		for item in items:
			if not pool.get(item["video_id"], {}).get("metadata_verified"): pool[item["video_id"]] = item
	def inspect(item: dict[str, Any], timeout: float) -> dict[str, Any]:
		data = _run_json(["--no-playlist", item["url"]], timeout=timeout)
		if str(data.get("id") or "") != item["video_id"]: raise YouTubeError("Full metadata did not identify the requested video")
		return _entry(data, verified=True)
	initial = str(query or "").strip() or f"{track.get('artists', '')} {track.get('title', '')}".strip()
	if not more:
		queries.append(initial)
		try: merge(search(initial, limit, 120))
		except YouTubeError as exc: errors.append(str(exc))
		ranked = rank_candidates(track, list(pool.values()))
		if ranked:
			proposed = evaluate_candidate(track, dict(ranked[0], metadata_verified=True))
			if proposed["automatic_eligible"]:
				try: merge([inspect(ranked[0], 30)])
				except YouTubeError as exc: errors.append(str(exc))
	if more or not any(evaluate_candidate(track, c)["automatic_eligible"] for c in pool.values()):
		fallback_start = time.monotonic()
		deadline = fallback_start + 30
		executor = ThreadPoolExecutor(max_workers=2)
		pending = {}
		queue = [q for q in fallback_queries(track, more=more) if q not in queries]
		try:
			while queue or pending:
				while queue and len(pending) < 2 and time.monotonic() < deadline:
					q = queue.pop(0); queries.append(q)
					pending[executor.submit(search, q, 10, max(0.001, deadline - time.monotonic()))] = q
				remaining = deadline - time.monotonic()
				if remaining <= 0: errors.append("fallback deadline reached"); break
				if not pending: break
				done, _ = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
				for future in done:
					pending.pop(future)
					try: merge(future.result())
					except Exception as exc: errors.append(str(exc))
			promising = [evaluate_candidate(track, c) for c in pool.values()]
			promising = [c for c in promising if c["score"] > 0 and c["matching_evidence"]["title_similarity"] >= 0.80]
			promising.sort(key=lambda c: (bool(c.get("metadata_verified")), bool(c.get("duration_s")), abs(c["matching_evidence"]["title_similarity"] - 0.92)))
			for item in promising[:5]:
				if item.get("metadata_verified"): continue
				remaining = deadline - time.monotonic()
				if remaining <= 0: errors.append("fallback deadline reached"); break
				try: merge([inspect(item, remaining)])
				except YouTubeError as exc: errors.append(str(exc))
		finally:
			for future in pending: future.cancel()
			executor.shutdown(wait=False, cancel_futures=True)
		fallback_seconds = time.monotonic() - fallback_start
	else: fallback_seconds = 0
	ranked = rank_candidates(track, list(pool.values()))
	summary = {"status": "partial" if errors else "no_results" if not pool else "no_relevant" if not ranked else "complete", "returned": len(pool), "relevant": len(ranked), "queries": queries, "errors": errors, "elapsed_seconds": time.monotonic() - started, "fallback_seconds": fallback_seconds, "rejected_video_ids": [c["video_id"] for c in pool.values() if not evaluate_candidate(track, c)["review_relevant"]]}
	return SearchResults(ranked, summary)


def choose_candidate(track: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
	ranked = rank_candidates(track, candidates)
	winner = next((c for c in ranked if c["automatic_eligible"]), None)
	return {"status": "matched" if winner else "missing", "candidate": winner, "candidates": ranked, "search_summary": getattr(candidates, "summary", {})}


def inspect_manual_url(url: str) -> dict[str, Any]:
	parsed = urlparse(str(url or "").strip())
	host = (parsed.hostname or "").casefold()
	if parsed.scheme not in ("http", "https") or host not in ("youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"):
		raise YouTubeError("The chosen source must be a YouTube or YouTube Music video URL.")
	data = _run_json(["--no-playlist", str(url)])
	video_id = str(data.get("id") or "")
	if not video_id:
		raise YouTubeError("The selected YouTube URL did not resolve to a video.")
	return {
		"video_id": video_id,
		"url": data.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
		"title": data.get("title") or "",
		"uploader": data.get("uploader") or data.get("channel") or "",
		"duration_s": float(data.get("duration") or 0),
		"score": None,
		"reasons": ["selected manually after review"],
		"selected_by": "manual_url",
	}
