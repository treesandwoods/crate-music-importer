import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crate_music_importer.ipod_import.jobs import (
	JobStore,
	_commands,
	_raycast_deeplink,
	acknowledge_notification,
	cancel_incomplete,
	cancel_queued,
	cancel_source_progress,
	enqueue,
	jobs_snapshot,
	recover_interrupted,
	retry_job,
	run_queue,
	sync_from_manifest,
)
from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest, new_manifest, save_manifest, set_album, upsert_recording
from crate_music_importer.ipod_import.music_cache import build_full_cache, load_music_cache, save_music_cache, upsert_music_cache_track
from crate_music_importer.ipod_import.resolver import resolve_youtube


PLAYLIST_URL = "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1"
ALBUM_URL = "https://open.spotify.com/album/48a7rOjTzpD1zzJAteeveE"


class DurableJobTests(unittest.TestCase):
	def test_enqueue_snapshots_the_preexisting_music_cache(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			save_music_cache(paths, build_full_cache(paths, manifest, [{
				"persistent_id": "PID-BEFORE",
				"database_id": "1",
				"title": "Existing",
				"artist": "Artist",
				"duration_s": 180,
			}]))
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			self.assertEqual(job["cacheBaseline"], {"captured": True, "persistentIds": ["PID-BEFORE"]})

	def test_enqueue_seeds_not_started_track_names_for_immediate_status(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			job = enqueue(
				"album_combined",
				ALBUM_URL,
				root=root,
				popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()),
				seed={
					"name": "Album",
					"total": 1,
					"tracks": [{
						"position": 1,
						"trackNumber": 1,
						"discNumber": 1,
						"recordingId": "recording",
						"title": "Song",
						"artists": "Artist",
					}],
				},
			)
			self.assertEqual(job["source"]["name"], "Album")
			self.assertEqual(job["counts"]["notStarted"], 1)
			self.assertEqual(job["tracks"][0]["state"], "not_started")

	def test_enqueue_is_atomic_and_rejects_duplicate_active_source(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			popen = lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid())
			job = enqueue("playlist_combined", PLAYLIST_URL, root=root, popen=popen)
			self.assertEqual(job["status"], "queued")
			self.assertEqual(JobStore(root).load(job["jobId"])["source"]["id"], "37i9dQZF1DXTESTFIXTURE1")
			with self.assertRaisesRegex(ValueError, "already queued or running"):
				enqueue("playlist_combined", PLAYLIST_URL, root=root, popen=popen)

	def test_update_job_has_explicit_mode_and_routes_only_to_update_commands(self):
		with tempfile.TemporaryDirectory() as directory:
			job = enqueue(
				"playlist_update_combined",
				PLAYLIST_URL,
				root=Path(directory),
				popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()),
				seed={"savedPlaylistId": "saved-id", "name": "Saved", "total": 2},
			)
			self.assertEqual(job["mode"], "update")
			self.assertEqual(job["source"]["id"], "saved-id")
			self.assertEqual(_commands(job), [
				["playlist-update-prepare", "saved-id", "--confirm-download"],
				["playlist-update-apply", "saved-id", "--confirm-music-write"],
			])

	def test_cancelling_update_preserves_saved_playlist_progress(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			job = enqueue("playlist_update_combined", PLAYLIST_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()), seed={"savedPlaylistId": "saved-id"})
			result = cancel_incomplete(job["jobId"], root=root)
			self.assertEqual(result["removedRecordings"], 0)
			self.assertEqual(result["removedFiles"], 0)

	def test_one_runner_processes_fifo_jobs_in_order(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			popen = lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid())
			first = enqueue("playlist_combined", PLAYLIST_URL, root=root, popen=popen)
			second = enqueue("album_combined", ALBUM_URL, root=root, popen=popen)
			commands = []
			notifications = []

			def engine(command):
				commands.append(command)
				return 0

			run_queue(root=root, engine=engine, notifier=notifications.append)
			self.assertEqual(commands[0][0], "import")
			self.assertEqual(commands[2][0], "album-import")
			self.assertEqual(notifications, [first["jobId"], second["jobId"]])
			self.assertEqual(JobStore(root).load(first["jobId"])["status"], "complete")
			self.assertEqual(JobStore(root).load(second["jobId"])["status"], "complete")
			self.assertFalse(JobStore(root).runner_path.exists())

	def test_notification_failure_does_not_stop_the_fifo_queue(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			popen = lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid())
			first = enqueue("playlist_combined", PLAYLIST_URL, root=root, popen=popen)
			second = enqueue("album_combined", ALBUM_URL, root=root, popen=popen)
			commands = []

			def notify(_job_id):
				raise RuntimeError("Raycast unavailable")

			run_queue(root=root, engine=lambda command: commands.append(command) or 0, notifier=notify)
			self.assertEqual(len(commands), 4)
			self.assertTrue(JobStore(root).load(first["jobId"])["notification"]["pending"])
			self.assertTrue(JobStore(root).load(second["jobId"])["notification"]["pending"])

	def test_all_queued_jobs_share_one_runner_lock(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			launches = []

			def popen(*_args, **_kwargs):
				launches.append(True)
				return SimpleNamespace(pid=os.getpid())

			first = enqueue("playlist_combined", PLAYLIST_URL, root=root, popen=popen)
			second = enqueue("album_combined", ALBUM_URL, root=root, popen=popen)
			self.assertEqual(first["runnerPid"], os.getpid())
			self.assertEqual(second["runnerPid"], os.getpid())
			self.assertEqual(JobStore(root).runner()["pid"], os.getpid())
			self.assertEqual(len(launches), 1)
			self.assertLess(first["queueSequence"], second["queueSequence"])

	def test_fresh_starting_lock_is_never_replaced_by_a_second_runner(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			store = JobStore(root)
			store.state_dir.mkdir(parents=True)
			store.runner_path.write_text('{"pid": null, "status": "starting"}\n', encoding="utf-8")
			launches = []

			def popen(*_args, **_kwargs):
				launches.append(True)
				return SimpleNamespace(pid=os.getpid())

			job = enqueue("album_combined", ALBUM_URL, root=root, popen=popen)
			self.assertIsNone(job["runnerPid"])
			self.assertEqual(launches, [])
			self.assertEqual(store.runner()["status"], "starting")

	def test_mutation_lock_prevents_a_second_worker(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			store = JobStore(root)
			job = enqueue(
				"album_combined",
				ALBUM_URL,
				root=root,
				popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()),
			)
			commands = []
			with store.worker_lock() as acquired:
				self.assertTrue(acquired)
				run_queue(root=root, engine=lambda command: commands.append(command) or 0)
			self.assertEqual(commands, [])
			self.assertEqual(store.load(job["jobId"])["status"], "queued")

	def test_queued_job_can_be_removed_but_completed_job_cannot(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			self.assertEqual(cancel_queued(job["jobId"], root=root)["status"], "cancelled")
			completed = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			completed["status"] = "complete"
			JobStore(root).save(completed)
			with self.assertRaisesRegex(ValueError, "completed import"):
				cancel_incomplete(completed["jobId"], root=root)

	def test_cancelling_unfinished_import_deletes_unique_tool_owned_progress(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {
				"title": "Partial Song",
				"artists": "Artist",
				"album": "Partial Album",
				"album_id": "48a7rOjTzpD1zzJAteeveE",
				"duration_ms": 180000,
				"sp_id": "partial-track",
			}, source_type="album")
			relative = f"tracks/{key}.mp3"
			managed = root / relative
			managed.parent.mkdir(parents=True)
			managed.write_bytes(b"partial")
			(paths.staging / key).mkdir(parents=True)
			recording["managed_file"] = {"relative_path": relative, "tool_owned": True}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE",
				"name": "Partial Album",
				"url": ALBUM_URL,
				"tracks": [],
				"complete": True,
				"total_count": 1,
			}, [{"position": 1, "recording_id": key, "status": "downloaded"}])
			save_manifest(paths, manifest)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			job["status"] = "failed"
			job["phase"] = "failed"
			JobStore(root).save(job)

			result = cancel_incomplete(job["jobId"], root=root)

			self.assertEqual(result["removedRecordings"], 1)
			self.assertEqual(result["removedFiles"], 1)
			self.assertFalse(managed.exists())
			self.assertFalse((paths.staging / key).exists())
			self.assertFalse(JobStore(root).job_path(job["jobId"]).exists())
			saved = load_manifest(paths)
			self.assertNotIn("48a7rOjTzpD1zzJAteeveE", saved["albums"])
			self.assertNotIn(key, saved["recordings"])

	def test_cancelling_purges_only_new_importer_owned_music_and_cache_entries(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			save_music_cache(paths, build_full_cache(paths, manifest, [{
				"persistent_id": "PID-BEFORE",
				"database_id": "1",
				"title": "Existing",
				"artist": "Artist",
				"duration_s": 180,
			}]))
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			key, recording = upsert_recording(manifest, {
				"title": "New Song",
				"artists": "Artist",
				"album": "New Album",
				"album_id": "48a7rOjTzpD1zzJAteeveE",
				"duration_ms": 180000,
				"sp_id": "new-track",
			}, source_type="album")
			relative = f"tracks/{key}.mp3"
			managed = root / relative
			managed.parent.mkdir(parents=True)
			managed.write_bytes(b"new")
			recording["managed_file"] = {"relative_path": relative, "tool_owned": True}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			recording["music"] = {"source": "managed_album_import", "persistent_id": "PID-NEW", "database_id": "2", "location": str(managed)}
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE",
				"name": "New Album",
				"url": ALBUM_URL,
				"tracks": [],
				"complete": True,
				"total_count": 1,
			}, [{"position": 1, "recording_id": key, "status": "managed_ready"}])
			save_manifest(paths, manifest)
			upsert_music_cache_track(paths, {
				"persistent_id": "PID-NEW",
				"database_id": "2",
				"title": "New Song",
				"artist": "Artist",
				"duration_s": 180,
				"location": str(managed),
				"comment": f"Managed by Crate Music Importer; recording_id={key}",
			})
			job["status"] = "failed"
			job["phase"] = "failed"
			JobStore(root).save(job)
			deleted = []

			result = cancel_incomplete(
				job["jobId"],
				root=root,
				delete_music=lambda persistent_id, recording_id: deleted.append((persistent_id, recording_id)) or True,
				lookup_imported=lambda _recording_id: None,
			)

			self.assertEqual(deleted, [("PID-NEW", key)])
			self.assertEqual(result["removedMusicTracks"], 1)
			self.assertEqual(result["removedCacheEntries"], 1)
			self.assertFalse(managed.exists())
			self.assertNotIn(key, load_manifest(paths)["recordings"])
			self.assertEqual(set(load_music_cache(paths)["tracks"]), {"PID-BEFORE"})

	def test_cancelling_keeps_tracks_that_were_in_the_cache_before_the_attempt(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			save_music_cache(paths, build_full_cache(paths, manifest, [{
				"persistent_id": "PID-BEFORE",
				"database_id": "1",
				"title": "Existing",
				"artist": "Artist",
				"duration_s": 180,
			}]))
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			key, recording = upsert_recording(manifest, {
				"title": "Existing",
				"artists": "Artist",
				"album": "Album",
				"album_id": "48a7rOjTzpD1zzJAteeveE",
				"duration_ms": 180000,
				"sp_id": "existing-track",
			}, source_type="album")
			recording["music"] = {"source": "existing_library", "persistent_id": "PID-BEFORE", "database_id": "1"}
			recording["active_reference"] = {"kind": "existing_music", "persistent_id": "PID-BEFORE"}
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE",
				"name": "Album",
				"url": ALBUM_URL,
				"tracks": [],
				"complete": True,
				"total_count": 1,
			}, [{"position": 1, "recording_id": key, "status": "reused_music"}])
			save_manifest(paths, manifest)
			job["status"] = "failed"
			job["phase"] = "failed"
			JobStore(root).save(job)

			result = cancel_incomplete(
				job["jobId"],
				root=root,
				delete_music=lambda *_args: self.fail("preexisting Music must not be deleted"),
				lookup_imported=lambda _recording_id: None,
			)

			self.assertEqual(result["removedMusicTracks"], 0)
			self.assertEqual(result["removedCacheEntries"], 0)
			self.assertIn(key, load_manifest(paths)["recordings"])
			self.assertIn("PID-BEFORE", load_music_cache(paths)["tracks"])

	def test_cancelling_finds_and_removes_an_import_that_stopped_before_its_id_was_saved(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			save_music_cache(paths, build_full_cache(paths, manifest, []))
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			key, recording = upsert_recording(manifest, {
				"title": "Pending",
				"artists": "Artist",
				"album": "Album",
				"album_id": "48a7rOjTzpD1zzJAteeveE",
				"duration_ms": 180000,
				"sp_id": "pending-track",
			}, source_type="album")
			relative = f"tracks/{key}.mp3"
			managed = root / relative
			managed.parent.mkdir(parents=True)
			managed.write_bytes(b"pending")
			recording["managed_file"] = {"relative_path": relative, "tool_owned": True}
			recording["active_reference"] = {"kind": "managed_file", "relative_path": relative}
			recording["music_import_pending"] = {"relative_path": relative, "started_at": "fixture"}
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE",
				"name": "Album",
				"url": ALBUM_URL,
				"tracks": [],
				"complete": True,
				"total_count": 1,
			}, [{"position": 1, "recording_id": key, "status": "managed_ready"}])
			save_manifest(paths, manifest)
			job["status"] = "failed"
			job["phase"] = "failed"
			JobStore(root).save(job)
			deleted = []

			result = cancel_incomplete(
				job["jobId"],
				root=root,
				delete_music=lambda persistent_id, recording_id: deleted.append((persistent_id, recording_id)) or True,
				lookup_imported=lambda recording_id: {
					"persistent_id": "PID-PENDING",
					"comment": f"Managed by Crate Music Importer; recording_id={recording_id}",
				},
			)

			self.assertEqual(deleted, [("PID-PENDING", key)])
			self.assertEqual(result["removedMusicTracks"], 1)
			self.assertFalse(managed.exists())
			self.assertNotIn(key, load_manifest(paths)["recordings"])

	def test_retry_inherits_the_original_cache_baseline(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			save_music_cache(paths, build_full_cache(paths, manifest, [{
				"persistent_id": "PID-BEFORE",
				"database_id": "1",
				"title": "Existing",
				"artist": "Artist",
				"duration_s": 180,
			}]))
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			upsert_music_cache_track(paths, {
				"persistent_id": "PID-PARTIAL",
				"database_id": "2",
				"title": "Partial",
				"artist": "Artist",
				"duration_s": 180,
			})
			job["status"] = "failed"
			job["phase"] = "failed"
			job["retryable"] = True
			JobStore(root).save(job)

			retried = retry_job(job["jobId"], root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))

			self.assertEqual(retried["cacheBaseline"], {"captured": True, "persistentIds": ["PID-BEFORE"]})

	def test_ready_source_can_be_cancelled_when_legacy_job_says_complete(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			key, _recording = upsert_recording(manifest, {
				"title": "Still Pending",
				"artists": "Artist",
				"album": "Album",
				"album_id": "48a7rOjTzpD1zzJAteeveE",
				"duration_ms": 180000,
				"sp_id": "pending-track",
			}, source_type="album")
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE",
				"name": "Legacy Ready Album",
				"url": ALBUM_URL,
				"tracks": [],
				"complete": True,
				"total_count": 1,
			}, [{"position": 1, "recording_id": key, "status": "search_required"}])
			save_manifest(paths, manifest)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			job["status"] = "complete"
			job["phase"] = "complete"
			JobStore(root).save(job)

			result = cancel_source_progress("album", "48a7rOjTzpD1zzJAteeveE", root=root)

			self.assertEqual(result["sourceId"], "48a7rOjTzpD1zzJAteeveE")
			self.assertNotIn("48a7rOjTzpD1zzJAteeveE", load_manifest(paths)["albums"])
			self.assertEqual(JobStore(root).load(job["jobId"])["status"], "complete")

	def test_stale_running_job_becomes_retryable_instead_of_restarting(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			store = JobStore(root)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			job["status"] = "running"
			job["phase"] = "downloading"
			job["pid"] = 99999999
			store.save(job)
			recover_interrupted(store)
			recovered = store.load(job["jobId"])
			self.assertEqual(recovered["status"], "failed")
			self.assertTrue(recovered["retryable"])
			self.assertTrue(recovered["notification"]["pending"])

	def test_retry_creates_new_fifo_job_and_preserves_failed_record(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			popen = lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid())
			original = enqueue("album_combined", ALBUM_URL, root=root, popen=popen)
			original["status"] = "failed"
			original["phase"] = "failed"
			original["retryable"] = True
			JobStore(root).save(original)

			retried = retry_job(original["jobId"], root=root, popen=popen)
			preserved = JobStore(root).load(original["jobId"])
			self.assertNotEqual(retried["jobId"], original["jobId"])
			self.assertEqual(retried["status"], "queued")
			self.assertEqual(retried["retryOf"], original["jobId"])
			self.assertEqual(preserved["status"], "superseded")
			self.assertFalse(preserved["retryable"])
			self.assertEqual(preserved["supersededBy"], retried["jobId"])

	def test_track_states_explain_youtube_matching_download_and_approval(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			items = []
			for position, title in enumerate(("New", "Matched", "Approval", "Downloaded"), start=1):
				key, recording = upsert_recording(manifest, {
					"title": title,
					"artists": "Artist",
					"album": "Album",
					"album_id": "48a7rOjTzpD1zzJAteeveE",
					"duration_ms": 180000,
					"sp_id": f"track-{position}",
				}, source_type="album")
				items.append({"position": position, "recording_id": key, "status": "search_required"})
				if title == "Matched":
					recording["youtube"] = {"url": "https://www.youtube.com/watch?v=matched"}
				elif title == "Approval":
					recording["review"] = {"kind": "youtube_missing", "message": "Choose", "candidates": [{"url": "candidate"}]}
				elif title == "Downloaded":
					recording["active_reference"] = {"kind": "managed_file", "relative_path": "tracks/file.mp3"}
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE",
				"name": "Album",
				"url": ALBUM_URL,
				"tracks": [],
				"complete": True,
				"total_count": 4,
			}, items)
			save_manifest(paths, manifest)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			job["status"] = "running"
			states = [track["state"] for track in sync_from_manifest(job, JobStore(root))["tracks"]]
			self.assertEqual(states, ["not_started", "youtube_match_found", "matches_need_approval", "downloaded"])

	def test_approved_attention_job_is_retired_when_album_is_requeued_and_completed(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {
				"title": "Song",
				"artists": "Artist",
				"album": "Album",
				"album_id": "48a7rOjTzpD1zzJAteeveE",
				"duration_ms": 180000,
				"sp_id": "track-id",
			}, source_type="album")
			recording["review"] = {"kind": "youtube_missing", "message": "Choose", "candidates": []}
			recording["last_error"] = "No YouTube result scored strictly above 0.87."
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE",
				"name": "Album",
				"url": ALBUM_URL,
				"tracks": [],
				"complete": True,
				"total_count": 1,
			}, [{"position": 1, "recording_id": key, "status": "review_required"}])
			save_manifest(paths, manifest)
			popen = lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid())
			old = enqueue("album_combined", ALBUM_URL, root=root, popen=popen)
			old["status"] = "needs_attention"
			old["phase"] = "needs_attention"
			JobStore(root).save(old)

			resolve_youtube(key, "https://www.youtube.com/watch?v=chosen", paths=paths, inspect=lambda _url: {
				"video_id": "chosen",
				"url": "https://www.youtube.com/watch?v=chosen",
				"title": "Song",
				"uploader": "Artist",
				"duration_s": 180,
				"reasons": [],
			})
			resolved = next(job for job in jobs_snapshot(root=root)["jobs"] if job["jobId"] == old["jobId"])
			self.assertEqual(resolved["status"], "ready_to_continue")

			new = enqueue("album_combined", ALBUM_URL, root=root, popen=popen)
			self.assertEqual(JobStore(root).load(old["jobId"])["status"], "superseded")
			new["status"] = "complete"
			new["phase"] = "complete"
			JobStore(root).save(new)
			legacy_old = JobStore(root).load(old["jobId"])
			legacy_old["status"] = "needs_attention"
			legacy_old["phase"] = "needs_attention"
			legacy_old.pop("supersededBy", None)
			JobStore(root).save(legacy_old)
			statuses = [job["status"] for job in jobs_snapshot(root=root)["jobs"]]
			self.assertNotIn("needs_attention", statuses)
			self.assertEqual(JobStore(root).load(old["jobId"])["status"], "superseded")

	def test_terminal_notification_survives_reopen_and_acknowledges_once(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			popen = lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid())
			job = enqueue("playlist_combined", PLAYLIST_URL, root=root, popen=popen)
			run_queue(root=root, engine=lambda _command: 0, notifier=lambda _job_id: None)

			reopened = JobStore(root).load(job["jobId"])
			self.assertTrue(reopened["notification"]["pending"])
			acknowledge_notification(job["jobId"], root=root)
			reopened_again = JobStore(root).load(job["jobId"])
			self.assertFalse(reopened_again["notification"]["pending"])
			self.assertIsNotNone(reopened_again["notification"]["notifiedAt"])

	def test_terminal_deeplink_passes_only_durable_job_context(self):
		with patch.dict(os.environ, {"CRATE_RAYCAST_AUTHOR": "fixture-author", "CRATE_RAYCAST_EXTENSION": "fixture-extension"}):
			url = _raycast_deeplink("job-id")
		self.assertIn("fixture-author/fixture-extension", url)
		self.assertIn("import-activity-problems", url)
		self.assertIn("launchType=background", url)
		self.assertIn("job-id", url)
		self.assertNotIn("Album added", url)

	def test_terminal_notification_retries_failed_raycast_launches(self):
		from crate_music_importer.ipod_import.jobs import notify_raycast

		results = [SimpleNamespace(returncode=1), SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]
		calls = []
		delays = []
		with patch.dict(os.environ, {"CRATE_RAYCAST_AUTHOR": "fixture-author"}):
			notify_raycast(
				"job-id",
				opener=lambda *args, **kwargs: calls.append((args, kwargs)) or results.pop(0),
				sleeper=delays.append,
			)
		self.assertEqual(len(calls), 3)
		self.assertEqual(delays, [0.25, 0.5])

	def test_job_json_never_uses_partial_temporary_file_as_record(self):
		with tempfile.TemporaryDirectory() as directory:
			store = JobStore(Path(directory))
			job = {"jobId": "fixture", "createdAt": "1", "updatedAt": "1", "status": "queued"}
			store.save(job)
			json.loads(store.job_path("fixture").read_text(encoding="utf-8"))
			self.assertEqual(list(store.jobs_dir.glob("*.tmp")), [])

	def test_jobs_snapshot_omits_internal_baselines_and_limits_completed_history(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			store = JobStore(root)
			for index in range(55):
				store.save({
					"jobId": f"complete-{index}",
					"createdAt": f"{index:03d}",
					"status": "complete",
					"phase": "complete",
					"cacheBaseline": {"captured": True, "persistentIds": [f"PID-{value}" for value in range(100)]},
					"notification": {"pending": False},
				})
			store.save({
				"jobId": "superseded",
				"createdAt": "999",
				"status": "superseded",
				"phase": "superseded",
				"cacheBaseline": {"captured": True, "persistentIds": ["SECRET-INTERNAL-ID"]},
				"notification": {"pending": False},
			})

			snapshot = jobs_snapshot(root=root)

			self.assertEqual(len(snapshot["jobs"]), 50)
			self.assertNotIn("superseded", {job["status"] for job in snapshot["jobs"]})
			self.assertTrue(all("cacheBaseline" not in job for job in snapshot["jobs"]))
			self.assertNotIn("SECRET-INTERNAL-ID", json.dumps(snapshot))


if __name__ == "__main__":
	unittest.main()
