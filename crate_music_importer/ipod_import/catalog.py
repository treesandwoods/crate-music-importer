"""Complete Music identities; source recordings are optional links, never identities."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unicodedata
import uuid
from pathlib import Path
from typing import Any


def atomic_json(path: Path, data: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	name = None
	try:
		with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
			name = handle.name
			json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(name, path)
		fd = os.open(path.parent, os.O_RDONLY)
		try:
			os.fsync(fd)
		finally:
			os.close(fd)
	finally:
		if name:
			Path(name).unlink(missing_ok=True)


def library_id(persistent_id: str) -> str:
	if not persistent_id:
		raise ValueError("Catalog registration requires an exact Music persistent ID.")
	return "lib_" + uuid.uuid5(uuid.NAMESPACE_URL, "crate:music:" + persistent_id).hex


def register_tracks(manifest: dict[str, Any], tracks: list[dict[str, Any]], *, complete: bool = False) -> dict[str, Any]:
	"""Pure schema migration/refresh. Never infer rewrite authority from placement."""
	catalog = copy.deepcopy(manifest.get("catalog") or {"version": 1, "entries": {}, "complete": False})
	if catalog.get("version") != 1:
		raise ValueError("Unsupported Crate catalog version.")
	seen: set[str] = set()
	for track in tracks:
		pid = str(track.get("persistent_id") or "")
		key = library_id(pid)
		if pid in seen:
			raise ValueError(f"Duplicate Music persistent ID: {pid}")
		seen.add(pid)
		entry = catalog["entries"].setdefault(key, {
			"library_id": key, "provenance": "Music library", "authority": {"rewrite": False, "replace": False, "delete": False},
			"fingerprints": {}, "recording_ids": [], "duration_ms": None, "format": None, "artwork_present": None,
		})
		entry.update(persistent_id=pid, database_id=str(track.get("database_id") or ""), location=track.get("location"), music={**entry.get("music", {}), **copy.deepcopy(track)}, present_in_music=True)
		entry["duration_ms"] = round(float(track.get("duration_s") or 0) * 1000)
		entry["format"] = Path(str(track.get("location") or "")).suffix.lstrip(".").lower() or None
		if "artwork_sha256" in track:
			entry["artwork_present"] = bool(track["artwork_sha256"])
		for rid, recording in (manifest.get("recordings") or {}).items():
			if pid == str((recording.get("music") or {}).get("persistent_id") or (recording.get("active_reference") or {}).get("persistent_id") or ""):
				if rid not in entry["recording_ids"]:
					entry["recording_ids"].append(rid)
				# A marker and old integrity claim remain evidence to verify, not authority.
				entry["provenance"] = "Crate source history"
				recording["library_id"] = key
	if complete:
		for entry in catalog["entries"].values():
			entry["present_in_music"] = entry["persistent_id"] in seen
		catalog["complete"] = True
	manifest["catalog"] = catalog
	return catalog


def catalog_tracks(manifest: dict[str, Any]) -> list[dict[str, Any]]:
	return [copy.deepcopy(entry["music"]) for entry in manifest.get("catalog", {}).get("entries", {}).values() if entry.get("present_in_music")]


def component(value: str, fallback: str) -> str:
	value = unicodedata.normalize("NFC", str(value or "")).strip()
	value = "".join(" " if ch in '/:\\' or ord(ch) < 32 else ch for ch in value).strip(" .") or fallback
	while len(value.encode("utf-8")) > 180:
		value = value[:-1]
	return value


def canonical_relative_path(track: dict[str, Any], *, suffix: str = "") -> str:
	artist = "Compilations" if track.get("compilation") else component(track.get("album_artist"), "Unknown Artist")
	album = component(track.get("album"), "Unknown Album")
	title = component(track.get("title"), "Untitled")
	number = int(track.get("track_no") or 0)
	disc = int(track.get("disc_no") or 0)
	prefix = f"{disc}-{number:02d} — " if number and (disc > 1 or int(track.get("disc_total") or 0) > 1) else f"{number:02d} — " if number else ""
	return str(Path("Music") / artist / album / (prefix + title + (f" — {suffix}" if suffix else "") + ".mp3"))


def path_key(value: str) -> str:
	return unicodedata.normalize("NFD", value).casefold()


def plan_paths(tracks: list[dict[str, Any]], occupied: set[str]) -> tuple[dict[str, str], list[dict[str, Any]]]:
	"""Reserve names across the entire plan and the case-insensitive filesystem."""
	result: dict[str, str] = {}
	collisions = []
	reserved = {path_key(value) for value in occupied}
	for track in sorted(tracks, key=lambda item: item["persistent_id"]):
		pid = track["persistent_id"]
		candidate = canonical_relative_path(track)
		base = candidate
		index = 0
		while path_key(candidate) in reserved:
			index += 1
			suffix = hashlib.sha256(library_id(pid).encode()).hexdigest()[:10] + (f"-{index}" if index > 1 else "")
			candidate = canonical_relative_path(track, suffix=suffix)
		if candidate != base:
			collisions.append({"persistent_id": pid, "requested": base, "resolved": candidate})
		reserved.add(path_key(candidate))
		result[pid] = candidate
	return result, collisions
