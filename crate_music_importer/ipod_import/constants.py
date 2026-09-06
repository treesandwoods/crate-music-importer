"""Configurable locations and recording format policy."""

from pathlib import Path


from crate_music_importer.ipod_import.config import configured_path

MANAGED_ROOT = configured_path("managedMusicDirectory", "CRATE_MANAGED_ROOT", Path.home() / "Music/mp3 Music")
MANIFEST_VERSION = 1
IMPORT_ALBUM = "Playlist Imports"
IMPORT_ALBUM_ARTIST = "Various Artists"
IMPORT_GENRE = "Music"
YOUTUBE_CONFIDENCE_MIN = 0.87
MUSIC_CONFIDENCE_MIN = 0.92
MUSIC_AMBIGUITY_MARGIN = 0.035
