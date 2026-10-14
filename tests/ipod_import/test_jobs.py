import contextlib
import hashlib
import json
import os
import tempfile
import threading
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
	run_job,
	sync_from_manifest,
	update_structured_progress,
)
from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest, new_manifest, save_manifest, set_album, upsert_recording, update_manifest
from crate_music_importer.ipod_import.music_cache import build_full_cache, load_music_cache, save_music_cache, upsert_music_cache_track
from crate_music_importer.ipod_import.resolver import resolve_youtube


PLAYLIST_URL = "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1"
ALBUM_URL = "https://open.spotify.com/album/48a7rOjTzpD1zzJAteeveE"


class DurableJobTests(unittest.TestCase):
	def test_runner_does_not_overwrite_a_newer_startup_marker(self):
		with tempfile.TemporaryDirectory() as directory:
			store = JobStore(Path(directory))
			store.state_dir.mkdir(parents=True)

			def slow_start(*_args, **_kwargs):
				store.runner_path.write_text(json.dumps({"pid": 222, "status": "running"}), encoding="utf-8")
				return SimpleNamespace(pid=111)

			with patch("crate_music_importer.ipod_import.jobs._pid_running", return_value=False):
				pid = __import__("crate_music_importer.ipod_import.jobs", fromlist=["_ensure_runner"])._ensure_runner(store, popen=slow_start)

			self.assertEqual(pid, 111)
			self.assertEqual(store.runner()["pid"], 222)

	def test_dead_startup_owner_is_recovered_on_poll_without_launching_work(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			store = JobStore(root)
			store.state_dir.mkdir(parents=True)
			store.runner_path.write_text(json.dumps({
				"pid": None, "status": "starting", "startupToken": "dead-token", "starterPid": 123456,
			}), encoding="utf-8")
			queued = {"jobId": "queued", "status": "queued", "phase": "queued", "source": {"type": "album", "id": "album"}}
			store.save(queued)
			with patch("crate_music_importer.ipod_import.jobs._pid_running", return_value=False), patch(
				"crate_music_importer.ipod_import.jobs._ensure_runner",
				side_effect=AssertionError("polling must not start imports"),
			):
				snapshot = jobs_snapshot(root=root, manifest={})
			row = snapshot["jobs"][0]
			self.assertEqual(row["status"], "failed")
			self.assertTrue(row["retryable"])
			self.assertIn("during startup", row["errorSummary"])
			self.assertFalse(store.runner_path.exists())

	def test_enqueue_runner_pid_checkpoint_preserves_progress_written_during_spawn(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			store = JobStore(root)
			def spawn(*_args, **_kwargs):
				job = store.list()[0]
				job["phase"] = "worker_started"
				job["trackProgress"] = {"recording-1": "downloaded"}
				store.save(job)
				return SimpleNamespace(pid=987654)

			queued = enqueue("album_combined", ALBUM_URL, root=root, popen=spawn)

		self.assertEqual(queued["phase"], "worker_started")
		self.assertEqual(queued["trackProgress"], {"recording-1": "downloaded"})
		self.assertEqual(queued["runnerPid"], 987654)

	def test_crashed_cancellation_reservation_becomes_actionable_failed_history(self):
		with tempfile.TemporaryDirectory() as directory:
			store = JobStore(Path(directory))
			store.save({
				"jobId": "canceling",
				"status": "cancelling",
				"phase": "cancelling",
				"cancellationOwnerPid": 123456,
				"source": {"type": "album", "id": "source"},
			})
			with patch("crate_music_importer.ipod_import.jobs._pid_running", return_value=False):
				recover_interrupted(store)

			recovered = store.load("canceling")
			self.assertEqual(recovered["status"], "failed")
			self.assertFalse(recovered["retryable"])
			self.assertIn("Review this source", recovered["errorSummary"])
			self.assertTrue(recovered["notification"]["pending"])

	def test_activity_poll_recovers_dead_workers_from_its_single_job_snapshot(self):
		with tempfile.TemporaryDirectory() as directory:
			store = JobStore(Path(directory))
			store.save({
				"jobId": "dead-running", "status": "running", "pid": 123456,
				"source": {"type": "album", "id": "album-a"}, "notification": {"pending": False},
			})
			store.save({
				"jobId": "dead-cancelling", "status": "cancelling", "cancellationOwnerPid": 123457,
				"source": {"type": "album", "id": "album-b"}, "cancellationCleanup": {"rec": {"fileDeleteStartedAt": "checkpoint"}},
				"notification": {"pending": False},
			})
			original_list = JobStore.list
			with patch("crate_music_importer.ipod_import.jobs._pid_running", return_value=False), patch.object(
				JobStore, "list", autospec=True, side_effect=lambda instance: original_list(instance)
			) as listing:
				snapshot = jobs_snapshot(root=store.root, manifest={})
			self.assertEqual(listing.call_count, 1)
			rows = {job["jobId"]: job for job in snapshot["jobs"]}
			self.assertEqual(rows["dead-running"]["status"], "failed")
			self.assertTrue(rows["dead-running"]["retryable"])
			self.assertEqual(rows["dead-cancelling"]["status"], "failed")
			self.assertFalse(rows["dead-cancelling"]["retryable"])
			self.assertEqual(store.load("dead-cancelling")["cancellationCleanup"], {"rec": {"fileDeleteStartedAt": "checkpoint"}})

	def test_enqueue_between_final_empty_check_and_runner_release_is_not_stranded(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			store = JobStore(root)
			store.state_dir.mkdir(parents=True)
			store.runner_path.write_text(json.dumps({"pid": os.getpid(), "status": "running"}), encoding="utf-8")
			observed_empty = threading.Event()
			continue_runner = threading.Event()
			original_list = JobStore.list
			worker_thread = threading.current_thread()
			calls = {"count": 0, "done": False}

			def gated_list(instance):
				jobs = original_list(instance)
				if instance.root == root and threading.current_thread() is not worker_thread:
					calls["count"] += 1
				if instance.root == root and threading.current_thread() is not worker_thread and calls["count"] >= 2 and not calls["done"] and not any(job.get("status") == "queued" for job in jobs):
					calls["done"] = True
					observed_empty.set()
					continue_runner.wait(timeout=5)
				return jobs

			with patch.object(JobStore, "list", gated_list):
				worker = threading.Thread(target=lambda: run_queue(root=root, engine=lambda _command: 0, notifier=lambda _job_id: None))
				worker.start()
				self.assertTrue(observed_empty.wait(timeout=5))
				results = []
				launched_workers = []
				def launch(*_args, **_kwargs):
					next_worker = threading.Thread(target=lambda: run_queue(root=root, engine=lambda _command: 0, notifier=lambda _job_id: None))
					launched_workers.append(next_worker)
					next_worker.start()
					return SimpleNamespace(pid=os.getpid())
				launcher = threading.Thread(target=lambda: results.append(enqueue("album_combined", ALBUM_URL, root=root, popen=launch)))
				launcher.start()
				continue_runner.set()
				worker.join(timeout=5)
				launcher.join(timeout=5)
				for next_worker in launched_workers:
					next_worker.join(timeout=5)

			self.assertFalse(worker.is_alive())
			self.assertFalse(launcher.is_alive())
			job = results[0]
			self.assertEqual(JobStore(root).load(job["jobId"])["status"], "complete")
			self.assertFalse(JobStore(root).runner_path.exists())

	def test_cancel_rechecks_completion_before_removing_progress(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			store = JobStore(root)
			job = enqueue("playlist_update_combined", PLAYLIST_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()), seed={"savedPlaylistId": "saved-id"})
			job.update(status="running", pid=os.getpid())
			store.save(job)

			def mark_complete(_pid):
				current = store.load(job["jobId"])
				current.update(status="complete", phase="complete", pid=None)
				store.save(current)

			with patch("crate_music_importer.ipod_import.jobs._pid_running", side_effect=[True, False, False]):
				with self.assertRaisesRegex(ValueError, "completed import"):
					cancel_incomplete(job["jobId"], root=root, terminate=mark_complete)

			self.assertEqual(store.load(job["jobId"])["status"], "complete")

	def test_phase_only_progress_is_durable_even_without_manifest_source(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			store = JobStore(root)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			job["status"] = "running"
			store.save(job)
			update_structured_progress(job, store, {"phase": "loading_music_cache"})

			self.assertEqual(store.load(job["jobId"])["phase"], "loading_music_cache")

	def test_acknowledgement_updates_notification_without_replacing_concurrent_job_fields(self):
		with tempfile.TemporaryDirectory() as directory:
			store = JobStore(Path(directory))
			store.save({"jobId": "fixture", "status": "complete", "notification": {"pending": True, "notifiedAt": None}})
			locked = threading.Event()
			release = threading.Event()

			def concurrent_writer():
				with store.transaction_lock():
					current = store.load("fixture")
					current["phase"] = "newer-phase"
					locked.set()
					release.wait(timeout=5)
					store.save(current)

			writer = threading.Thread(target=concurrent_writer)
			writer.start()
			self.assertTrue(locked.wait(timeout=5))
			acknowledger = threading.Thread(target=lambda: acknowledge_notification("fixture", root=store.root))
			acknowledger.start()
			release.set()
			writer.join(timeout=5)
			acknowledger.join(timeout=5)

			self.assertEqual(store.load("fixture")["phase"], "newer-phase")
			self.assertFalse(store.load("fixture")["notification"]["pending"])

	def test_concurrent_retries_create_only_one_active_attempt(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			original = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			original.update(status="failed", retryable=True)
			JobStore(root).save(original)
			results = []
			start = threading.Barrier(3)

			def retry():
				start.wait(timeout=5)
				try:
					results.append(retry_job(original["jobId"], root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid())))
				except ValueError:
					results.append(None)

			threads = [threading.Thread(target=retry) for _ in range(2)]
			for thread in threads:
				thread.start()
			start.wait(timeout=5)
			for thread in threads:
				thread.join(timeout=5)

			self.assertEqual(sum(result is not None for result in results), 1)
			self.assertEqual(sum(job["status"] == "queued" for job in JobStore(root).list()), 1)

	def test_malformed_job_file_is_visible_without_hiding_valid_history(self):
		with tempfile.TemporaryDirectory() as directory:
			store = JobStore(Path(directory))
			store.save({"jobId": "valid", "createdAt": "2", "status": "complete"})
			store.jobs_dir.mkdir(parents=True, exist_ok=True)
			malformed = store.jobs_dir / "malformed.json"
			malformed.write_text("{", encoding="utf-8")

			jobs = store.list()
			self.assertEqual({job["jobId"] for job in jobs}, {"valid", "malformed"})
			placeholder = next(job for job in jobs if job["jobId"] == "malformed")
			self.assertEqual(placeholder["status"], "failed")
			self.assertIn("invalid JSON", placeholder["errorSummary"])
			self.assertEqual(malformed.read_text(encoding="utf-8"), "{")

	def test_job_ids_and_embedded_ids_cannot_escape_or_mismatch_job_files(self):
		with tempfile.TemporaryDirectory() as directory:
			store = JobStore(Path(directory))
			for job_id in ("../outside", "subdir/job", "\\outside", ".", ".."):
				with self.subTest(job_id=job_id), self.assertRaises(ValueError):
					store.job_path(job_id)
			store.jobs_dir.mkdir(parents=True)
			path = store.jobs_dir / "filename.json"
			original = b'{"jobId":"different-name","status":"running","source":{"type":"album","id":"a"}}\n'
			path.write_bytes(original)
			traversal = store.jobs_dir / "traversal.json"
			traversal_original = b'{"jobId":"../outside","status":"running","source":{"type":"album","id":"a"}}\n'
			traversal.write_bytes(traversal_original)
			jobs = {job["jobId"]: job for job in store.list()}
			job = jobs["filename"]
			self.assertEqual(job["jobId"], "filename")
			self.assertTrue(job["malformedRecord"])
			self.assertIn("invalid job ID", job["errorSummary"])
			self.assertEqual(path.read_bytes(), original)
			self.assertEqual(jobs["traversal"]["status"], "failed")
			self.assertEqual(traversal.read_bytes(), traversal_original)
			self.assertFalse((store.jobs_dir.parent / "outside.json").exists())

	def test_malformed_staging_recording_id_cannot_delete_outside_staging(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			manifest["recordings"]["../escape"] = {"id": "../escape"}
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE", "name": "Album", "url": ALBUM_URL,
				"tracks": [], "complete": True, "total_count": 1,
			}, [{"position": 1, "recording_id": "../escape", "status": "downloaded"}])
			save_manifest(paths, manifest)
			outside = root / "escape"
			outside.mkdir()
			(outside / "sentinel").write_text("keep", encoding="utf-8")
			job.update(status="failed", phase="failed")
			JobStore(root).save(job)
			with self.assertRaisesRegex(ValueError, "outside the managed staging directory"):
				cancel_incomplete(job["jobId"], root=root)
			self.assertEqual((outside / "sentinel").read_text(encoding="utf-8"), "keep")

	def test_malformed_job_object_is_visible_without_rewriting_original(self):
		with tempfile.TemporaryDirectory() as directory:
			store = JobStore(Path(directory))
			store.jobs_dir.mkdir(parents=True)
			path = store.job_path("malformed")
			original = b'{"jobId":"malformed","status":"running","source":[],"pid":"bad"}\n'
			path.write_bytes(original)

			job = store.list()[0]
			self.assertEqual(job["status"], "failed")
			self.assertEqual(job["source"], {})
			self.assertIn("original record was preserved", job["errorSummary"])
			self.assertEqual(jobs_snapshot(root=store.root, manifest={})["jobs"][0]["status"], "failed")
			self.assertEqual(path.read_bytes(), original)

	def test_malformed_active_job_fields_become_read_only_attention_placeholders(self):
		with tempfile.TemporaryDirectory() as directory:
			store = JobStore(Path(directory))
			store.jobs_dir.mkdir(parents=True)
			invalid_values = {
				"status-list": {"status": ["running"]},
				"source-type-list": {"status": "running", "source": {"type": [], "id": "album"}},
				"source-id-list": {"status": "running", "source": {"type": "album", "id": []}},
				"pid-string": {"status": "running", "source": {"type": "album", "id": "album"}, "pid": "123"},
				"pid-negative": {"status": "running", "source": {"type": "album", "id": "album"}, "pid": -1},
				"notification-list": {"status": "queued", "source": {"type": "album", "id": "album"}, "notification": []},
				"counts-list": {"status": "running", "source": {"type": "album", "id": "album"}, "counts": []},
				"tracks-object": {"status": "running", "source": {"type": "album", "id": "album"}, "tracks": {}},
			}
			originals = {}
			for job_id, fields in invalid_values.items():
				payload = {"jobId": job_id, **fields}
				originals[job_id] = json.dumps(payload).encode("utf-8")
				store.job_path(job_id).write_bytes(originals[job_id])

			with patch("crate_music_importer.ipod_import.jobs.load_manifest", return_value={}):
				snapshot = jobs_snapshot(root=store.root)
			self.assertEqual({job["jobId"] for job in snapshot["jobs"]}, set(invalid_values))
			self.assertTrue(all(job["status"] == "failed" and "original record was preserved" in job["errorSummary"] for job in snapshot["jobs"]))
			for job_id, original in originals.items():
				self.assertEqual(store.job_path(job_id).read_bytes(), original)

	def test_jobs_snapshot_uses_one_manifest_snapshot_for_many_attention_rows(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			store = JobStore(root)
			manifest = new_manifest(ManagedPaths(root))
			for index in range(100):
				store.save({
					"jobId": f"attention-{index}",
					"createdAt": f"{index:03d}",
					"source": {"type": "album", "id": "same-album", "name": "Album"},
					"status": "needs_attention",
					"counts": {"total": 1, "complete": 0, "review": 1, "failed": 0},
					"tracks": [],
					"notification": {"pending": False},
				})

			with patch("crate_music_importer.ipod_import.jobs.load_manifest", return_value=manifest) as load:
				snapshot = jobs_snapshot(root=root)

			self.assertEqual(load.call_count, 1)
			self.assertEqual(len(snapshot["jobs"]), 1)
			self.assertEqual(snapshot["jobs"][0]["jobId"], "attention-99")
			self.assertEqual(len(store.list()), 100)

	def test_large_snapshot_fixture_reads_one_manifest_and_one_job_list(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			recording_ids = []
			for index in range(500):
				key, _recording = upsert_recording(manifest, {
					"title": f"Fixture Track {index}",
					"artists": "Fixture Artist",
					"duration_ms": 180000,
				})
				recording_ids.append(key)
			store = JobStore(root)
			for index in range(100):
				source_id = f"fixture-album-{index}"
				manifest["albums"][source_id] = {
					"name": f"Fixture Album {index}",
					"items": [{"position": 0, "recording_id": recording_ids[index], "status": "review"}],
				}
				store.save({
					"jobId": f"fixture-job-{index}",
					"createdAt": f"{index:03}",
					"source": {"type": "album", "id": source_id, "name": f"Fixture Album {index}"},
					"action": "album_combined",
					"status": "needs_attention",
					"phase": "needs_attention",
					"counts": {"total": 1, "complete": 0, "review": 1, "failed": 0},
					"tracks": [],
					"notification": {"pending": False},
				})
			save_manifest(paths, manifest)

			original_list = JobStore.list
			with patch("crate_music_importer.ipod_import.jobs.load_manifest", wraps=load_manifest) as load, patch.object(
				JobStore, "list", autospec=True, side_effect=lambda instance: original_list(instance)
			) as listing:
				snapshot = jobs_snapshot(root=root)

			self.assertEqual(load.call_count, 1)
			self.assertEqual(listing.call_count, 1)
			self.assertEqual(len(snapshot["jobs"]), 100)
			self.assertEqual(len(original_list(store)), 100)

	def test_jobs_snapshot_reuses_supplied_reconciled_jobs(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			jobs = [{"jobId": "fixture", "status": "complete", "notification": {"pending": False}}]
			with patch("crate_music_importer.ipod_import.jobs.reconcile_terminal_jobs", side_effect=AssertionError("must reuse supplied jobs")):
				snapshot = jobs_snapshot(root=root, manifest={}, jobs=jobs)

		self.assertEqual([job["jobId"] for job in snapshot["jobs"]], ["fixture"])

	def test_job_atomic_write_failure_keeps_previous_record_and_cleans_temporary_file(self):
		with tempfile.TemporaryDirectory() as directory:
			store = JobStore(Path(directory))
			store.save({"jobId": "fixture", "status": "queued"})
			before = store.job_path("fixture").read_bytes()
			with patch("crate_music_importer.ipod_import.jobs.json.dump", side_effect=OSError("simulated disk failure")):
				with self.assertRaisesRegex(OSError, "simulated disk failure"):
					store.save({"jobId": "fixture", "status": "complete"})

			self.assertEqual(store.job_path("fixture").read_bytes(), before)
			self.assertEqual(list(store.jobs_dir.glob("*.tmp")), [])

	def test_completed_track_checkpoint_survives_later_progress_and_polling(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			keys = []
			for title in ["First", "Second"]:
				key, recording = upsert_recording(manifest, {"title": title, "artists": "Artist", "duration_ms": 180000})
				recording["music_binding"] = {"persistent_id": title}
				keys.append(key)
			set_album(manifest, {"id": "48a7rOjTzpD1zzJAteeveE", "name": "Album", "complete": True}, [{"position": i + 1, "recording_id": key} for i, key in enumerate(keys)])
			save_manifest(paths, manifest)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_a, **_kw: SimpleNamespace(pid=os.getpid()))
			job["status"] = "running"
			store = JobStore(root)
			update_structured_progress(job, store, {"phase": "complete", "recording_id": keys[0], "position": 1})
			update_structured_progress(job, store, {"phase": "adding_to_music", "recording_id": keys[1], "position": 2})
			polled = sync_from_manifest(store.load(job["jobId"]), store)
			self.assertEqual([t["state"] for t in polled["tracks"]], ["complete", "adding_to_music"])
			self.assertEqual(polled["counts"]["complete"], 1)

	def test_unverified_music_binding_is_not_counted_as_complete(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {"title": "Song", "artists": "Artist", "duration_ms": 180000})
			recording["music_binding"] = {"persistent_id": "PID"}
			recording["music_import_pending"] = {"relative_path": "Music/Song.mp3"}
			set_album(manifest, {"id": "48a7rOjTzpD1zzJAteeveE", "name": "Album", "complete": True}, [{"position": 1, "recording_id": key}])
			save_manifest(paths, manifest)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_a, **_kw: SimpleNamespace(pid=os.getpid()))
			job["status"] = "complete"
			polled = sync_from_manifest(job, JobStore(root))
			self.assertEqual(polled["tracks"][0]["state"], "checking_music_ids")
			self.assertEqual(polled["counts"]["complete"], 0)

	def test_album_apply_exception_marks_the_track_failed_and_keeps_the_job_retryable(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			key, recording = upsert_recording(manifest, {"title": "Song", "artists": "Artist", "duration_ms": 180000})
			recording["managed_file"] = {"relative_path": "Music/Song.mp3"}
			set_album(manifest, {"id": "48a7rOjTzpD1zzJAteeveE", "name": "Album", "complete": True}, [{"position": 1, "recording_id": key}])
			save_manifest(paths, manifest)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_a, **_kw: SimpleNamespace(pid=os.getpid()))
			store = JobStore(root)
			def engine(command):
				if command[0] == "album-import":
					return 0
				update_structured_progress(job, store, {"phase": "adding_to_music", "recording_id": key})
				raise RuntimeError("Music read failed")
			finished = run_job(job, store, engine=engine, notifier=lambda _id: None)
			self.assertEqual(finished["status"], "failed")
			self.assertTrue(finished["retryable"])
			self.assertEqual(finished["tracks"][0]["state"], "failed")
			self.assertEqual(finished["tracks"][0]["detail"], "Music read failed")
			self.assertEqual(finished["errorSummary"], "Music read failed")
			self.assertEqual(finished["counts"]["adding"], 0)

	def setUp(self):
		lock = patch("crate_music_importer.ipod_import.dependency_lock.dependency_lock", return_value=contextlib.nullcontext())
		lock.start()
		self.addCleanup(lock.stop)
		music_locks = [
			patch("crate_music_importer.ipod_import.music_lock.music_operation_lock", return_value=contextlib.nullcontext()),
			patch("crate_music_importer.ipod_import.jobs.music_operation_lock", return_value=contextlib.nullcontext()),
			patch("crate_music_importer.ipod_import.music_cache.music_operation_lock", return_value=contextlib.nullcontext()),
		]
		for music_lock in music_locks:
			music_lock.start()
			self.addCleanup(music_lock.stop)

	def test_enqueue_snapshots_preexisting_manifest_bindings_without_using_cache_as_truth(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			_key, recording = upsert_recording(manifest, {"title": "Existing", "artists": "Artist", "duration_ms": 180000})
			recording["music_binding"] = {"persistent_id": "PID-BEFORE"}
			save_manifest(paths, manifest)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			self.assertEqual(job["stateBaseline"], {"captured": True, "persistentIds": ["PID-BEFORE"]})

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

	def test_update_continuation_and_retry_recover_the_saved_confirmation(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			manifest["playlists"]["saved-id"] = {"name": "Saved", "items": [], "pending_update": {"confirmation_token": "confirmed", "addition_items": [], "music_checkpoint": {"baseline": ["PID"]}}}
			save_manifest(paths, manifest)
			popen = lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid())
			job = enqueue("playlist_update_combined", PLAYLIST_URL, root=root, popen=popen, seed={"savedPlaylistId": "saved-id"})
			self.assertEqual(job["confirmationToken"], "confirmed")
			self.assertTrue(job["resumeUpdate"])
			self.assertEqual(_commands(job)[0], ["playlist-update-prepare", "saved-id", "--confirm-download", "--confirmation-token", "confirmed", "--resume"])
			job["status"] = "failed"
			job["retryable"] = True
			job["confirmationToken"] = None  # Legacy Continue jobs omitted the token.
			JobStore(root).save(job)
			retried = retry_job(job["jobId"], root=root, popen=popen)
			self.assertEqual(retried["confirmationToken"], "confirmed")
			self.assertTrue(retried["resumeUpdate"])
			self.assertEqual(load_manifest(paths)["playlists"]["saved-id"]["pending_update"]["music_checkpoint"], {"baseline": ["PID"]})

	def test_fresh_update_confirmation_does_not_resume_an_older_plan(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			manifest["playlists"]["saved-id"] = {"pending_update": {"confirmation_token": "old"}}
			save_manifest(paths, manifest)
			job = enqueue("playlist_update_combined", PLAYLIST_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()), seed={"savedPlaylistId": "saved-id", "confirmationToken": "new"})
			self.assertEqual(job["confirmationToken"], "new")
			self.assertNotIn("--resume", _commands(job)[0])

	def test_snapshot_retires_old_attention_attempts_even_when_newer_attempt_failed(self):
		with tempfile.TemporaryDirectory() as directory:
			store = JobStore(Path(directory))
			for job_id, created, status in (("old", "1", "needs_attention"), ("new", "2", "failed")):
				store.save({"jobId": job_id, "createdAt": created, "source": {"type": "album", "id": "album"}, "status": status, "errorSummary": "failure", "notification": {"pending": True}})
			snapshot = jobs_snapshot(root=store.root)
			self.assertEqual([job["jobId"] for job in snapshot["jobs"]], ["new"])
			self.assertEqual(store.load("old")["status"], "superseded")
			self.assertFalse(store.load("old")["notification"]["pending"])

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

	def test_fresh_startup_marker_with_dead_owner_is_replaced_immediately(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			store = JobStore(root)
			store.state_dir.mkdir(parents=True)
			store.runner_path.write_text(json.dumps({
				"pid": None, "status": "starting", "startupToken": "abandoned", "starterPid": 123456,
			}), encoding="utf-8")
			with patch("crate_music_importer.ipod_import.jobs._pid_running", return_value=False):
				pid = __import__("crate_music_importer.ipod_import.jobs", fromlist=["_ensure_runner"])._ensure_runner(
					store, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=987654),
				)
			self.assertEqual(pid, 987654)
			self.assertEqual(store.runner()["pid"], 987654)

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

	def test_cancelling_unfinished_import_deletes_unique_crate_managed_progress(self):
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
			recording["managed_file"] = {"relative_path": relative, "managed_by_crate": True, "sha256": hashlib.sha256(b"partial").hexdigest()}
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

	def test_cancelling_purges_only_new_music_backed_by_a_verified_managed_file(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			save_music_cache(paths, build_full_cache(paths, [{
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
			recording["managed_file"] = {"relative_path": relative, "managed_by_crate": True, "sha256": hashlib.sha256(b"new").hexdigest()}
			recording["music_binding"] = {"persistent_id": "PID-NEW"}
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
			music = [{"persistent_id": "PID-NEW", "location": str(managed)}]
			def exact_lookup(persistent_id):
				return next((track for track in music if track["persistent_id"] == persistent_id), None)
			def delete_track(persistent_id, managed_path):
				deleted.append((persistent_id, str(managed_path)))
				music[:] = [track for track in music if track["persistent_id"] != persistent_id]
				return True

			result = cancel_incomplete(
				job["jobId"],
				root=root,
				delete_music=delete_track,
				exact_lookup=exact_lookup,
			)

			self.assertEqual(deleted, [("PID-NEW", str(managed.resolve()))])
			self.assertEqual(result["removedMusicTracks"], 1)
			self.assertEqual(result["removedCacheEntries"], 1)
			self.assertFalse(managed.exists())
			self.assertNotIn(key, load_manifest(paths)["recordings"])
			self.assertEqual(set(load_music_cache(paths)["tracks"]), {"PID-BEFORE"})

	def test_cancel_retry_recovers_file_unlink_before_checkpoint(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			key, recording = upsert_recording(manifest, {
				"title": "Interrupted Delete", "artists": "Artist", "album": "Album",
				"album_id": "48a7rOjTzpD1zzJAteeveE", "duration_ms": 180000,
			}, source_type="album")
			managed = root / f"tracks/{key}.mp3"
			managed.parent.mkdir(parents=True)
			managed.write_bytes(b"verified cancellation audio")
			recording["managed_file"] = {
				"relative_path": f"tracks/{key}.mp3", "managed_by_crate": True,
				"sha256": hashlib.sha256(managed.read_bytes()).hexdigest(),
			}
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE", "name": "Album", "url": ALBUM_URL,
				"tracks": [], "complete": True, "total_count": 1,
			}, [{"position": 1, "recording_id": key, "status": "downloaded"}])
			save_manifest(paths, manifest)
			job.update(status="failed", phase="failed")
			JobStore(root).save(job)
			delete_file = __import__("crate_music_importer.ipod_import.jobs", fromlist=["_delete_managed_file"])._delete_managed_file
			def crash_after_unlink(*args):
				result = delete_file(*args)
				raise RuntimeError("simulated crash after unlink")

			with patch("crate_music_importer.ipod_import.jobs._delete_managed_file", side_effect=crash_after_unlink), self.assertRaisesRegex(RuntimeError, "after unlink"):
				cancel_incomplete(job["jobId"], root=root)

			self.assertFalse(managed.exists())
			self.assertIn(key, load_manifest(paths)["recordings"])
			recovered = cancel_incomplete(job["jobId"], root=root)
			self.assertEqual(recovered["removedRecordings"], 1)
			self.assertNotIn(key, load_manifest(paths)["recordings"])

	def test_cancel_retry_recovers_music_delete_before_file_unlink(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			key, recording = upsert_recording(manifest, {
				"title": "Interrupted Music Delete", "artists": "Artist", "album": "Album",
				"album_id": "48a7rOjTzpD1zzJAteeveE", "duration_ms": 180000,
			}, source_type="album")
			managed = root / f"tracks/{key}.mp3"
			managed.parent.mkdir(parents=True)
			managed.write_bytes(b"verified cancellation audio")
			recording["managed_file"] = {
				"relative_path": f"tracks/{key}.mp3", "managed_by_crate": True,
				"sha256": hashlib.sha256(managed.read_bytes()).hexdigest(),
			}
			recording["music_binding"] = {"persistent_id": "PID-CANCEL"}
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE", "name": "Album", "url": ALBUM_URL,
				"tracks": [], "complete": True, "total_count": 1,
			}, [{"position": 1, "recording_id": key, "status": "managed_ready"}])
			save_manifest(paths, manifest)
			job.update(status="failed", phase="failed")
			JobStore(root).save(job)
			music = {"persistent_id": "PID-CANCEL", "location": str(managed.resolve())}
			delete_calls = []
			def delete_music(pid, _path):
				delete_calls.append(pid)
				music.clear()
				raise RuntimeError("simulated crash after Music deletion")
			def exact_lookup(pid):
				return music if music.get("persistent_id") == pid else None

			with self.assertRaisesRegex(RuntimeError, "after Music deletion"):
				cancel_incomplete(job["jobId"], root=root, delete_music=delete_music, exact_lookup=exact_lookup)
			self.assertTrue(managed.exists())

			with patch("crate_music_importer.ipod_import.jobs.remove_music_cache_tracks", return_value=1):
				result = cancel_incomplete(
					job["jobId"], root=root,
					delete_music=lambda *_args: self.fail("Music deletion must not be repeated"),
					exact_lookup=exact_lookup,
				)
			self.assertEqual(delete_calls, ["PID-CANCEL"])
			self.assertEqual(result["removedMusicTracks"], 1)
			self.assertFalse(managed.exists())
			self.assertNotIn(key, load_manifest(paths)["recordings"])

	def test_cancel_rechecks_references_before_deleting_shared_recording(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			key, recording = upsert_recording(manifest, {
				"title": "Shared Track", "artists": "Artist", "album": "Album",
				"album_id": "48a7rOjTzpD1zzJAteeveE", "duration_ms": 180000,
			}, source_type="album")
			managed = root / f"tracks/{key}.mp3"
			managed.parent.mkdir(parents=True)
			managed.write_bytes(b"verified cancellation audio")
			recording["managed_file"] = {
				"relative_path": f"tracks/{key}.mp3", "managed_by_crate": True,
				"sha256": hashlib.sha256(managed.read_bytes()).hexdigest(),
			}
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE", "name": "Album", "url": ALBUM_URL,
				"tracks": [], "complete": True, "total_count": 1,
			}, [{"position": 1, "recording_id": key, "status": "downloaded"}])
			save_manifest(paths, manifest)
			job.update(status="failed", phase="failed")
			JobStore(root).save(job)
			original_load = load_manifest
			calls = {"count": 0}
			def add_new_reference(managed_paths):
				calls["count"] += 1
				if calls["count"] == 2:
					def add_reference(latest):
						latest["recordings"][key].setdefault("playlist_memberships", {})["new-source"] = [1]
					update_manifest(paths, add_reference)
				return original_load(managed_paths)

			with patch("crate_music_importer.ipod_import.jobs.load_manifest", side_effect=add_new_reference):
				result = cancel_incomplete(
					job["jobId"], root=root,
					delete_music=lambda *_args: self.fail("shared Music must remain"),
					exact_lookup=lambda _pid: self.fail("shared Music must remain"),
				)
			self.assertEqual(result["removedRecordings"], 0)
			self.assertTrue(managed.exists())
			self.assertNotIn("48a7rOjTzpD1zzJAteeveE", load_manifest(paths)["albums"])
			self.assertEqual(load_manifest(paths)["recordings"][key]["playlist_memberships"], {"new-source": [1]})

	def test_cancel_preserves_other_collection_membership_with_same_source_id(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			key, recording = upsert_recording(manifest, {
				"title": "Shared Track", "artists": "Artist", "album": "Album",
				"album_id": "48a7rOjTzpD1zzJAteeveE", "duration_ms": 180000,
			}, source_type="album")
			managed = root / f"tracks/{key}.mp3"
			managed.parent.mkdir(parents=True)
			managed.write_bytes(b"verified cancellation audio")
			recording["managed_file"] = {
				"relative_path": f"tracks/{key}.mp3", "managed_by_crate": True,
				"sha256": hashlib.sha256(managed.read_bytes()).hexdigest(),
			}
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE", "name": "Album", "url": ALBUM_URL,
				"tracks": [], "complete": True, "total_count": 1,
			}, [{"position": 1, "recording_id": key, "status": "downloaded"}])
			recording["playlist_memberships"] = {"48a7rOjTzpD1zzJAteeveE": [1]}
			save_manifest(paths, manifest)
			job.update(status="failed", phase="failed")
			JobStore(root).save(job)
			result = cancel_incomplete(
				job["jobId"], root=root,
				delete_music=lambda *_args: self.fail("shared Music must remain"),
				exact_lookup=lambda _pid: self.fail("shared Music must remain"),
			)
			self.assertEqual(result["removedRecordings"], 0)
			self.assertTrue(managed.exists())
			self.assertIn("48a7rOjTzpD1zzJAteeveE", load_manifest(paths)["recordings"][key]["playlist_memberships"])

	def test_cancelling_keeps_bindings_that_existed_before_the_attempt(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			save_music_cache(paths, build_full_cache(paths, [{
				"persistent_id": "PID-BEFORE",
				"database_id": "1",
				"title": "Existing",
				"artist": "Artist",
				"duration_s": 180,
			}]))
			key, recording = upsert_recording(manifest, {
				"title": "Existing",
				"artists": "Artist",
				"album": "Album",
				"album_id": "48a7rOjTzpD1zzJAteeveE",
				"duration_ms": 180000,
				"sp_id": "existing-track",
			}, source_type="album")
			recording["music_binding"] = {"persistent_id": "PID-BEFORE"}
			set_album(manifest, {
				"id": "48a7rOjTzpD1zzJAteeveE",
				"name": "Album",
				"url": ALBUM_URL,
				"tracks": [],
				"complete": True,
				"total_count": 1,
			}, [{"position": 1, "recording_id": key, "status": "reused_music"}])
			save_manifest(paths, manifest)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			job["status"] = "failed"
			job["phase"] = "failed"
			JobStore(root).save(job)

			result = cancel_incomplete(
				job["jobId"],
				root=root,
				delete_music=lambda *_args: self.fail("preexisting Music must not be deleted"),
				exact_lookup=lambda _persistent_id: None,
			)

			self.assertEqual(result["removedMusicTracks"], 0)
			self.assertEqual(result["removedCacheEntries"], 0)
			self.assertIn(key, load_manifest(paths)["recordings"])
			self.assertIn("PID-BEFORE", load_music_cache(paths)["tracks"])

	def test_cancelling_does_not_guess_music_id_when_binding_was_never_saved(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			save_music_cache(paths, build_full_cache(paths, []))
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
			recording["managed_file"] = {"relative_path": relative, "managed_by_crate": True, "sha256": hashlib.sha256(b"pending").hexdigest()}
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
				delete_music=lambda persistent_id, managed_path: deleted.append((persistent_id, managed_path)) or True,
				exact_lookup=lambda _persistent_id: None,
			)

			self.assertEqual(deleted, [])
			self.assertEqual(result["removedMusicTracks"], 0)
			self.assertFalse(managed.exists())
			self.assertNotIn(key, load_manifest(paths)["recordings"])

	def test_retry_inherits_the_original_state_baseline(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			paths = ManagedPaths(root)
			manifest = new_manifest(paths)
			_key, recording = upsert_recording(manifest, {"title": "Existing", "artists": "Artist", "duration_ms": 180000})
			recording["music_binding"] = {"persistent_id": "PID-BEFORE"}
			save_manifest(paths, manifest)
			job = enqueue("album_combined", ALBUM_URL, root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))
			job["status"] = "failed"
			job["phase"] = "failed"
			job["retryable"] = True
			JobStore(root).save(job)

			retried = retry_job(job["jobId"], root=root, popen=lambda *_args, **_kwargs: SimpleNamespace(pid=os.getpid()))

			self.assertEqual(retried["stateBaseline"], {"captured": True, "persistentIds": ["PID-BEFORE"]})

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
					recording["managed_file"] = {"managed_by_crate": True, "relative_path": "tracks/file.mp3"}
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

	def test_terminal_deeplink_targets_completion_notifications_and_passes_only_durable_job_context(self):
		with patch.dict(os.environ, {"CRATE_RAYCAST_AUTHOR": "fixture-author", "CRATE_RAYCAST_EXTENSION": "fixture-extension"}):
			url = _raycast_deeplink("job-id")
			self.assertIn("fixture-author/fixture-extension", url)
			self.assertIn("completion-notifications", url)
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

	def test_jobs_snapshot_omits_internal_baselines_and_keeps_all_completed_history(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			store = JobStore(root)
			for index in range(55):
				store.save({
					"jobId": f"complete-{index}",
					"createdAt": f"{index:03d}",
					"status": "complete",
					"phase": "complete",
					"stateBaseline": {"captured": True, "persistentIds": [f"PID-{value}" for value in range(100)]},
					"notification": {"pending": False},
				})
			store.save({
				"jobId": "superseded",
				"createdAt": "999",
				"status": "superseded",
				"phase": "superseded",
				"stateBaseline": {"captured": True, "persistentIds": ["SECRET-INTERNAL-ID"]},
				"notification": {"pending": False},
			})

			snapshot = jobs_snapshot(root=root)

			self.assertEqual(len(snapshot["jobs"]), 55)
			self.assertNotIn("superseded", {job["status"] for job in snapshot["jobs"]})
			self.assertTrue(all("stateBaseline" not in job for job in snapshot["jobs"]))
			self.assertNotIn("SECRET-INTERNAL-ID", json.dumps(snapshot))


if __name__ == "__main__":
	unittest.main()
