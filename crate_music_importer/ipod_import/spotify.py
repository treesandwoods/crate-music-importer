"""Resolve public Spotify playlist pages without storing credentials."""

from __future__ import annotations

import base64
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class SpotifyError(RuntimeError):
	pass


@dataclass
class SpotifyPlaylist:
	id: str
	name: str
	url: str
	tracks: list[dict[str, Any]]
	total_count: int | None
	complete: bool
	warning: str | None = None
	source_type: str = "playlist"
	album_artist: str = ""
	release_year: int | None = None

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)


_ID_RE = re.compile(r"^[A-Za-z0-9]{16,32}$")


def parse_source_url(value: str, *, expected_type: str | None = None) -> tuple[str, str, str]:
	text = str(value or "").strip()
	parsed = urlparse(text)
	parts = [part for part in parsed.path.split("/") if part]
	if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in ("open.spotify.com", "play.spotify.com"):
		raise SpotifyError("Provide a public Spotify playlist or album URL from open.spotify.com.")
	source_type = parts[0].lower() if parts else ""
	if len(parts) < 2 or source_type not in ("playlist", "album") or not _ID_RE.match(parts[1]):
		raise SpotifyError("The Spotify URL is not a valid playlist or album URL.")
	if expected_type and source_type != expected_type:
		raise SpotifyError(f"The Spotify URL is not a valid {expected_type} URL.")
	source_id = parts[1]
	return source_type, source_id, f"https://open.spotify.com/{source_type}/{source_id}"


def parse_playlist_url(value: str) -> tuple[str, str]:
	_, source_id, canonical_url = parse_source_url(value, expected_type="playlist")
	return source_id, canonical_url


def parse_album_url(value: str) -> tuple[str, str]:
	_, source_id, canonical_url = parse_source_url(value, expected_type="album")
	return source_id, canonical_url


def load_fixture(path: Path) -> SpotifyPlaylist:
	with path.open("r", encoding="utf-8") as handle:
		data = json.load(handle)
	tracks = list(data.get("tracks") or [])
	total = data.get("total_count")
	complete = bool(data.get("complete", total is None or int(total) == len(tracks)))
	return SpotifyPlaylist(
		id=str(data["id"]),
		name=str(data.get("name") or ("Spotify Album" if data.get("source_type") == "album" else "Spotify Playlist")),
		url=str(data.get("url") or f"https://open.spotify.com/{data.get('source_type') or 'playlist'}/{data['id']}"),
		tracks=tracks,
		total_count=int(total) if total is not None else len(tracks),
		complete=complete,
		warning=data.get("warning"),
		source_type=str(data.get("source_type") or "playlist"),
		album_artist=str(data.get("album_artist") or ""),
		release_year=_integer(data.get("release_year")),
	)


def _get(url: str, timeout: int) -> str:
	request = Request(url, headers={
		"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
		"Accept": "text/html,application/xhtml+xml",
		"Accept-Language": "en-US,en;q=0.9",
	})
	try:
		with urlopen(request, timeout=timeout) as response:
			return response.read().decode("utf-8", errors="replace")
	except HTTPError as exc:
		if exc.code in (401, 403, 404):
			raise SpotifyError("Spotify could not expose this playlist. It may be private, unavailable, or region-restricted.") from exc
		raise SpotifyError(f"Spotify returned HTTP {exc.code}.") from exc
	except URLError as exc:
		raise SpotifyError(f"Could not reach Spotify: {exc.reason}") from exc


def fetch_playlist(
	value: str,
	*,
	timeout: int = 25,
	known_track_covers: dict[str, str] | None = None,
) -> SpotifyPlaylist:
	playlist_id, canonical_url = parse_playlist_url(value)
	playlist = _fetch_public_source((
		(canonical_url, _parse_initial_page),
		(f"https://open.spotify.com/embed/playlist/{playlist_id}", _parse_embed_page),
	), "playlist", playlist_id, canonical_url, timeout)
	_enrich_playlist_covers(playlist.tracks, timeout=timeout, known_track_covers=known_track_covers)
	return playlist


def fetch_album(value: str, *, timeout: int = 25) -> SpotifyPlaylist:
	album_id, canonical_url = parse_album_url(value)
	return _fetch_public_source((
		(canonical_url, _parse_initial_album_page),
		(f"https://open.spotify.com/embed/album/{album_id}", _parse_embed_album_page),
	), "album", album_id, canonical_url, timeout)


def _fetch_public_source(
	sources: tuple[tuple[str, Callable[[str, str, str], dict[str, Any]]], ...],
	source_type: str,
	source_id: str,
	canonical_url: str,
	timeout: int,
	*,
	attempts: int = 3,
) -> SpotifyPlaylist:
	"""Retry Spotify's occasionally truncated public embed before reporting an incomplete source."""
	pages: list[dict[str, Any]] = []
	errors: list[str] = []
	latest: SpotifyPlaylist | None = None
	for _attempt in range(max(1, attempts)):
		for url, parser in sources:
			try:
				pages.append(parser(_get(url, timeout), source_id, canonical_url))
			except SpotifyError as exc:
				errors.append(str(exc))
		if not pages:
			continue
		latest = _finalize_source(pages, errors, source_type, source_id, canonical_url)
		if latest.complete:
			return latest
	if latest is None:
		raise SpotifyError(errors[-1] if errors else f"Spotify returned no {source_type} data.")
	return latest


def _finalize_source(
	pages: list[dict[str, Any]],
	errors: list[str],
	source_type: str,
	source_id: str,
	canonical_url: str,
) -> SpotifyPlaylist:
	if not pages:
		raise SpotifyError(errors[-1] if errors else f"Spotify returned no {source_type} data.")
	best = max(pages, key=lambda item: len(item.get("tracks") or []))
	tracks = [dict(track) for track in best["tracks"]]
	metadata_by_id: dict[str, dict[str, Any]] = {}
	for page in pages:
		for track in page.get("tracks") or []:
			if track.get("sp_id"):
				metadata_by_id.setdefault(str(track["sp_id"]), {}).update({key: value for key, value in track.items() if value not in (None, "", 0)})
	for position, track in enumerate(tracks, start=1):
		richer = metadata_by_id.get(str(track.get("sp_id") or ""), {})
		for key in ("album", "album_artist", "isrc", "cover_url", "duration_ms", "track_no", "disc_no", "release_year"):
			if track.get(key) in (None, "", 0) and richer.get(key) not in (None, "", 0):
				track[key] = richer[key]
		track["position"] = position
	known_totals = [int(item.get("total_count") or 0) for item in pages]
	total = max([len(tracks), *known_totals])
	complete = len(tracks) >= total and (source_type != "playlist" or len(tracks) != 100)
	warning = None
	if not complete:
		warning = (
			f"Spotify exposed {len(tracks)} of {total} reported tracks. The importer will not download an incomplete {source_type}. "
			f"Retry after Spotify exposes the full ordered {source_type}."
		)
	album_artist = _clean(best.get("album_artist"))
	release_year = _integer(best.get("release_year"))
	if source_type == "album":
		disc_total = max((_integer(track.get("disc_no")) or 1 for track in tracks), default=1)
		for track in tracks:
			track["album"] = _clean(track.get("album")) or _clean(best.get("name"))
			track["album_artist"] = _clean(track.get("album_artist")) or album_artist or _clean(track.get("artists"))
			track["track_no"] = _integer(track.get("track_no")) or int(track["position"])
			track["track_total"] = total
			track["disc_no"] = _integer(track.get("disc_no")) or 1
			track["disc_total"] = disc_total
			track["release_year"] = _integer(track.get("release_year")) or release_year
			track["album_id"] = source_id
	return SpotifyPlaylist(
		source_id,
		best["name"],
		canonical_url,
		tracks,
		total,
		complete,
		warning,
		source_type=source_type,
		album_artist=album_artist,
		release_year=release_year,
	)


def _parse_initial_page(text: str, playlist_id: str, canonical_url: str) -> dict[str, Any]:
	match = re.search(r'<script[^>]+id=["\']initialState["\'][^>]*>(.*?)</script>', text, re.S | re.I)
	if not match:
		raise SpotifyError("Spotify did not include public playlist state in its page.")
	try:
		state = json.loads(base64.b64decode(html.unescape(match.group(1).strip())).decode("utf-8", errors="replace"))
	except Exception as exc:
		raise SpotifyError("Spotify returned playlist state in an unexpected format.") from exc
	items = state.get("entities", {}).get("items", {})
	entity = items.get(f"spotify:playlist:{playlist_id}") if isinstance(items, dict) else None
	if not isinstance(entity, dict):
		for candidate in items.values() if isinstance(items, dict) else []:
			if isinstance(candidate, dict) and candidate.get("id") == playlist_id:
				entity = candidate
				break
	if not isinstance(entity, dict):
		raise SpotifyError("Spotify could not find the public playlist in its page state.")
	content = entity.get("content") if isinstance(entity.get("content"), dict) else {}
	rows = content.get("items") if isinstance(content.get("items"), list) else []
	return {
		"name": _clean(entity.get("name")) or "Spotify Playlist",
		"tracks": _tracks_from_initial(rows),
		"total_count": _integer(content.get("totalCount")),
		"url": canonical_url,
	}


def _parse_initial_album_page(text: str, album_id: str, canonical_url: str) -> dict[str, Any]:
	match = re.search(r'<script[^>]+id=["\']initialState["\'][^>]*>(.*?)</script>', text, re.S | re.I)
	if not match:
		raise SpotifyError("Spotify did not include public album state in its page.")
	try:
		state = json.loads(base64.b64decode(html.unescape(match.group(1).strip())).decode("utf-8", errors="replace"))
	except Exception as exc:
		raise SpotifyError("Spotify returned album state in an unexpected format.") from exc
	items = state.get("entities", {}).get("items", {})
	entity = items.get(f"spotify:album:{album_id}") if isinstance(items, dict) else None
	if not isinstance(entity, dict):
		for candidate in items.values() if isinstance(items, dict) else []:
			if isinstance(candidate, dict) and candidate.get("id") == album_id:
				entity = candidate
				break
	if not isinstance(entity, dict):
		raise SpotifyError("Spotify could not find the public album in its page state.")
	name = _clean(entity.get("name")) or "Spotify Album"
	content = entity.get("tracksV2") if isinstance(entity.get("tracksV2"), dict) else {}
	rows = content.get("items") if isinstance(content.get("items"), list) else []
	cover = entity.get("coverArt") if isinstance(entity.get("coverArt"), dict) else {}
	cover_url = _best_image(cover.get("sources", []))
	album_artist = _artists(entity.get("artists"))
	release_year = _release_year(entity)
	return {
		"name": name,
		"album_artist": album_artist,
		"release_year": release_year,
		"tracks": _tracks_from_initial(
			rows,
			album_name=name,
			album_artist=album_artist,
			cover_url=cover_url,
			release_year=release_year,
		),
		"total_count": _integer(content.get("totalCount")),
		"url": canonical_url,
	}


def _parse_embed_page(text: str, playlist_id: str, canonical_url: str) -> dict[str, Any]:
	match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, re.S | re.I)
	if not match:
		raise SpotifyError("Spotify embed did not include playlist data.")
	try:
		data = json.loads(html.unescape(match.group(1).strip()))
	except Exception as exc:
		raise SpotifyError("Spotify embed data had an unexpected format.") from exc
	entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity")
	if not isinstance(entity, dict) or entity.get("id") != playlist_id:
		raise SpotifyError("Spotify embed returned the wrong playlist.")
	rows = entity.get("trackList") if isinstance(entity.get("trackList"), list) else []
	return {
		"name": _clean(entity.get("title") or entity.get("name")) or "Spotify Playlist",
		# A playlist embed exposes only the playlist cover, not each recording's
		# album cover. Never attach that shared image to track metadata.
		"tracks": _tracks_from_embed(rows, None),
		"total_count": _integer(entity.get("trackCount") or entity.get("totalCount")),
		"url": canonical_url,
	}


def _parse_embed_album_page(text: str, album_id: str, canonical_url: str) -> dict[str, Any]:
	match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, re.S | re.I)
	if not match:
		raise SpotifyError("Spotify embed did not include album data.")
	try:
		data = json.loads(html.unescape(match.group(1).strip()))
	except Exception as exc:
		raise SpotifyError("Spotify album embed data had an unexpected format.") from exc
	entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity")
	if not isinstance(entity, dict) or entity.get("id") != album_id:
		raise SpotifyError("Spotify embed returned the wrong album.")
	rows = entity.get("trackList") if isinstance(entity.get("trackList"), list) else []
	cover_url = _best_image(entity.get("coverArt", {}).get("sources", [])) if isinstance(entity.get("coverArt"), dict) else None
	name = _clean(entity.get("title") or entity.get("name")) or "Spotify Album"
	album_artist = _clean(entity.get("subtitle"))
	return {
		"name": name,
		"album_artist": album_artist,
		"release_year": _release_year(entity),
		"tracks": _tracks_from_embed(rows, cover_url, album_name=name, album_artist=album_artist),
		"total_count": _integer(entity.get("trackCount") or entity.get("totalCount")),
		"url": canonical_url,
	}


def _tracks_from_initial(
	rows: list[Any],
	*,
	album_name: str = "",
	album_artist: str = "",
	cover_url: str | None = None,
	release_year: int | None = None,
) -> list[dict[str, Any]]:
	tracks: list[dict[str, Any]] = []
	for position, row in enumerate(rows, start=1):
		if not isinstance(row, dict):
			continue
		data = None
		for candidate in (
			row.get("itemV2", {}).get("data") if isinstance(row.get("itemV2"), dict) else None,
			row.get("item", {}).get("data") if isinstance(row.get("item"), dict) else None,
			row.get("track"),
		):
			if isinstance(candidate, dict):
				data = candidate
				break
		if not data:
			continue
		title = _clean(data.get("name") or data.get("title"))
		artists = _artists(data.get("artists"))
		if not title or not artists:
			continue
		album = data.get("albumOfTrack") or data.get("album")
		album = album if isinstance(album, dict) else {}
		cover = album.get("coverArt") if isinstance(album.get("coverArt"), dict) else {}
		tracks.append({
			"position": position,
			"title": title,
			"artists": artists,
			"album": album_name or _clean(album.get("name")),
			"album_artist": album_artist,
			"duration_ms": _duration_ms(data),
			"sp_id": _spotify_track_id(data.get("uri") or data.get("id")),
			"isrc": _external_id(data, "isrc"),
			"cover_url": cover_url or _best_image(cover.get("sources", [])),
			"track_no": _integer(data.get("trackNumber")) or position,
			"disc_no": _integer(data.get("discNumber")) or 1,
			"release_year": release_year,
		})
	return tracks


def _tracks_from_embed(
	rows: list[Any],
	cover_url: str | None,
	*,
	album_name: str = "",
	album_artist: str = "",
) -> list[dict[str, Any]]:
	tracks: list[dict[str, Any]] = []
	for position, row in enumerate(rows, start=1):
		if not isinstance(row, dict) or row.get("entityType") not in (None, "track"):
			continue
		title = _clean(row.get("title"))
		artists = _clean(row.get("subtitle")).replace("\u00a0", " ")
		if title and artists:
			tracks.append({
				"position": position,
				"title": title,
				"artists": artists,
				"album": album_name,
				"album_artist": album_artist,
				"duration_ms": _integer(row.get("duration")) or 0,
				"sp_id": _spotify_track_id(row.get("uri")),
				"isrc": None,
				"cover_url": cover_url,
				"track_no": position,
				"disc_no": 1,
			})
	return tracks


def _track_cover(track_id: str, timeout: int) -> str | None:
	try:
		data = json.loads(_get(
			f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{track_id}",
			min(timeout, 15),
		))
	except (SpotifyError, json.JSONDecodeError):
		return None
	cover_url = _clean(data.get("thumbnail_url")) if isinstance(data, dict) else ""
	return cover_url if cover_url.startswith(("https://", "http://")) else None


def _enrich_playlist_covers(
	tracks: list[dict[str, Any]],
	*,
	timeout: int,
	known_track_covers: dict[str, str] | None = None,
) -> None:
	known_track_covers = known_track_covers or {}
	missing_by_id: dict[str, list[dict[str, Any]]] = {}
	for track in tracks:
		if track.get("cover_url"):
			continue
		track_id = str(track.get("sp_id") or "")
		known_cover = str(known_track_covers.get(track_id) or "")
		if known_cover.startswith(("https://", "http://")):
			track["cover_url"] = known_cover
			continue
		if track_id:
			missing_by_id.setdefault(track_id, []).append(track)
	if not missing_by_id:
		return
	workers = min(8, len(missing_by_id))
	with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="spotify-artwork") as executor:
		futures = {executor.submit(_track_cover, track_id, timeout): track_id for track_id in missing_by_id}
		for future in as_completed(futures):
			track_id = futures[future]
			cover_url = future.result()
			if cover_url:
				for track in missing_by_id[track_id]:
					track["cover_url"] = cover_url
	missing_titles = [str(track.get("title") or track.get("sp_id") or "unknown track") for track in tracks if not track.get("cover_url")]
	if missing_titles:
		preview = ", ".join(missing_titles[:3])
		if len(missing_titles) > 3:
			preview += f", and {len(missing_titles) - 3} more"
		raise SpotifyError(
			"Spotify did not expose individual album artwork for every playlist track "
			f"({preview}). Retry before downloading; playlist artwork will not be substituted."
		)


def _artists(value: Any) -> str:
	items = value.get("items") if isinstance(value, dict) else value
	names: list[str] = []
	for item in items if isinstance(items, list) else []:
		if not isinstance(item, dict):
			continue
		profile = item.get("profile") if isinstance(item.get("profile"), dict) else item
		name = _clean(profile.get("name"))
		if name:
			names.append(name)
	return ", ".join(names)


def _duration_ms(data: dict[str, Any]) -> int:
	value = data.get("duration") or data.get("durationMs") or data.get("duration_ms")
	if isinstance(value, dict):
		value = value.get("totalMilliseconds") or value.get("milliseconds") or value.get("ms")
	return _integer(value) or 0


def _external_id(data: dict[str, Any], name: str) -> str | None:
	values = data.get("externalIds") or data.get("external_ids")
	value = _clean(values.get(name) or values.get(name.upper())) if isinstance(values, dict) else ""
	return value or None


def _spotify_track_id(value: Any) -> str | None:
	text = _clean(value)
	if text.startswith("spotify:track:"):
		return text.rsplit(":", 1)[-1]
	return text if _ID_RE.match(text) else None


def _best_image(values: Any) -> str | None:
	best: tuple[int, str] | None = None
	for value in values if isinstance(values, list) else []:
		if not isinstance(value, dict) or not _clean(value.get("url")):
			continue
		size = max(_integer(value.get("width")) or 0, _integer(value.get("height")) or 0)
		if best is None or size > best[0]:
			best = (size, _clean(value.get("url")))
	return best[1] if best else None


def _integer(value: Any) -> int | None:
	try:
		return int(value)
	except (TypeError, ValueError):
		return None


def _release_year(value: Any) -> int | None:
	if not isinstance(value, dict):
		return None
	for candidate in (
		value.get("releaseYear"),
		value.get("year"),
		value.get("date", {}).get("year") if isinstance(value.get("date"), dict) else None,
		value.get("date", {}).get("isoString") if isinstance(value.get("date"), dict) else value.get("date"),
		value.get("releaseDate", {}).get("isoString") if isinstance(value.get("releaseDate"), dict) else value.get("releaseDate"),
	):
		match = re.match(r"^((?:19|20)\d{2})", str(candidate or ""))
		if match:
			return int(match.group(1))
	return None


def _clean(value: Any) -> str:
	return re.sub(r"\s+", " ", str(value or "")).strip()
