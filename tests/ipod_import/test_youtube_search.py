import unittest
import threading
from unittest.mock import patch
from crate_music_importer.ipod_import.youtube import evaluate_candidate, search_candidates, choose_candidate, fallback_queries, YouTubeError

TRACK = {"title": "Beautiful Song", "artists": "Artist", "album": "Album", "duration_ms": 180000}
ENTRY = {"id": "good", "title": "Artist - Beautiful Song", "uploader": "Label", "duration": 180}

def candidate(**updates):
	result = {"video_id": "good", "title": ENTRY["title"], "uploader": "Label", "duration_s": 180, "metadata_verified": True}
	result.update(updates)
	return result

class MatchingTests(unittest.TestCase):
	def test_title_forms(self):
		for title in ["Beautiful Song - Artist", "Artist - Beautiful Song Official Audio", "Artist - Beautiful Song (Official Audio)", "Artist - Beautiful Song (Audio) | Album"]:
			with self.subTest(title=title): self.assertTrue(evaluate_candidate(TRACK, candidate(title=title))["automatic_eligible"])

	def test_collaboration(self):
		track = dict(TRACK, artists="Artist, Guest", title="Beautiful Song (feat. Guest)")
		for title in ["Artist & Guest - Beautiful Song", "Artist feat. Guest - Beautiful Song", "Beautiful Song (feat. Guest)"]:
			with self.subTest(title=title): self.assertTrue(evaluate_candidate(track, candidate(title=title, uploader="Artist"))["automatic_eligible"])

	def test_slash_aliases_are_primary_artist_evidence(self):
		track = dict(TRACK, title="Pop Star", artists="Yusuf / Cat Stevens", duration_ms=253000)
		result = evaluate_candidate(track, candidate(
			title="Pop Star (Remastered 2020)",
			track="Pop Star",
			artist="Yusuf / Cat Stevens",
			uploader="Yusuf / Cat Stevens",
			duration_s=253,
		))
		self.assertTrue(result["automatic_eligible"])
		self.assertEqual(result["matching_evidence"]["artist_evidence"], 1.0)
		self.assertEqual(result["reasons"], [])

	def test_exact_combined_uploader_is_strong_artist_evidence(self):
		track = dict(TRACK, artists="Yusuf / Cat Stevens")
		result = evaluate_candidate(track, candidate(title="Beautiful Song", uploader="Yusuf / Cat Stevens"))
		self.assertTrue(result["automatic_eligible"])
		self.assertEqual(result["matching_evidence"]["artist_evidence"], 0.90)

	def test_secondary_collaborator_alone_remains_review_only(self):
		track = dict(TRACK, artists="Artist, Guest")
		result = evaluate_candidate(track, candidate(title="Guest - Beautiful Song", uploader="Guest"))
		self.assertTrue(result["review_relevant"])
		self.assertFalse(result["automatic_eligible"])
		self.assertIn("Only part of the artist name matches", result["reasons"][0])

	def test_structured_label_upload(self):
		self.assertTrue(evaluate_candidate(TRACK, candidate(title="Beautiful Song", artist="Artist", track="Beautiful Song"))["automatic_eligible"])
		self.assertFalse(evaluate_candidate(TRACK, candidate(title="Beautiful Song"))["review_relevant"])

	def test_long_and_missing_duration_are_review_only(self):
		for duration in [0, 188, 300]:
			result = evaluate_candidate(TRACK, candidate(duration_s=duration))
			self.assertTrue(result["review_relevant"])
			self.assertFalse(result["automatic_eligible"])
		self.assertTrue(evaluate_candidate(TRACK, candidate(duration_s=187))["automatic_eligible"])

	def test_short_and_same_album_unrelated_excluded(self):
		for title in ["Artist - Beautiful Songs", "Artist - Other Song"]:
			self.assertFalse(evaluate_candidate(TRACK, candidate(title=title, album="Album"))["review_relevant"])

	def test_conflicting_versions_excluded(self):
		for label in ["Live", "Remix", "Cover", "Karaoke", "Sped Up", "Acoustic"]:
			self.assertFalse(evaluate_candidate(TRACK, candidate(title=f"Artist - Beautiful Song ({label})"))["review_relevant"])
		self.assertTrue(evaluate_candidate(dict(TRACK, title="Beautiful Song (Cover)"), candidate(title="Artist - Beautiful Song (Cover)"))["review_relevant"])

	def test_version_words_in_names(self):
		track = dict(TRACK, title="Live and Let Die", artists="Live")
		self.assertTrue(evaluate_candidate(track, candidate(title="Live - Live and Let Die"))["automatic_eligible"])
		self.assertFalse(evaluate_candidate(track, candidate(title="Live - Live and Let Die (Live)"))["review_relevant"])

	def test_requested_cover_cannot_be_replaced_with_studio(self):
		track = dict(TRACK, title="This Is A Beautiful Song (Cover)")
		self.assertFalse(evaluate_candidate(track, candidate(title="Artist - This Is A Beautiful Song"))["review_relevant"])

	def test_threshold_is_strict(self):
		with patch("crate_music_importer.ipod_import.youtube.YOUTUBE_CONFIDENCE_MIN", 0.99):
			self.assertFalse(evaluate_candidate(TRACK, candidate())["automatic_eligible"])

class SearchTests(unittest.TestCase):
	def test_easy_match_only_initial_and_full_metadata(self):
		with patch("crate_music_importer.ipod_import.youtube._run_json", side_effect=[{"entries": [ENTRY]}, ENTRY]) as run:
			result = search_candidates(TRACK)
		self.assertEqual(run.call_count, 2)
		self.assertEqual(choose_candidate(TRACK, result)["status"], "matched")
		self.assertEqual(result.summary["fallback_seconds"], 0)

	def test_alternate_recovery_dedup_and_partial_failure(self):
		def run(args, **kwargs):
			if args[0] == "--no-playlist": return ENTRY
			q = args[-1]
			if q.startswith("ytsearch8:"): return {"entries": []}
			if "official audio" in q: raise YouTubeError("fixture failure")
			return {"entries": [ENTRY, ENTRY, dict(ENTRY, id="bad", title="Artist - Wrong Song")]}
		with patch("crate_music_importer.ipod_import.youtube._run_json", side_effect=run): result = search_candidates(TRACK)
		self.assertEqual([c["video_id"] for c in result], ["good"])
		self.assertTrue(result[0]["automatic_eligible"])
		self.assertEqual(result.summary["status"], "partial")

	def test_missing_metadata_enriched(self):
		def run(args, **kwargs):
			if args[0] == "--no-playlist": return dict(ENTRY, artist="Artist")
			return {"entries": [dict(ENTRY, title="Beautiful Song", duration=None)]}
		with patch("crate_music_importer.ipod_import.youtube._run_json", side_effect=run): result = search_candidates(TRACK)
		self.assertTrue(result[0]["automatic_eligible"])

	def test_queries_clean_packaging_keep_versions_and_dedup(self):
		queries = fallback_queries(dict(TRACK, title="Beautiful Song (Live) - 2011 Remaster", album=""))
		self.assertEqual(len(queries), 2)
		self.assertTrue(all("Live" in q and "Remaster" not in q for q in queries))

	def test_more_from_empty_uses_quotes(self):
		with patch("crate_music_importer.ipod_import.youtube._run_json", return_value={"entries": []}) as run:
			result = search_candidates(TRACK, more=True)
		self.assertEqual(result.summary["status"], "no_results")
		self.assertEqual(run.call_count, 3)
		self.assertTrue(all('"Beautiful Song"' in c.args[0][-1] for c in run.call_args_list))

	def test_deadline_shared_with_metadata_preserves_partial_results(self):
		clock = [0.0]
		lock = threading.Lock()
		def run(args, timeout):
			with lock:
				self.assertLessEqual(timeout, 30)
				if args[0] == "--no-playlist":
					clock[0] = 30
					raise YouTubeError("metadata timeout")
				clock[0] += 5
			return {"entries": [dict(ENTRY, duration=None)]}
		with patch("crate_music_importer.ipod_import.youtube.time.monotonic", side_effect=lambda: clock[0]), patch("crate_music_importer.ipod_import.youtube._run_json", side_effect=run):
			result = search_candidates(TRACK, more=True)
		self.assertEqual(len(result), 1)
		self.assertFalse(result[0]["automatic_eligible"])
		self.assertEqual(result.summary["fallback_seconds"], 30)
		self.assertEqual(result.summary["status"], "partial")

class SearchBoundsTests(unittest.TestCase):
	def test_fallback_runs_at_most_two_requests_concurrently(self):
		lock = threading.Lock()
		pair = threading.Barrier(2)
		active = 0
		maximum = 0
		calls = 0
		def run(args, **kwargs):
			nonlocal active, maximum, calls
			with lock:
				active += 1
				maximum = max(maximum, active)
				calls += 1
				number = calls
			if number <= 2:
				pair.wait(timeout=2)
			with lock: active -= 1
			return {"entries": []}
		with patch("crate_music_importer.ipod_import.youtube._run_json", side_effect=run):
			result = search_candidates(TRACK, more=True)
		self.assertEqual(calls, 3)
		self.assertEqual(maximum, 2)
		self.assertEqual(result.summary["status"], "no_results")

	def test_no_more_than_five_metadata_inspections(self):
		inspected = []
		def run(args, **kwargs):
			if args[0] == "--no-playlist":
				video_id = args[-1].split("=")[-1]
				inspected.append(video_id)
				return dict(ENTRY, id=video_id)
			return {"entries": [dict(ENTRY, id=str(i), duration=None) for i in range(9)]}
		with patch("crate_music_importer.ipod_import.youtube._run_json", side_effect=run): result = search_candidates(TRACK, more=True)
		self.assertEqual(len(inspected), 5)
		self.assertEqual(len(set(inspected)), 5)
		self.assertEqual(len(result), 9)
		self.assertEqual(sum(c["automatic_eligible"] for c in result), 5)
