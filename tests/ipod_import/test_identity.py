import unittest

from crate_music_importer.ipod_import.identity import clean_release_labels, match_music_track, normalize_recording_title, recording_id, version_markers
from crate_music_importer.ipod_import.music import MusicIndex


class IdentityTests(unittest.TestCase):
	def test_leading_artist_article_reuses_album_copy_conservatively(self):
		track = {"title": "Come And Go Blues", "artists": "Allman Brothers Band", "duration_ms": 295786}
		candidate = {"title": "Come and Go Blues", "artist": "The Allman Brothers Band", "album": "Brothers and Sisters", "duration_s": 295.128, "persistent_id": "ALBUM"}
		self.assertEqual(MusicIndex([candidate]).match(track)["status"], "reused")
		self.assertEqual(MusicIndex([dict(candidate, artist="Allman Brothers Band")]).match(dict(track, artists="The Allman Brothers Band"))["status"], "reused")
		for changes in ({"duration_s": 0}, {"duration_s": 300}, {"title": "Come and Go Blues (Live)"}, {"artist": "Allman Brothers Tribute Band"}):
			with self.subTest(changes=changes):
				self.assertEqual(MusicIndex([dict(candidate, **changes)]).match(track)["status"], "missing")
		self.assertEqual(MusicIndex([candidate, dict(candidate, persistent_id="SECOND")]).match(track)["status"], "ambiguous")
		self.assertNotEqual(recording_id(track), recording_id(dict(track, artists="The Allman Brothers Band")))

	def test_release_cleanup_removes_packaging_labels_and_keeps_meaningful_versions(self):
		self.assertEqual(clean_release_labels("Album (Deluxe Edition)"), "Album")
		self.assertEqual(clean_release_labels("Album - 40th Anniversary Edition"), "Album")
		self.assertEqual(clean_release_labels("Album [Collector’s Edition]"), "Album")
		self.assertEqual(clean_release_labels("Song - 2005 Remaster"), "Song")
		self.assertEqual(clean_release_labels("Song (Live - 2005 Remaster)"), "Song (Live)")
		self.assertEqual(clean_release_labels("Song (Mono)"), "Song")
		self.assertEqual(clean_release_labels("Song - Original Stereo Mix"), "Song")
		self.assertEqual(clean_release_labels("Song (Live in Stereo)"), "Song (Live)")
		self.assertEqual(clean_release_labels("Song (Acoustic Version)"), "Song (Acoustic Version)")
		self.assertEqual(clean_release_labels("Song - Club Remix"), "Song - Club Remix")

	def test_release_cleanup_never_turns_a_label_only_title_into_blank_text(self):
		self.assertEqual(clean_release_labels("Deluxe Edition"), "Deluxe Edition")

	def test_version_markers_distinguish_live_and_remix_but_ignore_remasters(self):
		self.assertEqual(version_markers("Song (Live)"), ("live",))
		self.assertEqual(version_markers("Song - Club Remix"), ("remix",))
		self.assertEqual(version_markers("Song (2011 Remaster)"), ())
		self.assertEqual(version_markers("Song (Mono)"), ())
		self.assertEqual(version_markers("Song (Stereo)"), ())
		self.assertEqual(normalize_recording_title("Song - Remastered 2011"), "song")

	def test_recording_id_is_stable_but_versions_are_distinct(self):
		base = {"title": "Song", "artists": "Artist", "duration_ms": 180000}
		live = {"title": "Song (Live)", "artists": "Artist", "duration_ms": 180000}
		self.assertEqual(recording_id(base), recording_id(dict(base)))
		self.assertNotEqual(recording_id(base), recording_id(live))
		self.assertEqual(recording_id(base), recording_id({"title": "Song - 2011 Remaster", "artists": "Artist", "duration_ms": 180000}))

	def test_confident_music_match_reuses_exact_recording(self):
		track = {"title": "Song", "artists": "Artist", "album": "Album", "duration_ms": 180000}
		candidate = {
			"title": "Song",
			"artist": "Artist",
			"album": "Album",
			"duration_s": 180.5,
			"persistent_id": "PID1",
		}
		result = match_music_track(track, [candidate])
		self.assertEqual(result["status"], "reused")
		self.assertEqual(result["candidate"]["persistent_id"], "PID1")

	def test_live_candidate_never_matches_studio_request(self):
		track = {"title": "Song", "artists": "Artist", "album": "Album", "duration_ms": 180000}
		candidate = {"title": "Song (Live)", "artist": "Artist", "album": "Live", "duration_s": 180, "persistent_id": "PID1"}
		self.assertEqual(match_music_track(track, [candidate])["status"], "missing")

	def test_remaster_label_does_not_prevent_music_library_reuse(self):
		track = {"title": "The Rain Song - Remaster", "artists": "Led Zeppelin", "album": "Houses of the Holy", "duration_ms": 461000}
		candidate = {
			"title": "The Rain Song",
			"artist": "Led Zeppelin",
			"album": "Houses of the Holy",
			"duration_s": 461,
			"persistent_id": "RAIN-SONG",
		}
		result = MusicIndex([candidate]).match(track)
		self.assertEqual(result["status"], "reused")
		self.assertEqual(result["candidate"]["persistent_id"], "RAIN-SONG")

	def test_duplicate_exact_music_tracks_require_review(self):
		track = {"title": "Song", "artists": "Artist", "album": "Album", "duration_ms": 180000}
		candidates = [
			{"title": "Song", "artist": "Artist", "album": "Album", "duration_s": 180, "persistent_id": "PID1"},
			{"title": "Song", "artist": "Artist", "album": "Album", "duration_s": 180, "persistent_id": "PID2"},
		]
		self.assertEqual(match_music_track(track, candidates)["status"], "ambiguous")


if __name__ == "__main__":
	unittest.main()
