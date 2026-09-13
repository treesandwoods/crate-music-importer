import tempfile
import unittest
from pathlib import Path

from crate_music_importer.ipod_import.manifest import ManagedPaths, new_manifest, set_album, set_playlist, upsert_recording
from crate_music_importer.ipod_import.music import MusicAutomationError
from crate_music_importer.ipod_import.playlist_update import apply_playlist_update, build_playlist_update_preview, save_pending_update, saved_playlists


URL = "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1"


def track(spotify_id, title=None):
	return {"sp_id": spotify_id, "title": title or spotify_id, "artists": "Artist", "album": "Album", "duration_ms": 180000}


class PlaylistUpdateTests(unittest.TestCase):
	def fixture(self, ids=("a", "b")):
		root = Path(self.directory.name)
		paths = ManagedPaths(root)
		manifest = new_manifest(paths)
		items = []
		music = []
		for position, spotify_id in enumerate(ids, start=1):
			key, recording = upsert_recording(manifest, track(spotify_id))
			persistent_id = f"PID-{spotify_id.upper()}"
			recording["music"] = {"source": "existing_library", "persistent_id": persistent_id, "database_id": str(position), "location": f"/Music/{spotify_id}.m4a"}
			recording["active_reference"] = {"kind": "existing_music", "persistent_id": persistent_id}
			items.append({"position": position, "recording_id": key, "spotify_id": spotify_id, "status": "reused_music"})
			music.append({"title": spotify_id, "artist": "Artist", "album": "Album", "duration_s": 180, "persistent_id": persistent_id, "database_id": str(position), "location": f"/Music/{spotify_id}.m4a", "comment": ""})
		playlist = set_playlist(manifest, {"id": "saved", "name": "Saved", "url": URL, "complete": True, "total_count": len(items)}, items)
		playlist["music_playlist_persistent_id"] = "PLAYLIST-PID"
		return paths, manifest, music

	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()

	def tearDown(self):
		self.directory.cleanup()

	def preview(self, manifest, music, current_ids, **values):
		current = {"id": "saved", "name": "Saved", "url": URL, "tracks": [track(value) for value in current_ids], "total_count": len(current_ids), "complete": True}
		current.update(values)
		return build_playlist_update_preview(current, music, manifest, ManagedPaths(Path(self.directory.name)), status_checker=lambda _name, pid: ("OWNED", pid))

	def test_lists_saved_playlists_with_count_and_link(self):
		_paths, manifest, _music = self.fixture(("a", "a", "b"))
		self.assertEqual(saved_playlists(manifest)[0]["track_count"], 3)
		self.assertEqual(saved_playlists(manifest)[0]["spotify_url"], URL)

	def test_first_update_backfills_and_reorder_is_noop(self):
		_paths, manifest, music = self.fixture(("a", "b"))
		preview = self.preview(manifest, music, ("b", "a"))
		self.assertTrue(preview.baseline_backfilled)
		self.assertEqual(preview.state, "up_to_date")
		self.assertEqual(preview.additions, [])
		self.assertEqual(preview.removals, [])

	def test_occurrence_diff_detects_duplicate_addition_and_removal(self):
		_paths, manifest, music = self.fixture(("a", "b"))
		added = self.preview(manifest, music, ("a", "a", "b"))
		self.assertEqual([row["spotify_id"] for row in added.additions], ["a"])
		removed = self.preview(manifest, music, ("b",))
		self.assertEqual([row["spotify_id"] for row in removed.removals], ["a"])

	def test_addition_snapshot_records_its_saved_position_for_future_updates(self):
		_paths, manifest, music = self.fixture(("a", "b"))
		preview = self.preview(manifest, music, ("a", "b", "c"))
		addition = preview.addition_items[0]
		snapshot = preview.spotify_snapshot[2]
		self.assertEqual(snapshot["recording_id"], addition["recording_id"])
		self.assertEqual(snapshot["saved_position"], addition["position"])

	def test_partial_legacy_baseline_defers_all_removals(self):
		_paths, manifest, music = self.fixture(("a", "b"))
		manifest["playlists"]["saved"]["items"][1].pop("spotify_id")
		preview = self.preview(manifest, music, ("a",))
		self.assertTrue(preview.removals_deferred)
		self.assertEqual(preview.removals, [])
		self.assertIn("additions-only", preview.warning)

	def test_incomplete_fetch_blocks_even_when_spotify_reports_more(self):
		_paths, manifest, music = self.fixture(("a", "b"))
		preview = self.preview(manifest, music, ("a",), total_count=2)
		self.assertEqual(preview.state, "incomplete_data")
		self.assertTrue(preview.to_dict()["blocked"])

	def test_missing_music_playlist_and_collision_are_blocked(self):
		paths, manifest, music = self.fixture(("a",))
		manifest["playlists"]["saved"]["music_playlist_persistent_id"] = None
		missing = build_playlist_update_preview({"id": "saved", "name": "Saved", "url": URL, "tracks": [track("a")], "total_count": 1, "complete": True}, music, manifest, paths, status_checker=lambda _name, pid: ("OWNED", pid))
		self.assertEqual(missing.state, "missing_music_playlist")
		manifest["playlists"]["saved"]["music_playlist_persistent_id"] = "PID"
		collision = build_playlist_update_preview({"id": "saved", "name": "Saved", "url": URL, "tracks": [track("a")], "total_count": 1, "complete": True}, music, manifest, paths, status_checker=lambda _name, _pid: ("COLLISION", "OTHER"))
		self.assertEqual(collision.state, "playlist_collision")

	def test_removal_reference_count_selects_unlink_or_permanent_delete(self):
		paths, manifest, music = self.fixture(("a", "b", "c"))
		items = manifest["playlists"]["saved"]["items"]
		by_spotify = {item["spotify_id"]: item["recording_id"] for item in items}
		manifest["recordings"][by_spotify["a"]]["playlist_memberships"]["other"] = [1]
		manifest["playlists"]["other"] = {"name": "Other", "items": [{"position": 1, "recording_id": by_spotify["a"], "spotify_id": "a"}]}
		set_album(manifest, {"id": "album", "name": "Album", "url": "url", "complete": True, "total_count": 1}, [{"position": 1, "recording_id": by_spotify["b"], "spotify_id": "b"}])
		c = manifest["recordings"][by_spotify["c"]]
		c["managed_file"] = {"relative_path": "tracks/c.mp3", "tool_owned": True, "metadata_profile": "playlist", "audio_sha256": "audio-c"}
		c["active_reference"] = {"kind": "managed_file", "relative_path": "tracks/c.mp3"}
		c["music"]["source"] = "managed_import"
		preview = self.preview(manifest, music, ())
		self.assertEqual({row["spotify_id"]: row["action"] for row in preview.removals}, {"a": "unlink", "b": "unlink", "c": "delete"})

	def test_apply_preserves_manual_order_removes_one_occurrence_and_appends(self):
		paths, manifest, music = self.fixture(("a", "b"))
		b_item = manifest["playlists"]["saved"]["items"][1]
		b = manifest["recordings"][b_item["recording_id"]]
		managed_path = paths.root / "tracks/b.mp3"
		managed_path.parent.mkdir(parents=True)
		managed_path.write_bytes(b"mp3")
		b["managed_file"] = {"relative_path": "tracks/b.mp3", "tool_owned": True, "metadata_profile": "playlist", "audio_sha256": "audio-b"}
		b["active_reference"] = {"kind": "managed_file", "relative_path": "tracks/b.mp3"}
		b["music"]["source"] = "managed_import"
		music.append({"title": "c", "artist": "Artist", "album": "Album", "duration_s": 180, "persistent_id": "PID-C", "database_id": "3", "location": "/Music/c.m4a", "comment": ""})
		preview = self.preview(manifest, music, ("a", "c"))
		save_pending_update(preview, paths)
		membership_calls = []
		def editor(_name, _pid, expected, removals, additions):
			membership_calls.append((expected, removals, additions))
			return [value for index, value in enumerate(expected) if index not in set(removals)] + additions
		deleted = []
		result = apply_playlist_update(
			preview.manifest,
			"saved",
			paths,
			music,
			exact_lookup=lambda pid: next((row for row in music if row["persistent_id"] == pid), None),
			cache_updater=lambda _track: None,
			cache_remover=lambda ids: len(ids),
			membership_reader=lambda _name, _pid: ["MANUAL-1", "PID-A", "PID-B", "MANUAL-2"],
			membership_editor=editor,
			music_deleter=lambda pid, recording_id: deleted.append((pid, recording_id)) or True,
			status_checker=lambda _name, pid: ("OWNED", pid),
			audio_hasher=lambda _path: "audio-b",
			owned_lookup=lambda recording_id: {"persistent_id": "PID-B"} if recording_id == b_item["recording_id"] else None,
		)
		self.assertEqual(membership_calls[0], (["MANUAL-1", "PID-A", "PID-B", "MANUAL-2"], [2], ["PID-C"]))
		self.assertEqual(result["additions"], 1)
		self.assertEqual(result["removals"], 1)
		self.assertEqual(len(deleted), 1)
		self.assertFalse(managed_path.exists())
		self.assertNotIn(b_item["recording_id"], preview.manifest["recordings"])
		self.assertEqual([item["spotify_id"] for item in preview.manifest["playlists"]["saved"]["items"]], ["a", "c"])

	def test_apply_inserts_addition_before_next_imported_track_without_losing_manual_entries(self):
		paths, manifest, music = self.fixture(("a", "b"))
		music.append({"title": "c", "artist": "Artist", "album": "Album", "duration_s": 180, "persistent_id": "PID-C", "database_id": "3", "location": "/Music/c.m4a", "comment": ""})
		preview = self.preview(manifest, music, ("a", "c", "b"))
		save_pending_update(preview, paths)
		membership_calls = []
		def editor(_name, _pid, expected, removals, additions):
			membership_calls.append((expected, removals, additions))
			return [value for index, value in enumerate(expected) if index not in set(removals)] + additions
		result = apply_playlist_update(
			preview.manifest,
			"saved",
			paths,
			music,
			exact_lookup=lambda pid: next((row for row in music if row["persistent_id"] == pid), None),
			cache_updater=lambda _track: None,
			cache_remover=lambda _ids: 0,
			membership_reader=lambda _name, _pid: ["MANUAL-1", "PID-A", "MANUAL-2", "PID-B", "MANUAL-3"],
			membership_editor=editor,
			music_deleter=lambda *_args: False,
			status_checker=lambda _name, pid: ("OWNED", pid),
			owned_lookup=lambda _recording_id: None,
		)
		self.assertEqual(membership_calls, [(
			["MANUAL-1", "PID-A", "MANUAL-2", "PID-B", "MANUAL-3"],
			[3, 4],
			["PID-C", "PID-B", "MANUAL-3"],
		)])
		self.assertEqual(result["additions"], 1)
		playlist = preview.manifest["playlists"]["saved"]
		self.assertEqual([item["spotify_id"] for item in playlist["items"]], ["a", "c", "b"])
		self.assertEqual([item["position"] for item in playlist["items"]], [1, 2, 3])
		self.assertEqual([item["saved_position"] for item in playlist["spotify_occurrence_snapshot"]], [1, 2, 3])

	def test_retry_accepts_already_applied_suffix_and_refuses_drift(self):
		paths, manifest, music = self.fixture(("a",))
		c_key, c = upsert_recording(manifest, track("c"))
		c["music"] = {"source": "existing_library", "persistent_id": "PID-C", "database_id": "3", "location": "/Music/c.m4a"}
		c["active_reference"] = {"kind": "existing_music", "persistent_id": "PID-C"}
		music.append({"title": "c", "artist": "Artist", "album": "Album", "duration_s": 180, "persistent_id": "PID-C", "database_id": "3", "location": "/Music/c.m4a", "comment": ""})
		manifest["playlists"]["saved"]["pending_update"] = {
			"spotify_snapshot": [{"spotify_id": "a", "recording_id": manifest["playlists"]["saved"]["items"][0]["recording_id"], "saved_position": 1, "spotify_position": 1}, {"spotify_id": "c", "recording_id": c_key, "saved_position": 2, "spotify_position": 2}],
			"addition_items": [{"position": 2, "spotify_position": 2, "recording_id": c_key, "spotify_id": "c", "status": "reused_music"}], "removals": [], "removal_positions": [],
			"music_checkpoint": {"baseline": ["PID-A"], "survivors": ["PID-A"], "append_ids": ["PID-C"], "remove_indexes": []},
		}
		edits = []
		result = apply_playlist_update(manifest, "saved", paths, music, exact_lookup=lambda pid: next((row for row in music if row["persistent_id"] == pid), None), cache_updater=lambda _track: None, cache_remover=lambda _ids: 0, membership_reader=lambda _name, _pid: ["PID-A", "PID-C"], membership_editor=lambda *args: edits.append(args) or [], music_deleter=lambda *_args: False, status_checker=lambda _name, pid: ("OWNED", pid), owned_lookup=lambda _recording_id: None)
		self.assertEqual(result["additions"], 1)
		self.assertEqual(edits, [])

		paths, manifest, music = self.fixture(("a",))
		manifest["playlists"]["saved"]["pending_update"] = {
			"spotify_snapshot": [], "addition_items": [], "removals": [], "removal_positions": [],
			"music_checkpoint": {"baseline": ["PID-A"], "survivors": ["PID-A"], "append_ids": ["PID-C"], "remove_indexes": []},
		}
		with self.assertRaisesRegex(MusicAutomationError, "changed unexpectedly"):
			apply_playlist_update(manifest, "saved", paths, music, exact_lookup=lambda _pid: None, cache_updater=lambda _track: None, cache_remover=lambda _ids: 0, membership_reader=lambda _name, _pid: ["DRIFT"], membership_editor=lambda *_args: [], music_deleter=lambda *_args: False, status_checker=lambda _name, pid: ("OWNED", pid), owned_lookup=lambda _recording_id: None)


if __name__ == "__main__":
	unittest.main()
