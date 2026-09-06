"""Recording identity normalization and conservative candidate scoring."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable

from crate_music_importer.ipod_import.constants import (
	MUSIC_AMBIGUITY_MARGIN,
	MUSIC_CONFIDENCE_MIN,
)


_VERSION_PATTERNS: tuple[tuple[str, str], ...] = (
	("live", r"\blive\b|\bin concert\b"),
	("acoustic", r"\bacoustic\b|\bunplugged\b"),
	("remix", r"\bremix(?:ed)?\b|\bclub mix\b|\bdub mix\b"),
	("demo", r"\bdemo\b"),
	("instrumental", r"\binstrumental\b"),
	("karaoke", r"\bkaraoke\b"),
	("edit", r"\bradio edit\b|\bsingle edit\b|\bedit\b"),
	("extended", r"\bextended\b|\blong version\b"),
)

_REMASTER_LABEL_FRAGMENT = (
	r"\b(?:(?:19|20)\d{2}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?"
	r"(?:\s+(?:version\s+)?(?:19|20)\d{2})?(?:\s+(?:edition|version))?\b"
)
_REMASTER_LABEL_PATTERN = re.compile(_REMASTER_LABEL_FRAGMENT, re.I)
_RELEASE_LABEL_FRAGMENT = r"""
	(?:the\s+)?(?:
		(?:(?:\d{1,3}(?:st|nd|rd|th)|(?:19|20)\d{2})\s+)?anniversary
			(?:\s+(?:super\s+deluxe|deluxe|expanded|special|collector(?:['’]s)?|legacy))?
			(?:\s+(?:edition|version))?
		|(?:(?:super\s+)?deluxe|expanded|special|collector(?:['’]s)?|legacy|standard|limited|bonus\s+tracks?)
			(?:\s+(?:edition|version))?
		|(?:deluxe|expanded)\s+re-?issue
		|re-?issue(?:d)?
		|(?:original\s+|in\s+)?(?:mono(?:\s*(?:and|&)\s*stereo)?|stereo)(?:\s+(?:mix|version))?
	)
"""
_RELEASE_LABEL_PATTERN = re.compile(_RELEASE_LABEL_FRAGMENT, re.I | re.X)
_NOISE_LABEL_PATTERN = re.compile(
	rf"(?:{_REMASTER_LABEL_FRAGMENT}|{_RELEASE_LABEL_FRAGMENT})",
	re.I | re.X,
)
_BRACKETED_LABEL_PATTERN = re.compile(
	r"(?P<space>\s*)(?P<open>\(|\[)(?P<body>[^)\]]*)(?P<close>\)|\])"
)
_SUFFIX_NOISE_PATTERN = re.compile(
	rf"(?:\s*(?:[-\u2013\u2014:|,;/]\s*)|\s+)(?:{_REMASTER_LABEL_FRAGMENT}|{_RELEASE_LABEL_FRAGMENT})\s*$",
	re.I | re.X,
)


def _tidy_display_fragment(value: str) -> str:
	text = re.sub(r"\s+", " ", value).strip()
	text = re.sub(r"^(?:\s*[-\u2013\u2014:|,;/]+\s*)+", "", text)
	text = re.sub(r"(?:\s*[-\u2013\u2014:|,;/]+\s*)+$", "", text)
	text = re.sub(r"(?:\s*[-\u2013\u2014:|,;/]+\s*){2,}", " - ", text)
	return re.sub(r"\s+", " ", text).strip()


def clean_release_labels(value: str | None) -> str:
	"""Remove release packaging labels while preserving recording versions."""
	original = re.sub(r"\s+", " ", str(value or "")).strip()
	if not original:
		return ""

	def clean_group(match: re.Match[str]) -> str:
		body = match.group("body").strip()
		if _NOISE_LABEL_PATTERN.fullmatch(body):
			return ""
		body = _REMASTER_LABEL_PATTERN.sub("", body)
		body = _RELEASE_LABEL_PATTERN.sub("", body)
		body = _tidy_display_fragment(body)
		if not body:
			return ""
		return f"{match.group('space')}{match.group('open')}{body}{match.group('close')}"

	cleaned = _BRACKETED_LABEL_PATTERN.sub(clean_group, original)
	while True:
		without_suffix = _SUFFIX_NOISE_PATTERN.sub("", cleaned)
		if without_suffix == cleaned:
			break
		cleaned = without_suffix
	cleaned = _REMASTER_LABEL_PATTERN.sub("", cleaned)
	cleaned = re.sub(r"\s*(?:\(\s*\)|\[\s*\])", "", cleaned)
	cleaned = re.sub(r"\s+(\)|\])", r"\1", cleaned)
	cleaned = re.sub(r"(\(|\[)\s+", r"\1", cleaned)
	cleaned = _tidy_display_fragment(cleaned)
	return cleaned or original


def normalize_text(value: str | None) -> str:
	text = unicodedata.normalize("NFKD", str(value or "").casefold())
	text = "".join(char for char in text if not unicodedata.combining(char))
	text = text.replace("&", " and ")
	text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
	return re.sub(r"\s+", " ", text).strip()


def normalize_recording_title(value: str | None) -> str:
	"""Normalize a title while treating release packaging labels as metadata."""
	return normalize_text(clean_release_labels(value))


def version_markers(value: str | None) -> tuple[str, ...]:
	text = normalize_text(value)
	markers = [name for name, pattern in _VERSION_PATTERNS if re.search(pattern, text)]
	return tuple(sorted(markers))


def normalized_artists(value: str | None) -> tuple[str, ...]:
	parts = re.split(r"\s*(?:,|;|/|\bfeat\.?\b|\bfeaturing\b|\bwith\b|\bx\b)\s*", str(value or ""), flags=re.I)
	cleaned = {normalize_text(part) for part in parts if normalize_text(part)}
	return tuple(sorted(cleaned))


def recording_identity(track: dict[str, Any]) -> dict[str, Any]:
	duration_ms = int(track.get("duration_ms") or 0)
	return {
		"title": normalize_recording_title(track.get("title")),
		"artists": list(normalized_artists(track.get("artists"))),
		"versions": list(version_markers(track.get("title"))),
		"duration_bucket_ms": int(round(duration_ms / 2000.0) * 2000) if duration_ms else 0,
	}


def recording_id(track: dict[str, Any]) -> str:
	identity = recording_identity(track)
	material = "|".join((
		identity["title"],
		",".join(identity["artists"]),
		",".join(identity["versions"]),
		str(identity["duration_bucket_ms"]),
	))
	return "rec_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _ratio(left: str, right: str) -> float:
	if not left or not right:
		return 0.0
	if left == right:
		return 1.0
	return SequenceMatcher(None, left, right).ratio()


def _artist_score(wanted: Iterable[str], found: Iterable[str]) -> float:
	wanted_set = set(wanted)
	found_set = set(found)
	if not wanted_set or not found_set:
		return 0.0
	if wanted_set == found_set:
		return 1.0
	best = max((_ratio(left, right) for left in wanted_set for right in found_set), default=0.0)
	overlap = len(wanted_set & found_set) / max(1, len(wanted_set))
	return max(best * 0.85, overlap)


def duration_score(wanted_ms: int, found_seconds: float) -> float:
	if wanted_ms <= 0 or found_seconds <= 0:
		return 0.70
	delta = abs((wanted_ms / 1000.0) - found_seconds)
	if delta <= 2:
		return 1.0
	if delta <= 4:
		return 0.94
	if delta <= 7:
		return 0.82
	if delta <= 12:
		return 0.62
	return 0.0


def score_music_candidate(track: dict[str, Any], candidate: dict[str, Any]) -> tuple[float, list[str]]:
	wanted_versions = set(version_markers(track.get("title")))
	found_versions = set(version_markers(candidate.get("title") or candidate.get("name")))
	reasons: list[str] = []
	if wanted_versions != found_versions:
		return 0.0, [f"version differs: wanted {sorted(wanted_versions)}, found {sorted(found_versions)}"]
	title_score = _ratio(normalize_recording_title(track.get("title")), normalize_recording_title(candidate.get("title") or candidate.get("name")))
	artist_score = _artist_score(normalized_artists(track.get("artists")), normalized_artists(candidate.get("artist")))
	duration = duration_score(int(track.get("duration_ms") or 0), float(candidate.get("duration_s") or candidate.get("duration") or 0))
	# Music metadata sometimes omits the leading article in a band's name.
	# Restrict this equivalence to exact titles and known durations within two seconds;
	# keep persisted recording identities and other provider scoring unchanged.
	if title_score == 1.0 and duration == 1.0:
		wanted_artists = normalized_artists(track.get("artists"))
		found_artists = normalized_artists(candidate.get("artist"))
		def without_article(artists: tuple[str, ...]) -> set[str]:
			return {artist[4:] if artist.startswith("the ") else artist for artist in artists}
		if wanted_artists and found_artists and without_article(wanted_artists) == without_article(found_artists):
			artist_score = 1.0
	album_score = _ratio(
		normalize_text(clean_release_labels(track.get("album"))),
		normalize_text(clean_release_labels(candidate.get("album"))),
	) if track.get("album") else 0.70
	if title_score < 0.90:
		reasons.append(f"title similarity only {title_score:.2f}")
	if artist_score < 0.88:
		reasons.append(f"artist similarity only {artist_score:.2f}")
	if duration < 0.82:
		reasons.append("duration differs")
	score = title_score * 0.48 + artist_score * 0.30 + duration * 0.17 + album_score * 0.05
	return min(1.0, score), reasons


def match_music_track(track: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
	ranked: list[dict[str, Any]] = []
	for candidate in candidates:
		score, reasons = score_music_candidate(track, candidate)
		if score <= 0:
			continue
		item = dict(candidate)
		item["score"] = round(score, 4)
		item["reasons"] = reasons
		ranked.append(item)
	ranked.sort(key=lambda item: float(item["score"]), reverse=True)
	if not ranked or float(ranked[0]["score"]) < MUSIC_CONFIDENCE_MIN:
		return {"status": "missing", "candidate": None, "candidates": ranked[:5]}
	if len(ranked) > 1 and float(ranked[0]["score"]) - float(ranked[1]["score"]) < MUSIC_AMBIGUITY_MARGIN:
		return {"status": "ambiguous", "candidate": None, "candidates": ranked[:5]}
	return {"status": "reused", "candidate": ranked[0], "candidates": ranked[:5]}
