import base64
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from crate_music_importer.ipod_import.spotify import SpotifyError, fetch_album, fetch_playlist, fetch_playlist_cover, load_fixture, parse_album_url, parse_playlist_url


FIXTURES = Path(__file__).parent / "fixtures"


class SpotifyFixtureTests(unittest.TestCase):
	def test_public_playlist_url_is_canonicalized(self):
		playlist_id, url = parse_playlist_url("https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1?si=abc")
		self.assertEqual(playlist_id, "37i9dQZF1DXTESTFIXTURE1")
		self.assertEqual(url, "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1")

	def test_non_playlist_url_is_refused(self):
		with self.assertRaises(SpotifyError):
			parse_playlist_url("https://open.spotify.com/album/37i9dQZF1DXTESTFIXTURE1")

	def test_public_album_url_is_canonicalized(self):
		album_id, url = parse_album_url("https://open.spotify.com/album/48a7rOjTzpD1zzJAteeveE?si=abc")
		self.assertEqual(album_id, "48a7rOjTzpD1zzJAteeveE")
		self.assertEqual(url, "https://open.spotify.com/album/48a7rOjTzpD1zzJAteeveE")

	def test_ordered_fixture_load(self):
		playlist = load_fixture(FIXTURES / "spotify_playlist.json")
		self.assertTrue(playlist.complete)
		self.assertEqual([track["position"] for track in playlist.tracks], [1, 2, 3, 4])

	def test_album_fixture_preserves_real_album_metadata(self):
		album = load_fixture(FIXTURES / "spotify_album.json")
		self.assertEqual(album.source_type, "album")
		self.assertEqual(album.album_artist, "Grimes")
		self.assertEqual(album.release_year, 2012)
		self.assertEqual([track["track_no"] for track in album.tracks], [1, 2])

	def test_mocked_public_pages_resolve_name_order_and_rich_metadata(self):
		playlist_id = "37i9dQZF1DXTESTFIXTURE1"
		state = {"entities": {"items": {f"spotify:playlist:{playlist_id}": {
			"id": playlist_id,
			"name": "Mock Public Playlist",
			"content": {"totalCount": 2, "items": [
				{"itemV2": {"data": {"name": "First", "uri": "spotify:track:firsttrackfixture01", "duration": {"totalMilliseconds": 101000}, "externalIds": {"isrc": "USFIX0000001"}, "artists": {"items": [{"profile": {"name": "Artist One"}}]}, "albumOfTrack": {"name": "Album One", "coverArt": {"sources": [{"url": "https://example.test/album-one.jpg", "width": 640, "height": 640}]}}}}},
				{"itemV2": {"data": {"name": "Second", "uri": "spotify:track:secondtrackfixture2", "duration": {"totalMilliseconds": 202000}, "artists": {"items": [{"profile": {"name": "Artist Two"}}]}, "albumOfTrack": {"name": "Album Two", "coverArt": {"sources": [{"url": "https://example.test/album-two.jpg", "width": 640, "height": 640}]}}}}},
			]},
		}}}}
		initial = f'<script id="initialState">{base64.b64encode(json.dumps(state).encode()).decode()}</script>'
		entity = {
			"id": playlist_id,
			"title": "Mock Public Playlist",
			"trackCount": 2,
			"trackList": [
				{"entityType": "track", "uri": "spotify:track:firsttrackfixture01", "title": "First", "subtitle": "Artist One", "duration": 101000},
				{"entityType": "track", "uri": "spotify:track:secondtrackfixture2", "title": "Second", "subtitle": "Artist Two", "duration": 202000},
			],
		}
		embed_data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
		embed = f'<script id="__NEXT_DATA__">{json.dumps(embed_data)}</script>'

		with patch("crate_music_importer.ipod_import.spotify._get", side_effect=[initial, embed]):
			playlist = fetch_playlist(f"https://open.spotify.com/playlist/{playlist_id}")
		self.assertEqual(playlist.name, "Mock Public Playlist")
		self.assertTrue(playlist.complete)
		self.assertEqual([track["title"] for track in playlist.tracks], ["First", "Second"])
		self.assertEqual(playlist.tracks[0]["album"], "Album One")
		self.assertEqual(playlist.tracks[0]["isrc"], "USFIX0000001")
		self.assertEqual([track["cover_url"] for track in playlist.tracks], [
			"https://example.test/album-one.jpg",
			"https://example.test/album-two.jpg",
		])

	def test_embed_playlist_cover_is_never_used_as_track_artwork(self):
		playlist_id = "37i9dQZF1DXTESTFIXTURE1"
		entity = {
			"id": playlist_id,
			"title": "Mock Public Playlist",
			"trackCount": 1,
			"coverArt": {"sources": [{"url": "https://example.test/playlist-cover.jpg"}]},
			"trackList": [{
				"entityType": "track",
				"uri": "spotify:track:firsttrackfixture01",
				"title": "First",
				"subtitle": "Artist One",
				"duration": 101000,
			}],
		}
		embed_data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
		embed = f'<script id="__NEXT_DATA__">{json.dumps(embed_data)}</script>'
		oembed = json.dumps({"thumbnail_url": "https://example.test/album-one.jpg"})
		with patch("crate_music_importer.ipod_import.spotify._get", side_effect=[
			SpotifyError("initial page unavailable"),
			embed,
			oembed,
		]):
			playlist = fetch_playlist(f"https://open.spotify.com/playlist/{playlist_id}")
		self.assertEqual(playlist.cover_url, "https://example.test/playlist-cover.jpg")
		self.assertEqual(playlist.tracks[0]["cover_url"], "https://example.test/album-one.jpg")
		self.assertNotEqual(playlist.tracks[0]["cover_url"], "https://example.test/playlist-cover.jpg")

	def test_playlist_cover_uses_lightweight_oembed_metadata(self):
		playlist_id = "37i9dQZF1DXTESTFIXTURE1"
		with patch(
			"crate_music_importer.ipod_import.spotify._get",
			return_value=json.dumps({"thumbnail_url": "https://example.test/playlist-cover.jpg"}),
		) as get:
			cover_url = fetch_playlist_cover(f"https://open.spotify.com/playlist/{playlist_id}")
		self.assertEqual(cover_url, "https://example.test/playlist-cover.jpg")
		self.assertEqual(get.call_args.args[0], f"https://open.spotify.com/oembed?url=https://open.spotify.com/playlist/{playlist_id}")

	def test_saved_track_artwork_survives_a_transient_oembed_omission(self):
		playlist_id = "37i9dQZF1DXTESTFIXTURE1"
		track_id = "firsttrackfixture01"
		entity = {
			"id": playlist_id,
			"title": "Mock Public Playlist",
			"trackCount": 1,
			"trackList": [{
				"entityType": "track",
				"uri": f"spotify:track:{track_id}",
				"title": "First",
				"subtitle": "Artist One",
				"duration": 101000,
			}],
		}
		embed_data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
		embed = f'<script id="__NEXT_DATA__">{json.dumps(embed_data)}</script>'

		with patch("crate_music_importer.ipod_import.spotify._get", side_effect=[
			SpotifyError("initial page unavailable"),
			embed,
			json.dumps({}),
		]):
			playlist = fetch_playlist(
				f"https://open.spotify.com/playlist/{playlist_id}",
				known_track_covers={track_id: "https://example.test/saved-album-cover.jpg"},
			)
		self.assertEqual(playlist.tracks[0]["cover_url"], "https://example.test/saved-album-cover.jpg")

	def test_mocked_public_album_resolves_track_and_disc_metadata(self):
		album_id = "48a7rOjTzpD1zzJAteeveE"
		state = {"entities": {"items": {f"spotify:album:{album_id}": {
			"id": album_id,
			"name": "Mock Album",
			"artists": {"items": [{"profile": {"name": "Album Artist"}}]},
			"date": {"isoString": "2014-01-02"},
			"coverArt": {"sources": [{"url": "https://example.test/cover.jpg", "width": 640, "height": 640}]},
			"tracksV2": {"totalCount": 1, "items": [
				{"itemV2": {"data": {
					"name": "Album Song",
					"uri": "spotify:track:albumtrackfixture1",
					"duration": {"totalMilliseconds": 201000},
					"externalIds": {"isrc": "USFIX0000010"},
					"trackNumber": 7,
					"discNumber": 2,
					"artists": {"items": [{"profile": {"name": "Track Artist"}}]},
				}}},
			]},
		}}}}
		initial = f'<script id="initialState">{base64.b64encode(json.dumps(state).encode()).decode()}</script>'
		with patch("crate_music_importer.ipod_import.spotify._get", side_effect=[initial, SpotifyError("embed unavailable")]):
			album = fetch_album(f"https://open.spotify.com/album/{album_id}")
		self.assertTrue(album.complete)
		self.assertEqual(album.name, "Mock Album")
		self.assertEqual(album.album_artist, "Album Artist")
		self.assertEqual(album.release_year, 2014)
		self.assertEqual(album.tracks[0]["album"], "Mock Album")
		self.assertEqual(album.tracks[0]["track_no"], 7)
		self.assertEqual(album.tracks[0]["disc_no"], 2)

	def test_incomplete_public_embed_is_retried_before_blocking(self):
		playlist_id = "37i9dQZF1DXTESTFIXTURE1"
		def initial(total):
			state = {"entities": {"items": {f"spotify:playlist:{playlist_id}": {
				"id": playlist_id, "name": "Retry Playlist", "content": {"totalCount": total, "items": []},
			}}}}
			return f'<script id="initialState">{base64.b64encode(json.dumps(state).encode()).decode()}</script>'
		def embed(titles):
			entity = {
				"id": playlist_id,
				"title": "Retry Playlist",
				"trackCount": 2,
				"trackList": [
					{"entityType": "track", "uri": f"spotify:track:retrytrackfixture{i}", "title": title, "subtitle": "Artist", "duration": 100000}
					for i, title in enumerate(titles, start=1)
				],
			}
			return f'<script id="__NEXT_DATA__">{json.dumps({"props": {"pageProps": {"state": {"data": {"entity": entity}}}}})}</script>'

		with patch("crate_music_importer.ipod_import.spotify._get", side_effect=[
			initial(2), embed(["First"]),
			initial(2), embed(["First", "Second"]),
			json.dumps({"thumbnail_url": "https://example.test/first.jpg"}),
			json.dumps({"thumbnail_url": "https://example.test/second.jpg"}),
		]) as get:
			playlist = fetch_playlist(f"https://open.spotify.com/playlist/{playlist_id}")
		self.assertTrue(playlist.complete)
		self.assertEqual([track["title"] for track in playlist.tracks], ["First", "Second"])
		self.assertEqual(get.call_count, 6)


if __name__ == "__main__":
	unittest.main()
