import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import import cli, health
from crate_music_importer.ipod_import.dependency_lock import dependency_lock, DependenciesBusyError
from crate_music_importer.ipod_import.manifest import ManagedPaths, new_manifest
from crate_music_importer.ipod_import.music_cache import build_full_cache, save_music_cache


class HealthTests(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.root = Path(self.tmp.name)
		self.paths = ManagedPaths(self.root / "managed")
		self.manifest = new_manifest(self.paths)
		self.stack = contextlib.ExitStack()
		self.addCleanup(self.stack.close)
		self.stack.enter_context(patch.dict(os.environ, {"CRATE_APPLICATION_STATE": str(self.root / "app")}))
		self.probe = self.stack.enter_context(patch.object(health, "probe_duration_ms", return_value=180000))
		self.decode = self.stack.enter_context(patch.object(health, "_decode"))
		self.real_tags = health._tags_and_artwork
		self.tags = self.stack.enter_context(patch.object(health, "_tags_and_artwork", return_value=({"title": "Song", "artist": "Artist", "album": "Playlist Imports", "track": "", "disc": "", "compilation": "1"}, [])))

	def track(self, pid="A", *, managed=False, data=b"audio", title="Song"):
		path = (self.paths.root if managed else self.root / "user") / f"{pid}.mp3"
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(data)
		track = {"persistent_id": pid, "location": str(path), "title": title, "artist": "Artist", "album": "Playlist Imports", "duration_s": 180, "compilation": True}
		if managed:
			self.manifest["recordings"][pid] = {"recording_id": pid, "spotify_ids": [f"spotify-{pid}"], "source_metadata": {"title": title, "artists": "Artist", "duration_ms": 180000, "cover_url": "track-cover"}, "managed_file": {"relative_path": path.name, "tool_owned": True, "sha256": health.file_sha256(path), "artwork_source_url": "track-cover", "metadata_profile": "playlist"}, "music": {"persistent_id": pid}, "active_reference": {"kind": "managed_file", "relative_path": path.name}}
		return track

	def report(self, tracks, **kwargs):
		save_music_cache(self.paths, build_full_cache(self.paths, self.manifest, tracks))
		return health.build_health_report(self.paths, self.manifest, tracks, **kwargs)

	def categories(self, result):
		return {issue["category"] for issue in result["issues"]}

	def test_missing_cloud_and_ordinary_tracks(self):
		good = self.track()
		missing = self.track("B")
		Path(missing["location"]).unlink()
		cloud = {"persistent_id": "C", "title": "Cloud"}
		result = self.report([good, missing, cloud])
		self.assertEqual(self.categories(result), {"missing_or_unreadable_file", "cloud_only"})
		self.assertEqual(result["summary"]["missingFiles"], 1)
		self.assertEqual(next(i for i in result["issues"] if i["category"] == "cloud_only")["severity"], "informational")
		self.tags.assert_not_called()
		self.decode.assert_not_called()

	def test_marked_untracked_and_absent_manifest_ids(self):
		track = self.track()
		track["comment"] = "Managed by Crate Music Importer; recording_id=missing"
		self.manifest["recordings"]["old"] = {"music": {"persistent_id": "GONE"}}
		result = self.report([track])
		self.assertIn("untracked_managed_track", self.categories(result))
		self.assertIn("manifest_id_absent", self.categories(result))
		self.assertEqual(next(i for i in result["issues"] if i["category"] == "untracked_managed_track")["ownership"], "untracked")

	def test_probe_failure_continues_and_deep_check_is_explicit(self):
		tracks = [self.track("A"), self.track("B", data=b"other")]
		self.probe.side_effect = [ValueError("bad container"), 180000]
		result = self.report(tracks, deep_all=True)
		self.assertEqual(self.probe.call_count, 2)
		self.decode.assert_called_once_with(Path(tracks[1]["location"]).resolve())
		self.assertIn("probe_failed", self.categories(result))
		self.assertEqual(result["status"], "failed")

	def test_exact_and_semantic_duplicates_are_distinct_and_version_aware(self):
		tracks = [self.track("A"), self.track("B"), self.track("C", data=b"other"), self.track("D", data=b"live", title="Song (Live)")]
		result = self.report(tracks)
		exact = [i for i in result["issues"] if i["category"] == "exact_duplicate"]
		semantic = [i for i in result["issues"] if i["category"] == "semantic_duplicate"]
		self.assertEqual(len(exact), 1)
		self.assertEqual(exact[0]["persistentIds"], ["A", "B"])
		self.assertEqual(len(semantic), 1)
		self.assertEqual(semantic[0]["persistentIds"], ["A", "B", "C"])
		self.assertEqual(exact[0]["ownership"], "user_owned")

	def test_managed_metadata_artwork_duration_and_decode(self):
		track = self.track(managed=True)
		self.tags.return_value = ({"title": "Wrong"}, ["Invalid artwork"])
		self.probe.return_value = 100000
		self.decode.side_effect = ValueError("truncated stream")
		result = self.report([track])
		self.assertTrue({"duration_mismatch", "tag_mismatch", "artwork_discrepancy", "decode_failed"} <= self.categories(result))
		self.assertTrue(all(i["ownership"] == "importer_owned" for i in result["issues"]))

	def test_expected_playlist_tags_are_healthy(self):
		result = self.report([self.track(managed=True)])
		self.assertEqual(result["issues"], [])
		self.assertEqual(result["status"], "healthy")

	def test_reference_only_never_compares_or_rewrites_user_tags(self):
		track = self.track()
		self.manifest["recordings"]["A"] = {"music": {"persistent_id": "A"}, "source_metadata": {"title": "Other"}, "active_reference": {"kind": "existing_music", "persistent_id": "A"}}
		result = self.report([track])
		self.assertEqual(result["issues"], [])
		self.tags.assert_not_called()
		self.decode.assert_not_called()

	def test_conflicting_references_are_ambiguous(self):
		track = self.track(managed=True)
		self.manifest["recordings"]["B"] = {"music": {"persistent_id": "A"}}
		result = self.report([track])
		self.assertIn("conflicting_recording_references", self.categories(result))
		self.tags.assert_not_called()
		self.decode.assert_not_called()

	def test_registered_file_without_music_still_checked(self):
		track = self.track(managed=True)
		Path(track["location"]).unlink()
		result = self.report([])
		self.assertTrue({"manifest_id_absent", "missing_or_unreadable_file"} <= self.categories(result))
		self.assertEqual(result["summary"]["localFiles"], 1)

	def test_cache_staleness_is_reported_without_mutating_the_index(self):
		track = self.track()
		save_music_cache(self.paths, build_full_cache(self.paths, self.manifest, [track]))
		before = self.paths.music_cache.read_bytes()
		result = health.build_health_report(self.paths, self.manifest, [])
		self.assertIn("stale_cache_reference", self.categories(result))
		self.assertEqual(self.paths.music_cache.read_bytes(), before)

	def test_missing_cached_location_does_not_conflict_with_exact_live_id(self):
		track = self.track()
		cache = build_full_cache(self.paths, self.manifest, [track])
		cache["tracks"][track["persistent_id"]]["location"] = None
		save_music_cache(self.paths, cache)
		result = health.build_health_report(self.paths, self.manifest, [track])
		self.assertNotIn("stale_cache_reference", self.categories(result))

	def test_different_cached_location_remains_stale(self):
		track = self.track()
		cache = build_full_cache(self.paths, self.manifest, [track])
		cache["tracks"][track["persistent_id"]]["location"] = str(self.root / "old.mp3")
		save_music_cache(self.paths, cache)
		result = health.build_health_report(self.paths, self.manifest, [track])
		self.assertIn("stale_cache_reference", self.categories(result))

	def test_scan_is_read_only_and_diagnostic_saved_atomically(self):
		track = self.track(managed=True)
		save_music_cache(self.paths, build_full_cache(self.paths, self.manifest, [track]))
		self.paths.manifest.write_text(json.dumps(self.manifest))
		before = {p: p.read_bytes() for p in self.paths.root.rglob("*") if p.is_file()}
		manifest_before = copy.deepcopy(self.manifest)
		result = health.build_health_report(self.paths, self.manifest, [track])
		self.assertEqual(self.manifest, manifest_before)
		with patch.object(health.os, "replace", wraps=os.replace) as replace:
			target = health.save_health_report(self.paths, result)
			replace.assert_called_once()
			self.assertEqual(Path(replace.call_args.args[0]).parent, target.parent)
		self.assertEqual(json.loads(target.read_text()), result)
		self.assertEqual({p: p.read_bytes() for p in self.paths.root.rglob("*") if p.is_file() and p != target}, before)

	def test_failed_atomic_replace_preserves_previous_diagnostic(self):
		target = health.save_health_report(self.paths, {"old": True})
		with patch.object(health.os, "replace", side_effect=OSError("disk error")):
			with self.assertRaises(OSError):
				health.save_health_report(self.paths, {"new": True})
		self.assertEqual(json.loads(target.read_text()), {"old": True})
		self.assertEqual(list(self.paths.state_dir.glob("*.tmp")), [])

	def test_health_holds_shared_dependency_lock(self):
		track = self.track()
		def probe(path):
			with dependency_lock():
				pass
			with self.assertRaises(DependenciesBusyError):
				with dependency_lock(exclusive=True):
					self.fail("Exclusive lock must not succeed")
			return 180000
		self.probe.side_effect = probe
		self.report([track])

	def test_cli_scan_failure_saved_and_confirmation_required(self):
		with patch.object(cli, "ManagedPaths", return_value=self.paths), patch.object(cli, "scan_music_library_for_health", side_effect=RuntimeError("Music permission denied")) as scan:
			with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
				cli.run(["health", "--json"])
			scan.assert_not_called()
			with contextlib.redirect_stdout(io.StringIO()) as output:
				self.assertEqual(cli.run(["health", "--confirm-read-only-scan", "--json"]), 0)
			report = json.loads(output.getvalue())
			self.assertEqual(report["status"], "failed")
			self.assertIn("permission denied", report["error"])
			self.assertEqual(json.loads((self.paths.state_dir / "health-last-result.json").read_text()), report)


	def test_missing_decoder_is_check_failure_not_claimed_corruption(self):
		track = self.track(managed=True)
		self.decode.side_effect = health.MediaError("ffmpeg is required")
		result = self.report([track])
		self.assertEqual(result["status"], "failed")
		self.assertEqual(result["summary"]["corruptFiles"], 0)
		self.assertIn("decode_unavailable", self.categories(result))

	def test_health_music_scan_preserves_duplicates_and_rejects_incomplete_rows(self):
		from crate_music_importer.ipod_import import music
		fields = ["Song", "Artist", "Album", "180", "PID", "1", "", "", "Artist", "1", "10", "1", "1", "false"]
		output = chr(31).join(fields) + chr(30)
		with patch.object(music, "_osascript", return_value=output * 2) as script:
			tracks = music.scan_music_library_for_health()
			self.assertEqual(len(tracks), 2)
			self.assertIsNone(tracks[0]["location"])
			self.assertIn("is missing value then", script.call_args.args[0])
			self.assertEqual(len(music.scan_music_library()), 1)
		with patch.object(music, "_osascript", return_value="partial row"):
			with self.assertRaises(music.MusicAutomationError):
				music.scan_music_library_for_health()
		result = health.build_health_report(self.paths, self.manifest, tracks)
		self.assertIn("duplicate_persistent_id", self.categories(result))

	def test_album_profile_uses_album_tags_and_existing_duration_tolerance(self):
		track = self.track(managed=True)
		track.update({"album": "Album", "track_no": 3, "disc_no": 2, "compilation": False})
		recording = self.manifest["recordings"]["A"]
		recording["album_metadata"] = {"album": "Album", "track_no": 3, "disc_no": 2, "is_compilation": False}
		recording["managed_file"].update({"metadata_profile": "album", "source_duration_ms": 180000})
		self.tags.return_value = ({"title": "Song", "artist": "Artist", "album": "Album", "track": "3/10", "disc": "2/2", "compilation": "0"}, [])
		self.probe.return_value = 181800
		self.assertEqual(self.report([track])["issues"], [])
		self.probe.return_value = 181801
		self.assertIn("duration_mismatch", self.categories(self.report([track])))

	def test_spotify_conflicts_locations_and_active_identity(self):
		first = self.track("A", managed=True)
		second = self.track("B", managed=True, data=b"different")
		self.manifest["recordings"]["B"]["spotify_ids"] = ["spotify-A"]
		self.manifest["recordings"]["A"]["active_reference"] = {"kind": "existing_music", "persistent_id": "B"}
		second["title"] = "Wrong recording (Live)"
		result = self.report([first, second])
		self.assertTrue({"spotify_duplicate", "conflicting_music_ids", "music_location_mismatch", "active_reference_mismatch"} <= self.categories(result))

	def test_one_metadata_failure_continues_to_later_recording(self):
		tracks = [self.track("A", managed=True), self.track("B", managed=True, data=b"different")]
		self.tags.side_effect = [ValueError("broken ID3"), ({"title": "Song", "artist": "Artist", "album": "Playlist Imports", "compilation": "1"}, [])]
		result = self.report(tracks)
		self.assertEqual(self.tags.call_count, 2)
		self.assertIn("metadata_check_failed", self.categories(result))

	def test_artwork_parser_reads_embedded_bytes_without_writes(self):
		from mutagen.id3 import ID3, APIC, TIT2
		from types import SimpleNamespace
		path = self.root / "art.mp3"
		tags = ID3()
		tags.add(TIT2(encoding=3, text=["Song"]))
		tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=b"invalid image"))
		tags.save(path)
		before = path.read_bytes()
		# Bypass only the outer test mock to exercise the real read-only parser.
		with patch.object(health, "_tool", return_value="ffmpeg"), patch.object(health.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr=b"bad image")) as decode:
			values, problems = self.real_tags(path)
		self.assertEqual(values["title"], "Song")
		self.assertIn("could not be parsed", problems[0])
		self.assertEqual(decode.call_args.kwargs["input"], b"invalid image")
		self.assertEqual(path.read_bytes(), before)

	def test_symlink_loop_is_a_finding_and_does_not_abort(self):
		track = self.track()
		path = Path(track["location"])
		path.unlink()
		path.symlink_to(path)
		later = self.track("B")
		result = self.report([track, later])
		self.assertIn("missing_or_unreadable_file", self.categories(result))
		self.probe.assert_called_once()

	def moved_track(self, pid="A", *, changed=False):
		track = self.track(pid, managed=True)
		old = Path(track["location"])
		new = old.parent / "Music" / old.name
		new.parent.mkdir(exist_ok=True)
		old.rename(new)
		if changed:
			new.write_bytes(b"changed tags or audio")
		track["location"] = str(new)
		return track

	def test_exact_live_id_at_checked_new_location_is_not_missing_or_duplicate(self):
		track = self.moved_track()
		before = copy.deepcopy(self.manifest)
		result = self.report([track])
		self.assertEqual(self.categories(result), {"saved_location_outdated"})
		self.assertEqual(result["issues"][0]["severity"], "informational")
		self.assertEqual(result["summary"]["missingFiles"], 0)
		self.assertEqual(result["status"], "healthy")
		self.assertEqual(self.manifest, before)
		self.decode.assert_called_once_with(Path(track["location"]).resolve())

	def test_relocations_group_into_one_informational_finding(self):
		tracks = [self.moved_track("A"), self.moved_track("B")]
		result = self.report(tracks)
		issues = [i for i in result["issues"] if i["category"] == "saved_location_outdated"]
		self.assertEqual(len(issues), 1)
		self.assertIn("2 old saved", issues[0]["detail"])

	def test_relocated_changed_bytes_keep_fingerprint_warning(self):
		result = self.report([self.moved_track(changed=True)])
		self.assertEqual(self.categories(result), {"saved_location_outdated", "managed_hash_mismatch"})
		self.assertEqual(result["summary"]["critical"], 0)

	def test_relocation_never_hides_corrupt_current_audio(self):
		track = self.moved_track()
		self.decode.side_effect = ValueError("invalid audio")
		result = self.report([track])
		self.assertIn("decode_failed", self.categories(result))
		self.assertIn("missing_or_unreadable_file", self.categories(result))
		self.assertNotIn("saved_location_outdated", self.categories(result))

	def test_relocation_needs_unambiguous_exact_id(self):
		track = self.moved_track()
		self.report([track])
		result = health.build_health_report(self.paths, self.manifest, [track, dict(track)])
		self.assertIn("missing_or_unreadable_file", self.categories(result))
		self.assertNotIn("saved_location_outdated", self.categories(result))

	def test_no_current_music_reference_still_reports_missing_file(self):
		self.moved_track()
		result = self.report([])
		self.assertIn("missing_or_unreadable_file", self.categories(result))
		self.assertEqual(result["summary"]["critical"], 1)

	def test_missing_path_is_not_evidence_of_spotify_duplicate(self):
		first = self.track("A", managed=True)
		second = self.track("B", managed=True, data=b"different")
		self.manifest["recordings"]["B"]["spotify_ids"] = ["spotify-A"]
		Path(second["location"]).unlink()
		result = self.report([first, second])
		self.assertNotIn("spotify_duplicate", self.categories(result))
		self.assertIn("missing_or_unreadable_file", self.categories(result))

	def test_shared_root_alone_is_not_importer_ownership(self):
		track = self.track(managed=True)
		self.manifest["recordings"].clear()
		result = self.report([track])
		self.assertEqual(result["issues"], [])
		self.decode.assert_not_called()
		self.tags.assert_not_called()

	def test_tag_only_change_is_benign_and_still_checks_metadata(self):
		track = self.track(managed=True)
		self.manifest["recordings"]["A"]["managed_file"]["audio_sha256"] = "audio-baseline"
		Path(track["location"]).write_bytes(b"new artwork")
		with patch.object(health, "audio_sha256", return_value="audio-baseline"):
			result = self.report([track])
		self.assertEqual(result["issues"], [])
		self.assertEqual(result["status"], "healthy")
		self.tags.assert_called_once()

	def test_audio_change_is_not_dismissed_as_a_tag_edit(self):
		track = self.track(managed=True)
		self.manifest["recordings"]["A"]["managed_file"]["audio_sha256"] = "old-audio"
		Path(track["location"]).write_bytes(b"replacement recording")
		with patch.object(health, "audio_sha256", return_value="new-audio"):
			result = self.report([track])
		self.assertIn("audio_content_changed", self.categories(result))
		self.assertEqual(result["checks"]["fileIntegrity"], "attention")
		self.assertNotIn("non_audio_file_changed", self.categories(result))

	def test_audio_fingerprint_failure_does_not_claim_unchanged_audio(self):
		track = self.track(managed=True)
		self.manifest["recordings"]["A"]["managed_file"]["audio_sha256"] = "old-audio"
		Path(track["location"]).write_bytes(b"changed")
		with patch.object(health, "audio_sha256", side_effect=ValueError("unavailable")):
			result = self.report([track])
		self.assertEqual(result["status"], "failed")
		self.assertIn("audio_fingerprint_unavailable", self.categories(result))

	def test_accepted_local_duration_is_bound_to_audio_baseline(self):
		track = self.track(managed=True)
		r = self.manifest["recordings"]["A"]
		r["managed_file"]["audio_sha256"] = "accepted-audio"
		r["local_preferences"] = {"accepted_duration": {"duration_ms": 100000, "audio_sha256": "accepted-audio"}}
		self.probe.return_value = 100000
		self.assertNotIn("duration_mismatch", self.categories(self.report([track])))
		self.probe.return_value = 120000
		self.assertIn("duration_mismatch", self.categories(self.report([track])))
		self.probe.return_value = 100000
		r["managed_file"]["audio_sha256"] = "replacement"
		self.assertIn("duration_mismatch", self.categories(self.report([track])))

	def test_distinct_recordings_decision_expires_if_file_changes(self):
		tracks = [self.track("A"), self.track("B", data=b"different")]
		self.manifest["health_preferences"] = {"distinct_recordings": [{"files": {t["persistent_id"]: health.file_sha256(Path(t["location"])) for t in tracks}}]}
		self.assertNotIn("semantic_duplicate", self.categories(self.report(tracks)))
		Path(tracks[1]["location"]).write_bytes(b"replacement")
		self.assertIn("semantic_duplicate", self.categories(self.report(tracks)))

	def test_legacy_profile_and_artwork_provenance_are_not_invented(self):
		track = self.track(managed=True)
		managed = self.manifest["recordings"]["A"]["managed_file"]
		managed.pop("metadata_profile")
		managed.pop("artwork_source_url")
		track.update(album="Original Album", track_no=4, compilation=False)
		self.tags.return_value = ({"title": "Song", "artist": "Artist", "album": "Original Album", "track": "4", "compilation": "0"}, [])
		self.assertEqual(self.report([track])["issues"], [])
		self.tags.return_value = (self.tags.return_value[0], ["Embedded artwork is missing."])
		self.assertIn("artwork_discrepancy", self.categories(self.report([track])))


if __name__ == "__main__":
	unittest.main()
