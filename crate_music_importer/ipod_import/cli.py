"""Command-line entry point suitable for Terminal and Raycast."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from crate_music_importer.ipod_import.constants import MANAGED_ROOT
from crate_music_importer.ipod_import.manifest import ManagedPaths, backfill_playlist_urls, load_manifest, save_manifest, update_manifest
from crate_music_importer.ipod_import.media import check_tools
from crate_music_importer.ipod_import.music import load_music_fixture, lookup_music_track, music_binding_id, scan_music_library, scan_music_library_for_health, verify_music_tracks
from crate_music_importer.ipod_import.music_cache import (
	load_music_cache,
	music_cache_tracks,
	remove_music_cache_tracks,
	refresh_music_cache,
	upsert_music_cache_track,
)
from crate_music_importer.ipod_import.pipeline import (
	apply_album_to_music,
	apply_to_music,
	build_album_library_preview,
	build_album_preview,
	build_preview,
	execute_album_import,
	execute_import,
	promote_recording,
)
from crate_music_importer.ipod_import.playlist_update import (
	apply_playlist_update,
	refresh_playlist_covers,
	build_playlist_update_preview,
	save_pending_update,
	saved_playlists,
)
from crate_music_importer.ipod_import.resolver import (
	resolve_music as resolve_music_choice,
	resolve_youtube as resolve_youtube_choice,
	resolver_snapshot,
	search_youtube as search_youtube_choices,
)
from crate_music_importer.ipod_import.spotify import fetch_album, fetch_playlist, fetch_playlist_cover, load_fixture, parse_album_url, parse_playlist_url, parse_source_url


def _parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		prog="crate-music-importer",
		description="Preview, download, and deliberately apply public Spotify albums and playlists to Music.app for normal whole-library iPod sync.",
	)
	subparsers = parser.add_subparsers(dest="command", required=True)

	preview = subparsers.add_parser("preview", aliases=["dry-run"], help="Read Spotify and Music metadata without downloading or changing Music.")
	preview.add_argument("url", nargs="?")
	preview.add_argument("--spotify-fixture", type=Path, help=argparse.SUPPRESS)
	preview.add_argument("--music-fixture", type=Path, help=argparse.SUPPRESS)
	preview.add_argument("--json", action="store_true")

	download = subparsers.add_parser("import", help="Create/reuse managed MP3s; never changes Music.app.")
	download.add_argument("url")
	download.add_argument("--confirm-download", action="store_true", required=True)

	apply = subparsers.add_parser("apply", help="Import only new managed MP3s and idempotently update one Music playlist.")
	apply.add_argument("playlist", help="Spotify playlist ID, Spotify URL, or exact saved playlist name")
	apply.add_argument("--confirm-music-write", action="store_true", required=True)

	subparsers.add_parser("saved-playlists", help="List saved imported playlists and stored Spotify links.").add_argument("--json", action="store_true")
	update_preview = subparsers.add_parser("playlist-update-preview", help="Preview an exact Spotify-order sync for a saved playlist.")
	update_preview.add_argument("playlist")
	update_preview.add_argument("--spotify-fixture", type=Path, help=argparse.SUPPRESS)
	update_preview.add_argument("--music-fixture", type=Path, help=argparse.SUPPRESS)
	update_preview.add_argument("--json", action="store_true")
	update_prepare = subparsers.add_parser("playlist-update-prepare", help="Prepare a confirmed full Spotify playlist sync.")
	update_prepare.add_argument("playlist")
	update_prepare.add_argument("--confirmation-token")
	update_prepare.add_argument("--confirm-download", action="store_true", required=True)
	update_apply = subparsers.add_parser("playlist-update-apply", help="Apply a confirmed guarded full-order playlist sync.")
	update_apply.add_argument("playlist")
	update_apply.add_argument("--confirm-music-write", action="store_true", required=True)
	link = subparsers.add_parser("playlist-link-set", help="Change the stored Spotify link for a saved imported playlist.")
	link.add_argument("playlist")
	link.add_argument("url")
	link.add_argument("--confirm", action="store_true", required=True)
	artwork_refresh = subparsers.add_parser("playlist-artwork-refresh", help="Refresh only a saved Spotify playlist cover URL.")
	artwork_refresh.add_argument("playlist")
	artwork_refresh.add_argument("--json", action="store_true")

	review = subparsers.add_parser("review", help="List unresolved/ambiguous recordings and candidates.")
	review.add_argument("playlist", nargs="?")
	review.add_argument("--json", action="store_true")

	resolve = subparsers.add_parser("resolve", help="Record a deliberate YouTube choice for an unresolved recording.")
	resolve.add_argument("recording", help="Recording ID or Spotify track ID")
	resolve.add_argument("youtube_url")

	search_youtube = subparsers.add_parser("search-youtube", help="Return scored YouTube candidates for the native Raycast resolver.")
	search_youtube.add_argument("recording", help="Recording ID or Spotify track ID")
	search_youtube.add_argument("--query")
	search_youtube.add_argument("--more", action="store_true")

	resolve_youtube = subparsers.add_parser("resolve-youtube", help="Choose a YouTube candidate from the native Raycast resolver.")
	resolve_youtube.add_argument("recording", help="Recording ID or Spotify track ID")
	resolve_youtube.add_argument("youtube_url")

	resolve_music = subparsers.add_parser("resolve-music", help="Choose a displayed Music-library candidate from the native Raycast resolver.")
	resolve_music.add_argument("recording", help="Recording ID or Spotify track ID")
	resolve_music.add_argument("music_persistent_id")

	promote = subparsers.add_parser("promote", help="Switch all memberships to a confident full-album Music copy; never deletes the loose MP3.")
	promote.add_argument("recording_id")
	promote.add_argument("music_persistent_id")
	promote.add_argument("--confirm-promotion", action="store_true", required=True)

	album_preview = subparsers.add_parser("album-preview", help="Preview a Spotify album without downloading or changing Music.")
	album_preview.add_argument("url", nargs="?")
	album_preview.add_argument("--spotify-fixture", type=Path, help=argparse.SUPPRESS)
	album_preview.add_argument("--music-fixture", type=Path, help=argparse.SUPPRESS)
	album_preview.add_argument("--json", action="store_true")

	album_import = subparsers.add_parser("album-import", help="Create or upgrade album-tagged managed MP3s; never invokes Music automation.")
	album_import.add_argument("url")
	album_import.add_argument("--confirm-download", action="store_true", required=True)

	album_apply = subparsers.add_parser("album-apply", help="Add or update one real album in Music without creating an album playlist.")
	album_apply.add_argument("album", help="Spotify album ID, Spotify URL, or exact saved album name")
	album_apply.add_argument("--confirm-music-write", action="store_true", required=True)

	audit = subparsers.add_parser("health-audit", help="Read or explicitly start a durable health audit.")
	audit.add_argument("operation", choices=("status", "start", "dismiss-duplicate"))
	audit.add_argument("issue_id", nargs="?")
	audit.add_argument("--deep-all", action="store_true")
	audit.add_argument("--status-only", action="store_true")
	health = subparsers.add_parser("health", help="Read-only Music library health and local audio diagnostics.")
	health.add_argument("--confirm-read-only-scan", action="store_true", required=True)
	health.add_argument("--deep-all", action="store_true", help="Also fully decode every local Music file.")
	health.add_argument("--json", action="store_true")

	deps = subparsers.add_parser("dependencies", help="Check or deliberately update Homebrew downloader tools.")
	deps.add_argument("operation", choices=["status", "update"])
	deps.add_argument("--confirm-plan", help="Plan ID from dependencies status; required to update.")

	subparsers.add_parser("status", help="Show saved managed-library state.")
	subparsers.add_parser("preflight", help="Show required tools and the configured managed output path.")
	return parser


def _known_playlist_track_covers(manifest: dict[str, Any]) -> dict[str, str]:
	"""Return exact Spotify-track artwork previously checkpointed by the importer."""
	covers: dict[str, str] = {}
	for recording in manifest.get("recordings", {}).values():
		if not isinstance(recording, dict):
			continue
		for occurrence in recording.get("source_occurrences") or []:
			if not isinstance(occurrence, dict):
				continue
			track_id = str(occurrence.get("spotify_id") or "")
			cover_url = str(occurrence.get("cover_url") or "")
			if track_id and cover_url.startswith(("https://", "http://")):
				covers[track_id] = cover_url
	return covers


def _playlist(args: argparse.Namespace, manifest: dict[str, Any]) -> dict[str, Any]:
	if getattr(args, "spotify_fixture", None):
		return load_fixture(args.spotify_fixture).to_dict()
	if not getattr(args, "url", None):
		raise ValueError("A Spotify playlist URL is required unless a test fixture is supplied.")
	return fetch_playlist(args.url, known_track_covers=_known_playlist_track_covers(manifest)).to_dict()


def _album(args: argparse.Namespace) -> dict[str, Any]:
	if getattr(args, "spotify_fixture", None):
		album = load_fixture(args.spotify_fixture)
		if album.source_type != "album":
			raise ValueError("The Spotify fixture is not an album fixture.")
		return album.to_dict()
	if not getattr(args, "url", None):
		raise ValueError("A Spotify album URL is required unless a test fixture is supplied.")
	return fetch_album(args.url).to_dict()


def _music_tracks(args: argparse.Namespace, paths: ManagedPaths) -> list[dict[str, Any]]:
	fixture = getattr(args, "music_fixture", None)
	return load_music_fixture(fixture) if fixture else music_cache_tracks(load_music_cache(paths))


def _current_saved_playlist(args: argparse.Namespace, manifest: dict[str, Any], playlist_id: str) -> dict[str, Any]:
	if getattr(args, "spotify_fixture", None):
		current = load_fixture(args.spotify_fixture).to_dict()
	else:
		url = str(manifest["playlists"][playlist_id].get("spotify_url") or "")
		if not url:
			raise ValueError("This saved playlist has no Spotify link. Use Change Stored Spotify Link first.")
		current = fetch_playlist(url, known_track_covers=_known_playlist_track_covers(manifest)).to_dict()
	current["fetched_spotify_playlist_id"] = current.get("id")
	current["id"] = playlist_id
	return current


def _save_playlist_cover(paths: ManagedPaths, playlist_id: str, source_url: str, cover_url: str | None) -> None:
	"""Persist a current cover without overwriting a concurrently changed Spotify link."""
	if not cover_url or not cover_url.startswith(("https://", "http://")):
		return

	def save(latest: dict[str, Any]) -> None:
		playlist = latest.get("playlists", {}).get(playlist_id)
		if isinstance(playlist, dict) and playlist.get("spotify_url") == source_url and playlist.get("cover_url") != cover_url:
			playlist["cover_url"] = cover_url

	update_manifest(paths, save)


def _find_playlist_id(manifest: dict[str, Any], value: str) -> str:
	text = str(value or "").strip()
	if "spotify.com" in text:
		text = parse_playlist_url(text)[0]
	if text in manifest["playlists"]:
		return text
	matches = [key for key, playlist in manifest["playlists"].items() if playlist.get("name") == text]
	if len(matches) == 1:
		return matches[0]
	if len(matches) > 1:
		raise ValueError("More than one saved Spotify playlist has that name; use the Spotify playlist ID.")
	raise ValueError(f"No saved playlist matches: {value}")


def _find_album_id(manifest: dict[str, Any], value: str) -> str:
	text = str(value or "").strip()
	if "spotify.com" in text:
		text = parse_album_url(text)[0]
	albums = manifest.get("albums", {})
	if text in albums:
		return text
	matches = [key for key, album in albums.items() if album.get("name") == text]
	if len(matches) == 1:
		return matches[0]
	if len(matches) > 1:
		raise ValueError("More than one saved Spotify album has that name; use the Spotify album ID.")
	raise ValueError(f"No saved album matches: {value}")


def _print_preview(preview: Any, *, as_json: bool) -> None:
	if as_json:
		playlist = preview.manifest["playlists"][preview.playlist_id]
		print(json.dumps({
			"source": {
				"type": "playlist",
				"id": preview.playlist_id,
				"name": playlist.get("name") or "Spotify Playlist",
				"url": playlist.get("spotify_url") or "",
				"total": len(preview.rows),
				"warning": playlist.get("warning"),
			},
			"playlist_id": preview.playlist_id,
			"counts": preview.counts,
			"rows": preview.rows,
		}, ensure_ascii=False, indent=2))
		return
	playlist = preview.manifest["playlists"][preview.playlist_id]
	print(f"Preview: {playlist['name']} ({len(preview.rows)} tracks)")
	if playlist.get("warning"):
		print(f"WARNING: {playlist['warning']}")
	category_counts = {"REUSE": 0, "DOWNLOAD": 0, "REVIEW": 0}
	for row in preview.rows:
		if row["status"] in ("reused_music", "managed_existing"):
			category = "REUSE"
		elif row["status"] == "review_music":
			category = "REVIEW"
		else:
			category = "DOWNLOAD"
		category_counts[category] += 1
		print(f"{row['position']:>3}. [{category}] {row['artists']} - {row['title']}")
		print(f"     {row['detail']}")
	print("Totals: " + ", ".join(f"{key.lower()}={value}" for key, value in category_counts.items()))
	print("No files were downloaded and Music.app was not changed.")


def _print_album_preview(preview: Any, *, as_json: bool) -> None:
	if as_json:
		album = preview.manifest["albums"][preview.album_id]
		print(json.dumps({
			"source": {
				"type": "album",
				"id": preview.album_id,
				"name": album.get("name") or "Spotify Album",
				"artist": album.get("album_artist") or "",
				"year": album.get("release_year"),
				"url": album.get("spotify_url") or "",
				"total": len(preview.rows),
				"warning": album.get("warning"),
			},
			"album_id": preview.album_id,
			"counts": preview.counts,
			"rows": preview.rows,
		}, ensure_ascii=False, indent=2))
		return
	album = preview.manifest["albums"][preview.album_id]
	artist = f" — {album['album_artist']}" if album.get("album_artist") else ""
	print(f"Album preview: {album['name']}{artist} ({len(preview.rows)} tracks)")
	if album.get("warning"):
		print(f"WARNING: {album['warning']}")
	category_counts: dict[str, int] = {}
	for row in preview.rows:
		category = "IN LIBRARY" if row["status"] == "in_library" else "NOT IN LIBRARY"
		category_counts[category] = category_counts.get(category, 0) + 1
		print(f"{row['disc_no']}.{row['track_no']:02d} [{category}] {row['artists']} - {row['title']}")
	print("Totals: " + ", ".join(f"{key.lower()}={value}" for key, value in category_counts.items()))
	print("Music.app tracks and files were not changed.")


def _save_album_library_bindings(paths: ManagedPaths, original: dict[str, Any], preview: Any) -> None:
	bindings = {
		row["recording_id"]: preview.manifest["recordings"][row["recording_id"]]
		for row in preview.rows if row["status"] == "in_library"
	}
	if not any(
		music_binding_id(original.get("recordings", {}).get(key) or {}) != music_binding_id(recording)
		for key, recording in bindings.items()
	):
		return
	def repair(latest: dict[str, Any]) -> None:
		for key, recording in bindings.items():
			current = latest["recordings"].get(key)
			if current is None:
				latest["recordings"][key] = copy.deepcopy(recording)
			else:
				current["music_binding"] = copy.deepcopy(recording["music_binding"])
	update_manifest(paths, repair)


def _review_rows(
	manifest: dict[str, Any],
	playlist_id: str | None,
	album_id: str | None = None,
) -> list[dict[str, Any]]:
	allowed = None
	if playlist_id:
		allowed = {item["recording_id"] for item in manifest["playlists"][playlist_id]["items"]}
	elif album_id:
		allowed = {item["recording_id"] for item in manifest.get("albums", {})[album_id]["items"]}
	rows = []
	for key, recording in manifest["recordings"].items():
		if allowed is not None and key not in allowed:
			continue
		if recording.get("review") or recording.get("last_error"):
			rows.append({
				"recording_id": key,
				"metadata": recording.get("source_metadata"),
				"review": recording.get("review"),
				"last_error": recording.get("last_error"),
			})
	return rows


def _progress(callback: Callable[[dict[str, Any]], None] | None, phase: str, **values: Any) -> None:
	if callback:
		callback({"phase": phase, **values})


def run(argv: list[str] | None = None, *, on_progress: Callable[[dict[str, Any]], None] | None = None) -> int:
	arguments = list(sys.argv[1:] if argv is None else argv)
	if arguments[:1] == ["health-audit"]:
		from crate_music_importer.ipod_import.health_audit import dismiss_duplicate_alert, load_health_audit, start_health_audit
		args = _parser().parse_args(arguments)
		paths = ManagedPaths()
		if args.operation == "dismiss-duplicate":
			if not args.issue_id:
				raise ValueError("A possible recording duplicate finding ID is required.")
			result = dismiss_duplicate_alert(paths, args.issue_id)
		else:
			result = start_health_audit(paths, args.deep_all) if args.operation == "start" else load_health_audit(paths, include_report=not args.status_only)
		print(json.dumps(result))
		return 0
	if arguments[:1] == ["dependencies"]:
		from crate_music_importer.ipod_import import dependencies
		args = _parser().parse_args(arguments)
		try:
			if args.operation == "update" and not args.confirm_plan:
				raise ValueError("Run dependencies status and explicitly confirm its plan ID before updating.")
			value = dependencies.update(args.confirm_plan) if args.operation == "update" else dependencies.status()
			print(json.dumps(value, indent=2))
			return 0
		except Exception as exc:
			print(json.dumps({"operation": args.operation, "operationTime": dependencies.datetime.now(dependencies.timezone.utc).isoformat(), "dependencies": [], "error": dependencies.redact(str(exc))}))
			return 1
	from crate_music_importer.ipod_import.dependency_lock import dependency_lock
	with dependency_lock():
		return _run(arguments, on_progress=on_progress)


def _run(
	argv: list[str] | None = None,
	*,
	on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> int:
	args = _parser().parse_args(argv)
	paths = ManagedPaths()
	if args.command == "preflight":
		print(f"Managed root: {MANAGED_ROOT}")
		for name, path in check_tools().items():
			print(f"{name}: {path}")
		print("Music access: macOS Automation permission for Terminal or Raycast -> Music (requested by preview/apply when used).")
		return 0
	if args.command == "health":
		from crate_music_importer.ipod_import.health import build_health_report, failed_health_report, save_health_report
		try:
			manifest = load_manifest(paths)
			music_tracks = scan_music_library_for_health()
			refresh_music_cache(paths, music_tracks)
			result = build_health_report(paths, manifest, music_tracks, on_progress=on_progress, deep_all=args.deep_all)
		except Exception as exc:
			result = failed_health_report(str(exc))
		save_health_report(paths, result)
		print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else f"Library health: {result['status']}\n{json.dumps(result, ensure_ascii=False, indent=2)}")
		return 0
	manifest = load_manifest(paths)
	if args.command == "saved-playlists":
		update_manifest(paths, backfill_playlist_urls)
		manifest = load_manifest(paths)
		resolved_covers = refresh_playlist_covers(manifest, fetch_playlist_cover)
		if resolved_covers:

			def cache_resolved_covers(latest: dict[str, Any]) -> None:
				for playlist_id, (source_url, cover_url) in resolved_covers.items():
					playlist = latest.get("playlists", {}).get(playlist_id)
					if isinstance(playlist, dict) and playlist.get("spotify_url") == source_url:
						playlist["cover_url"] = cover_url

			update_manifest(paths, cache_resolved_covers)
			manifest = load_manifest(paths)
		values = saved_playlists(manifest)
		print(json.dumps({"playlists": values}, ensure_ascii=False, indent=2) if args.json else "\n".join(f"{value['name']}\t{value['track_count']}\t{value['spotify_url']}" for value in values))
		return 0
	if args.command == "playlist-link-set":
		playlist_id = _find_playlist_id(manifest, args.playlist)
		_, canonical = parse_playlist_url(args.url)
		manifest["playlists"][playlist_id]["spotify_url"] = canonical
		manifest["playlists"][playlist_id]["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
		save_manifest(paths, manifest)
		print(json.dumps({"playlist_id": playlist_id, "spotify_url": canonical}, ensure_ascii=False))
		return 0
	if args.command == "playlist-artwork-refresh":
		playlist_id = _find_playlist_id(manifest, args.playlist)
		url = str(manifest["playlists"][playlist_id].get("spotify_url") or "")
		if not url:
			raise ValueError("This saved playlist has no Spotify link. Use Change Stored Spotify Link first.")
		cover_url = fetch_playlist_cover(url)
		if not cover_url:
			raise ValueError("Spotify did not return a playlist image. Try again later.")
		_save_playlist_cover(paths, playlist_id, url, cover_url)
		print(json.dumps({"playlist_id": playlist_id, "cover_url": cover_url}, ensure_ascii=False) if args.json else cover_url)
		return 0
	if args.command == "playlist-update-preview":
		playlist_id = _find_playlist_id(manifest, args.playlist)
		current = _current_saved_playlist(args, manifest, playlist_id)
		_save_playlist_cover(paths, playlist_id, str(manifest["playlists"][playlist_id].get("spotify_url") or ""), current.get("cover_url"))
		preview = build_playlist_update_preview(current, _music_tracks(args, paths), manifest, paths)
		print(json.dumps(preview.to_dict(), ensure_ascii=False, indent=2) if args.json else f"{len(preview.additions)} additions; {len(preview.removals)} removals; {preview.state}")
		return 0
	if args.command == "playlist-update-prepare":
		playlist_id = _find_playlist_id(manifest, args.playlist)
		current = _current_saved_playlist(args, manifest, playlist_id)
		_save_playlist_cover(paths, playlist_id, str(manifest["playlists"][playlist_id].get("spotify_url") or ""), current.get("cover_url"))
		preview = build_playlist_update_preview(current, music_cache_tracks(load_music_cache(paths)), manifest, paths)
		if preview.state != "ready":
			raise ValueError(preview.warning or "Playlist is up to date; no update was queued.")
		if (preview.removals or preview.reorders or preview.music_changes) and args.confirmation_token != preview.confirmation_token():
			raise ValueError("Spotify or Music changed after the update preview. Refresh and confirm the exact playlist sync again.")
		if preview.addition_items:
			check_tools()
			execute_import(preview=type("AdditionPreview", (), {"manifest": preview.manifest, "playlist_id": playlist_id})(), paths=paths, on_output=print, on_progress=on_progress, items_override=preview.addition_items)
		save_pending_update(preview, paths)
		print(f"Prepared {len(preview.additions)} additions, {len(preview.removals)} removals, {len(preview.reorders)} Spotify position changes, and {len(preview.music_changes)} Music repairs.")
		statuses = {str(item.get("status") or "") for item in preview.addition_items}
		return 2 if statuses & {"review_required", "failed", "review_music"} else 0
	if args.command == "playlist-update-apply":
		playlist_id = _find_playlist_id(manifest, args.playlist)
		result = apply_playlist_update(
			manifest,
			playlist_id,
			paths,
			music_cache_tracks(load_music_cache(paths)),
			exact_lookup=lookup_music_track,
			cache_updater=lambda track: upsert_music_cache_track(paths, track),
			cache_remover=lambda persistent_ids: remove_music_cache_tracks(paths, persistent_ids),
			on_progress=on_progress,
		)
		print(f"Playlist update complete: {result['additions']} additions; {result['removals']} removals; {result['reorders']} Spotify position changes; {result['music_repairs']} Music repairs; {result['deleted']} permanently deleted.")
		print("Finder/iPod sync settings were not touched.")
		return 0
	if args.command == "status":
		managed = sum(1 for recording in manifest["recordings"].values() if (recording.get("managed_file") or {}).get("managed_by_crate"))
		resolved = sum(1 for recording in manifest["recordings"].values() if music_binding_id(recording))
		unresolved = len(_review_rows(manifest, None))
		print(f"Managed root: {paths.root}")
		print(f"Albums: {len(manifest.get('albums', {}))}; playlists: {len(manifest['playlists'])}; recordings: {len(manifest['recordings'])}; managed files: {managed}; resolved in Music: {resolved}; unresolved: {unresolved}")
		return 0
	if args.command in ("preview", "dry-run"):
		_progress(on_progress, "loading_metadata", source_type="playlist")
		playlist = _playlist(args, manifest)
		_progress(on_progress, "loading_music_cache", source_type="playlist", source_id=playlist.get("id"), name=playlist.get("name"), total=len(playlist.get("tracks") or []))
		preview = build_preview(playlist, _music_tracks(args, paths), manifest, paths)
		_print_preview(preview, as_json=args.json)
		return 0
	if args.command == "album-preview":
		_progress(on_progress, "loading_metadata", source_type="album")
		album = _album(args)
		_progress(on_progress, "loading_music_cache", source_type="album", source_id=album.get("id"), name=album.get("name"), total=len(album.get("tracks") or []))
		music_tracks = load_music_fixture(args.music_fixture) if args.music_fixture else scan_music_library()
		if not args.music_fixture:
			refresh_music_cache(paths, music_tracks)
		preview = build_album_library_preview(album, music_tracks, manifest)
		_save_album_library_bindings(paths, manifest, preview)
		_print_album_preview(preview, as_json=args.json)
		return 0
	if args.command == "album-import":
		tools = check_tools()
		print("Tools: " + ", ".join(f"{name}={path}" for name, path in tools.items()))
		_progress(on_progress, "loading_metadata", source_type="album")
		album = fetch_album(args.url).to_dict()
		_progress(on_progress, "loading_music_cache", source_type="album", source_id=album.get("id"), name=album.get("name"), total=len(album.get("tracks") or []))
		preview = build_album_preview(album, music_cache_tracks(load_music_cache(paths)), manifest, paths)
		print(f"Preparing real album {album['name']} in {paths.root}")
		result = execute_album_import(preview, paths, on_output=print, on_progress=on_progress)
		statuses: dict[str, int] = {}
		for item in result["album"]["items"]:
			statuses[item["status"]] = statuses.get(item["status"], 0) + 1
		print("Result: " + ", ".join(f"{key}={value}" for key, value in sorted(statuses.items())))
		if statuses.get("review_required") or statuses.get("failed"):
			print("Next action in Raycast: open Review Activity, resolve the tracks, then continue the album.")
		else:
			print("Album files are ready for Music. The combined Raycast command will continue automatically.")
		return 0 if not statuses.get("failed") and not statuses.get("review_required") else 2
	if args.command == "album-apply":
		album_id = _find_album_id(manifest, args.album)
		album = manifest.get("albums", {}).get(album_id) or {}
		_progress(on_progress, "checking_music_ids", source_type="album", source_id=album_id, name=album.get("name"), total=len(album.get("items") or []))
		music_tracks = music_cache_tracks(load_music_cache(paths))
		result = apply_album_to_music(
			manifest,
			album_id,
			paths,
			music_tracks,
			on_progress=on_progress,
			exact_lookup=lookup_music_track,
			cache_updater=lambda track: upsert_music_cache_track(paths, track),
			cache_remover=lambda persistent_ids: remove_music_cache_tracks(paths, persistent_ids),
			verify_music=verify_music_tracks,
		)
		print(
			f"Music album updated: {result['track_count']} tracks; {result['new_imports']} new imports; "
			f"{result['updated_tracks']} Crate-managed files updated in place; {result['reused_tracks']} existing album tracks reused; "
			f"{result.get('stability_recoveries', 0)} Music additions recovered by Crate Music Importer."
		)
		print("No album playlist was created. Existing Spotify playlists keep using the same Music track IDs. Finder/iPod sync settings were not touched.")
		return 0
	if args.command == "import":
		tools = check_tools()
		print("Tools: " + ", ".join(f"{name}={path}" for name, path in tools.items()))
		_progress(on_progress, "loading_metadata", source_type="playlist")
		playlist = fetch_playlist(args.url, known_track_covers=_known_playlist_track_covers(manifest)).to_dict()
		_progress(on_progress, "loading_music_cache", source_type="playlist", source_id=playlist.get("id"), name=playlist.get("name"), total=len(playlist.get("tracks") or []))
		preview = build_preview(playlist, music_cache_tracks(load_music_cache(paths)), manifest, paths)
		print(f"Importing {playlist['name']} into {paths.root}")
		result = execute_import(preview, paths, on_output=print, on_progress=on_progress)
		statuses: dict[str, int] = {}
		for item in result["playlist"]["items"]:
			statuses[item["status"]] = statuses.get(item["status"], 0) + 1
		print("Result: " + ", ".join(f"{key}={value}" for key, value in sorted(statuses.items())))
		print(f"M3U8: {result['m3u8']}")
		if statuses.get("review_required") or statuses.get("failed"):
			print("Next action in Raycast: open Review Activity, resolve the tracks, then continue the playlist.")
		else:
			print("Playlist files are ready for Music. The combined Raycast command will continue automatically.")
		return 0 if not statuses.get("failed") and not statuses.get("review_required") else 2
	if args.command == "apply":
		playlist_id = _find_playlist_id(manifest, args.playlist)
		_progress(on_progress, "checking_music_ids", source_type="playlist", source_id=playlist_id)
		result = apply_to_music(
			manifest,
			playlist_id,
			paths,
			music_cache_tracks(load_music_cache(paths)),
			on_progress=on_progress,
			exact_lookup=lookup_music_track,
			cache_updater=lambda track: upsert_music_cache_track(paths, track),
		)
		print(f"Music playlist updated: {result['track_count']} ordered entries; {result['new_imports']} new managed MP3 imports.")
		print("Finder/iPod sync settings were not touched. Use the normal whole-library sync later; the iPod need not be connected now.")
		return 0
	if args.command == "review":
		playlist_id = None
		album_id = None
		if args.playlist:
			if "spotify.com" in args.playlist:
				source_type = parse_source_url(args.playlist)[0]
				if source_type == "album":
					album_id = _find_album_id(manifest, args.playlist)
				else:
					playlist_id = _find_playlist_id(manifest, args.playlist)
			elif args.playlist in manifest.get("albums", {}):
				album_id = args.playlist
			else:
				playlist_id = _find_playlist_id(manifest, args.playlist)
		rows = _review_rows(manifest, playlist_id, album_id)
		if args.json:
			snapshot = resolver_snapshot(paths)
			if playlist_id or album_id:
				source_type = "playlist" if playlist_id else "album"
				source_id = playlist_id or album_id
				snapshot["problems"] = [
					problem for problem in snapshot["problems"]
					if any(source["type"] == source_type and source["id"] == source_id for source in problem["sources"])
				]
				snapshot["sources"] = [source for source in snapshot["sources"] if source["type"] == source_type and source["id"] == source_id]
				snapshot["readySources"] = [source for source in snapshot["readySources"] if source["type"] == source_type and source["id"] == source_id]
			print(json.dumps(snapshot, ensure_ascii=False, indent=2))
		else:
			for row in rows:
				meta = row["metadata"] or {}
				print(f"\nRECORDING: {row['recording_id']}")
				print(f"TRACK: {meta.get('artists', '')} - {meta.get('title', '')}")
				review = row.get("review") or {}
				if review:
					print(f"PROBLEM: {review.get('message') or review.get('kind')}")
					for number, candidate in enumerate(review.get("candidates") or [], start=1):
						if review.get("kind") in ("music_ambiguity", "album_identity_mismatch", "album_identity_conflict"):
							print(
								f"  {number}. Music PID {candidate.get('persistent_id') or '?'} | score {candidate.get('score') or '?'} | "
								f"{candidate.get('artist') or ''} - {candidate.get('title') or candidate.get('name') or ''} | "
								f"album {candidate.get('album') or '?'} | {candidate.get('duration_s') or 0:.1f}s | {candidate.get('location') or 'no local file location'}"
							)
						else:
							print(
								f"  {number}. YouTube | score {candidate.get('score') if candidate.get('score') is not None else '?'} | "
								f"{candidate.get('uploader') or ''} - {candidate.get('title') or ''} | "
								f"{candidate.get('duration_s') or 0:.1f}s | {candidate.get('url') or '?'}"
							)
				if row.get("last_error"):
					print(f"LAST FAILURE: {row['last_error']}")
					if review.get("kind") in ("album_identity_mismatch", "album_identity_conflict"):
						print("TO RESOLVE: choose the Music track carrying the requested album identity; Crate will not modify an unregistered physical file.")
					else:
						print("TO RESOLVE IN RAYCAST: open Review Activity and choose a verified recording.")
			if not rows:
				print("No unresolved recordings.")
		return 0
	if args.command == "search-youtube":
		print(json.dumps(search_youtube_choices(args.recording, args.query, more=args.more, paths=paths), ensure_ascii=False, indent=2))
		return 0
	if args.command == "resolve-youtube":
		result = resolve_youtube_choice(args.recording, args.youtube_url, paths=paths)
		print(json.dumps(result, ensure_ascii=False, indent=2))
		return 0
	if args.command == "resolve-music":
		result = resolve_music_choice(args.recording, args.music_persistent_id, paths=paths)
		print(json.dumps(result, ensure_ascii=False, indent=2))
		return 0
	if args.command == "resolve":
		result = resolve_youtube_choice(args.recording, args.youtube_url, paths=paths, allow_music_override=True)
		print(
			f"Saved deliberate YouTube source for {result['recordingId']}; continue it from Review Activity."
		)
		return 0
	if args.command == "promote":
		recording = promote_recording(manifest, args.recording_id, args.music_persistent_id, music_cache_tracks(load_music_cache(paths)), paths)
		print(f"Promoted {recording['recording_id']} to Music persistent ID {args.music_persistent_id}.")
		print("The recording binding changed. No Music track or physical file was deleted.")
		return 0
	return 1


def main() -> None:
	try:
		if sys.argv[1:2] == ["--raycast"]:
			from crate_music_importer.ipod_import.raycast import main as raycast_main
			del sys.argv[1]
			raycast_main()
			return
		raise SystemExit(run())
	except KeyboardInterrupt:
		print("Interrupted. Completed manifest checkpoints and partial yt-dlp data are resumable.", file=sys.stderr)
		raise SystemExit(130)
	except Exception as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		raise SystemExit(2)
