import unittest
from unittest.mock import patch

from crate_music_importer.ipod_import.youtube import YouTubeError, choose_candidate, inspect_manual_url, score_candidate, search_candidates


class YouTubeScoringTests(unittest.TestCase):
	def test_official_exact_recording_scores_confidently(self):
		track = {"title": "Song", "artists": "Artist", "duration_ms": 180000}
		candidate = {"title": "Artist - Song (Official Audio)", "uploader": "Artist - Topic", "duration_s": 180}
		score, _ = score_candidate(track, candidate)
		self.assertGreater(score, 0.87)

	def test_live_is_rejected_but_remaster_label_is_ignored(self):
		studio = {"title": "Song", "artists": "Artist", "duration_ms": 180000}
		live = {"title": "Artist - Song (Live)", "uploader": "Artist", "duration_s": 180}
		remaster = {"title": "Artist - Song (2011 Remaster)", "uploader": "Artist", "duration_s": 180}
		self.assertEqual(score_candidate(studio, live)[0], 0.0)
		self.assertGreater(score_candidate(studio, remaster)[0], 0.87)

	def test_score_alone_cannot_authorize_unverified_candidate(self):
		self.assertEqual(choose_candidate({}, [{"score": 0.99, "video_id": "one"}])["status"], "missing")

	def test_score_equal_to_or_below_point_87_does_not_pass(self):
		for score in (0.87, 0.8699):
			with self.subTest(score=score):
				self.assertEqual(choose_candidate({}, [{"score": score, "video_id": "one"}])["status"], "missing")

	def test_manual_source_must_be_a_youtube_url_before_any_lookup(self):
		with self.assertRaises(YouTubeError):
			inspect_manual_url("https://example.com/not-youtube")

	def test_search_skips_unusable_entries_instead_of_failing_the_candidate_list(self):
		track = {"title": "Song", "artists": "Artist", "duration_ms": 180000}
		with patch("crate_music_importer.ipod_import.youtube._run_json", return_value={
			"entries": [
				None,
				{"id": "usable", "title": "Artist - Song", "uploader": "Artist - Topic", "duration": 180},
			],
		}) as run_json:
			candidates = search_candidates(track)

		self.assertIn("--ignore-errors", run_json.call_args_list[0].args[0])
		self.assertEqual([candidate["video_id"] for candidate in candidates], ["usable"])


if __name__ == "__main__":
	unittest.main()
