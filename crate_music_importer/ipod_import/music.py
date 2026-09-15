"""Read-only Music.app scanning and explicit, isolated playlist writes."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from crate_music_importer.ipod_import.identity import match_music_track, normalize_recording_title


class MusicAutomationError(RuntimeError):
	pass


_SCAN_SCRIPT = r'''
on replace_text(theText, findText, replacementText)
	set oldDelimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to findText
	set theItems to every text item of (theText as text)
	set AppleScript's text item delimiters to replacementText
	set theText to theItems as text
	set AppleScript's text item delimiters to oldDelimiters
	return theText
end replace_text

on clean_field(value)
	set textValue to value as text
	set textValue to my replace_text(textValue, character id 30, " ")
	set textValue to my replace_text(textValue, character id 31, " ")
	set textValue to my replace_text(textValue, return, " ")
	set textValue to my replace_text(textValue, linefeed, " ")
	return textValue
end clean_field

set rowSeparator to character id 30
set fieldSeparator to character id 31
set output to ""
with timeout of 600 seconds
	tell application "Music"
		set trackNames to name of every track of library playlist 1
		set trackArtists to artist of every track of library playlist 1
		set trackAlbums to album of every track of library playlist 1
		set trackDurations to duration of every track of library playlist 1
		set trackPersistentIDs to persistent ID of every track of library playlist 1
		set trackDatabaseIDs to database ID of every track of library playlist 1
		set trackLocations to location of every track of library playlist 1
		set trackComments to comment of every track of library playlist 1
		set trackAlbumArtists to album artist of every track of library playlist 1
		set trackNumbers to track number of every track of library playlist 1
		set trackCounts to track count of every track of library playlist 1
		set discNumbers to disc number of every track of library playlist 1
		set discCounts to disc count of every track of library playlist 1
		set compilationValues to compilation of every track of library playlist 1
	end tell
	repeat with trackIndex from 1 to count of trackNames
		set trackName to my clean_field(item trackIndex of trackNames)
		set trackArtist to my clean_field(item trackIndex of trackArtists)
		set trackAlbum to my clean_field(item trackIndex of trackAlbums)
		set trackDuration to item trackIndex of trackDurations as real
		set trackPersistentID to my clean_field(item trackIndex of trackPersistentIDs)
		set trackDatabaseID to my clean_field(item trackIndex of trackDatabaseIDs)
		set trackLocation to my clean_field(item trackIndex of trackLocations as text)
		set trackComment to my clean_field(item trackIndex of trackComments)
		set trackAlbumArtist to my clean_field(item trackIndex of trackAlbumArtists)
		set trackNumber to item trackIndex of trackNumbers as integer
		set trackCount to item trackIndex of trackCounts as integer
		set discNumber to item trackIndex of discNumbers as integer
		set discCount to item trackIndex of discCounts as integer
		set compilationValue to item trackIndex of compilationValues as boolean
		set output to output & trackName & fieldSeparator & trackArtist & fieldSeparator & trackAlbum & fieldSeparator & trackDuration & fieldSeparator & trackPersistentID & fieldSeparator & trackDatabaseID & fieldSeparator & trackLocation & fieldSeparator & trackComment & fieldSeparator & trackAlbumArtist & fieldSeparator & trackNumber & fieldSeparator & trackCount & fieldSeparator & discNumber & fieldSeparator & discCount & fieldSeparator & compilationValue & rowSeparator
	end repeat
end timeout
return output
'''


_SCAN_PLAYLIST_IMPORTS_SCRIPT = r'''
on replace_text(theText, findText, replacementText)
	set oldDelimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to findText
	set theItems to every text item of (theText as text)
	set AppleScript's text item delimiters to replacementText
	set theText to theItems as text
	set AppleScript's text item delimiters to oldDelimiters
	return theText
end replace_text

on clean_field(value)
	set textValue to value as text
	set textValue to my replace_text(textValue, character id 30, " ")
	set textValue to my replace_text(textValue, character id 31, " ")
	set textValue to my replace_text(textValue, return, " ")
	set textValue to my replace_text(textValue, linefeed, " ")
	return textValue
end clean_field

on run argv
	with timeout of 300 seconds
		tell application "Music"
			set selectedTracks to every track of library playlist 1 whose album is "Playlist Imports"
			repeat with requestedID in argv
				set selectedTracks to selectedTracks & (every track of library playlist 1 whose persistent ID is (requestedID as text))
			end repeat
			set rowSeparator to character id 30
			set fieldSeparator to character id 31
			set output to ""
			repeat with libraryTrack in selectedTracks
				try
					set trackName to my clean_field(name of libraryTrack)
					set trackArtist to my clean_field(artist of libraryTrack)
					set trackAlbum to my clean_field(album of libraryTrack)
					set trackDuration to duration of libraryTrack as real
					set trackPersistentID to my clean_field(persistent ID of libraryTrack)
					set trackDatabaseID to my clean_field(database ID of libraryTrack)
					try
						set trackLocation to my clean_field(POSIX path of (location of libraryTrack))
					on error
						set trackLocation to ""
					end try
					try
						set trackComment to my clean_field(comment of libraryTrack)
					on error
						set trackComment to ""
					end try
					try
						set trackAlbumArtist to my clean_field(album artist of libraryTrack)
					on error
						set trackAlbumArtist to ""
					end try
					try
						set trackNumber to track number of libraryTrack as integer
					on error
						set trackNumber to 0
					end try
					try
						set trackCount to track count of libraryTrack as integer
					on error
						set trackCount to 0
					end try
					try
						set discNumber to disc number of libraryTrack as integer
					on error
						set discNumber to 0
					end try
					try
						set discCount to disc count of libraryTrack as integer
					on error
						set discCount to 0
					end try
					try
						set compilationValue to compilation of libraryTrack as boolean
					on error
						set compilationValue to false
					end try
					set output to output & trackName & fieldSeparator & trackArtist & fieldSeparator & trackAlbum & fieldSeparator & trackDuration & fieldSeparator & trackPersistentID & fieldSeparator & trackDatabaseID & fieldSeparator & trackLocation & fieldSeparator & trackComment & fieldSeparator & trackAlbumArtist & fieldSeparator & trackNumber & fieldSeparator & trackCount & fieldSeparator & discNumber & fieldSeparator & discCount & fieldSeparator & compilationValue & rowSeparator
				end try
			end repeat
			return output
		end tell
	end timeout
end run
'''


_LOOKUP_TRACK_BY_ID_SCRIPT = r'''
on replace_text(theText, findText, replacementText)
	set oldDelimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to findText
	set theItems to every text item of (theText as text)
	set AppleScript's text item delimiters to replacementText
	set theText to theItems as text
	set AppleScript's text item delimiters to oldDelimiters
	return theText
end replace_text

on clean_field(value)
	set textValue to value as text
	set textValue to my replace_text(textValue, character id 30, " ")
	set textValue to my replace_text(textValue, character id 31, " ")
	set textValue to my replace_text(textValue, return, " ")
	set textValue to my replace_text(textValue, linefeed, " ")
	return textValue
end clean_field

on run argv
	set requestedID to item 1 of argv as text
	set fieldSeparator to character id 31
	set rowSeparator to character id 30
	with timeout of 60 seconds
		tell application "Music"
			set matches to every track of library playlist 1 whose persistent ID is requestedID
			if (count of matches) is 0 then return ""
			if (count of matches) is greater than 1 then error "Music returned duplicate tracks for persistent ID " & requestedID
			set libraryTrack to item 1 of matches
			set trackName to my clean_field(name of libraryTrack)
			set trackArtist to my clean_field(artist of libraryTrack)
			set trackAlbum to my clean_field(album of libraryTrack)
			set trackDuration to duration of libraryTrack as real
			set trackPersistentID to my clean_field(persistent ID of libraryTrack)
			set trackDatabaseID to my clean_field(database ID of libraryTrack)
			try
				set trackLocation to my clean_field(POSIX path of (location of libraryTrack))
			on error
				set trackLocation to ""
			end try
			try
				set trackComment to my clean_field(comment of libraryTrack)
			on error
				set trackComment to ""
			end try
			try
				set trackAlbumArtist to my clean_field(album artist of libraryTrack)
			on error
				set trackAlbumArtist to ""
			end try
			try
				set trackNumber to track number of libraryTrack as integer
			on error
				set trackNumber to 0
			end try
			try
				set trackCount to track count of libraryTrack as integer
			on error
				set trackCount to 0
			end try
			try
				set discNumber to disc number of libraryTrack as integer
			on error
				set discNumber to 0
			end try
			try
				set discCount to disc count of libraryTrack as integer
			on error
				set discCount to 0
			end try
			try
				set compilationValue to compilation of libraryTrack as boolean
			on error
				set compilationValue to false
			end try
			return trackName & fieldSeparator & trackArtist & fieldSeparator & trackAlbum & fieldSeparator & trackDuration & fieldSeparator & trackPersistentID & fieldSeparator & trackDatabaseID & fieldSeparator & trackLocation & fieldSeparator & trackComment & fieldSeparator & trackAlbumArtist & fieldSeparator & trackNumber & fieldSeparator & trackCount & fieldSeparator & discNumber & fieldSeparator & discCount & fieldSeparator & compilationValue & rowSeparator
		end tell
	end timeout
end run
'''


_LOOKUP_TRACK_BY_RECORDING_ID_SCRIPT = r'''
on replace_text(theText, findText, replacementText)
	set oldDelimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to findText
	set theItems to every text item of (theText as text)
	set AppleScript's text item delimiters to replacementText
	set theText to theItems as text
	set AppleScript's text item delimiters to oldDelimiters
	return theText
end replace_text

on clean_field(value)
	set textValue to value as text
	set textValue to my replace_text(textValue, character id 30, " ")
	set textValue to my replace_text(textValue, character id 31, " ")
	set textValue to my replace_text(textValue, return, " ")
	set textValue to my replace_text(textValue, linefeed, " ")
	return textValue
end clean_field

on run argv
	set ownershipMarker to "recording_id=" & (item 1 of argv as text)
	set fieldSeparator to character id 31
	set rowSeparator to character id 30
	with timeout of 60 seconds
		tell application "Music"
			set matches to every track of library playlist 1 whose comment contains ownershipMarker
			if (count of matches) is 0 then return ""
			if (count of matches) is greater than 1 then error "Music returned duplicate importer-owned tracks for " & ownershipMarker
			set libraryTrack to item 1 of matches
			set trackName to my clean_field(name of libraryTrack)
			set trackArtist to my clean_field(artist of libraryTrack)
			set trackAlbum to my clean_field(album of libraryTrack)
			set trackDuration to duration of libraryTrack as real
			set trackPersistentID to my clean_field(persistent ID of libraryTrack)
			set trackDatabaseID to my clean_field(database ID of libraryTrack)
			try
				set trackLocation to my clean_field(POSIX path of (location of libraryTrack))
			on error
				set trackLocation to ""
			end try
			set trackComment to my clean_field(comment of libraryTrack)
			return trackName & fieldSeparator & trackArtist & fieldSeparator & trackAlbum & fieldSeparator & trackDuration & fieldSeparator & trackPersistentID & fieldSeparator & trackDatabaseID & fieldSeparator & trackLocation & fieldSeparator & trackComment & rowSeparator
		end tell
	end timeout
end run
'''


_DELETE_IMPORTER_OWNED_TRACK_SCRIPT = r'''
on run argv
	set requestedID to item 1 of argv as text
	set ownershipMarker to "recording_id=" & (item 2 of argv as text)
	with timeout of 60 seconds
		tell application "Music"
			set matches to every track of library playlist 1 whose persistent ID is requestedID
			if (count of matches) is 0 then return "MISSING"
			if (count of matches) is greater than 1 then error "Music returned duplicate tracks for persistent ID " & requestedID
			set targetTrack to item 1 of matches
			set targetComment to ""
			try
				set targetComment to comment of targetTrack as text
			end try
			if targetComment does not contain ownershipMarker then error "Refusing to delete a Music track without the importer ownership marker: " & requestedID
			delete targetTrack
			return "DELETED"
		end tell
	end timeout
end run
'''


_ADD_FILE_SCRIPT = r'''
on run argv
	set filePath to item 1 of argv
	set ownershipComment to ""
	if (count of argv) is greater than 1 then set ownershipComment to item 2 of argv as text
	set fileAlias to POSIX file filePath as alias
	tell application "Music"
		set addedValue to add fileAlias
		if class of addedValue is list then
			if (count of addedValue) is 0 then error "Music did not return an imported track."
			set importedTrack to item 1 of addedValue
		else
			set importedTrack to addedValue
		end if
		set importedPID to persistent ID of importedTrack as text
		set importedDatabaseID to database ID of importedTrack as text
		if ownershipComment is not "" then
			try
				set comment of importedTrack to ownershipComment
			end try
		end if
		try
			set importedLocation to POSIX path of (location of importedTrack)
		on error
			set importedLocation to ""
		end try
		return importedPID & tab & importedDatabaseID & tab & importedLocation
	end tell
end run
'''


_SET_OWNERSHIP_MARKER_SCRIPT = r'''
on run argv
	set requestedID to item 1 of argv as text
	set ownershipComment to item 2 of argv as text
	tell application "Music"
		set matches to every track of library playlist 1 whose persistent ID is requestedID
		if (count of matches) is 0 then error "The managed Music track is missing: " & requestedID
		if (count of matches) is greater than 1 then error "Music returned duplicate tracks for persistent ID " & requestedID
		set comment of item 1 of matches to ownershipComment
		return "OK"
	end tell
end run
'''


_UPDATE_ALBUM_TRACK_SCRIPT = r'''
on run argv
	set requestedID to item 1 of argv
	set trackName to item 2 of argv
	set trackArtist to item 3 of argv
	set albumName to item 4 of argv
	set albumArtist to item 5 of argv
	set trackNumberValue to item 6 of argv as integer
	set trackCountValue to item 7 of argv as integer
	set discNumberValue to item 8 of argv as integer
	set discCountValue to item 9 of argv as integer
	set yearValue to item 10 of argv as integer
	set compilationValue to item 11 of argv is "true"
	set commentValue to item 12 of argv
	set artworkPath to item 13 of argv
	tell application "Music"
		set matches to every track of library playlist 1 whose persistent ID is requestedID
		if (count of matches) is 0 then error "The managed Music track is missing: " & requestedID
		set targetTrack to item 1 of matches
		set name of targetTrack to trackName
		set artist of targetTrack to trackArtist
		set album of targetTrack to albumName
		set album artist of targetTrack to albumArtist
		set genre of targetTrack to "Music"
		set track number of targetTrack to trackNumberValue
		set track count of targetTrack to trackCountValue
		set disc number of targetTrack to discNumberValue
		set disc count of targetTrack to discCountValue
		if yearValue is greater than 0 then set year of targetTrack to yearValue
		set compilation of targetTrack to compilationValue
		set comment of targetTrack to commentValue
		if artworkPath is not "" then
			set artworkFile to POSIX file artworkPath as alias
			set artworkData to read artworkFile as picture
			try
				set data of artwork 1 of targetTrack to artworkData
			on error
				make new artwork at end of artworks of targetTrack with properties {data:artworkData}
			end try
		end if
		return "OK"
	end tell
end run
'''


_UPDATE_MANAGED_ARTWORK_SCRIPT = r'''
on run argv
	set requestedID to item 1 of argv
	set recordingToken to item 2 of argv
	set artworkPath to item 3 of argv
	tell application "Music"
		set matches to every track of library playlist 1 whose persistent ID is requestedID
		if (count of matches) is 0 then error "The managed Music track is missing: " & requestedID
		set targetTrack to item 1 of matches
		set trackComment to comment of targetTrack as text
		if trackComment does not contain recordingToken then error "The Music track is not importer-owned."
		set artworkFile to POSIX file artworkPath as alias
		set artworkData to read artworkFile as picture
		try
			set data of artwork 1 of targetTrack to artworkData
		on error
			make new artwork at end of artworks of targetTrack with properties {data:artworkData}
		end try
		return "OK"
	end tell
end run
'''


_PLAYLIST_STATUS_SCRIPT = r'''
on run argv
	set requestedName to item 1 of argv
	set knownPID to item 2 of argv
	tell application "Music"
		if knownPID is not "" then
			set ownedPlaylist to missing value
			repeat with candidate in every user playlist
				try
					if (persistent ID of candidate as text) is knownPID then set ownedPlaylist to candidate
				end try
			end repeat
			if ownedPlaylist is missing value then return "MISSING" & tab & knownPID
			repeat with candidate in every user playlist
				try
					if (persistent ID of candidate as text) is not knownPID and (name of candidate as text) is requestedName then return "COLLISION" & tab & (persistent ID of candidate as text)
				end try
			end repeat
			return "OWNED" & tab & knownPID
		end if
		repeat with candidate in every user playlist
			try
				if (name of candidate as text) is requestedName then return "COLLISION" & tab & (persistent ID of candidate as text)
			end try
		end repeat
		return "AVAILABLE" & tab
	end tell
end run
'''


_SYNC_PLAYLIST_SCRIPT = r'''
on run argv
	set requestedName to item 1 of argv
	set knownPID to item 2 of argv
	set requestedIDs to items 3 thru -1 of argv
	tell application "Music"
		set targetPlaylist to missing value
		if knownPID is not "" then
			repeat with candidate in every user playlist
				try
					if (persistent ID of candidate as text) is knownPID then set targetPlaylist to candidate
				end try
			end repeat
			if targetPlaylist is missing value then error "The importer-owned Music playlist no longer exists. Refusing to replace a different playlist."
			repeat with candidate in every user playlist
				try
					if (persistent ID of candidate as text) is not knownPID and (name of candidate as text) is requestedName then error "A different Music playlist now uses the requested name."
				end try
			end repeat
		else
			repeat with candidate in every user playlist
				try
					if (name of candidate as text) is requestedName then error "A Music playlist with this name already exists and is not recorded as importer-owned."
				end try
			end repeat
		end if

		set sourceTracks to {}
		repeat with requestedID in requestedIDs
			set wantedID to requestedID as text
			set matches to every track of library playlist 1 whose persistent ID is wantedID
			if (count of matches) is 0 then error "A planned Music track is missing: " & requestedID
			set end of sourceTracks to item 1 of matches
		end repeat

		if targetPlaylist is missing value then
			set targetPlaylist to make new user playlist with properties {name:requestedName}
		else
			set name of targetPlaylist to requestedName
			delete every track of targetPlaylist
		end if
		repeat with sourceTrack in sourceTracks
			duplicate sourceTrack to targetPlaylist
		end repeat
		return "OK" & tab & (persistent ID of targetPlaylist as text)
	end tell
end run
'''


_PLAYLIST_MEMBERSHIP_SCRIPT = r'''
on run argv
	set requestedName to item 1 of argv
	set knownPID to item 2 of argv
	set separatorText to ASCII character 31
	tell application "Music"
		set targetPlaylist to missing value
		repeat with candidate in every user playlist
			try
				if (persistent ID of candidate as text) is knownPID then set targetPlaylist to candidate
			end try
		end repeat
		if targetPlaylist is missing value then error "The importer-owned Music playlist no longer exists."
		repeat with candidate in every user playlist
			try
				if (persistent ID of candidate as text) is not knownPID and (name of candidate as text) is requestedName then error "A different Music playlist now uses the requested name."
			end try
		end repeat
		set values to {}
		repeat with playlistTrack in every track of targetPlaylist
			set end of values to (persistent ID of playlistTrack as text)
		end repeat
		set AppleScript's text item delimiters to separatorText
		set encoded to values as text
		set AppleScript's text item delimiters to ""
		return "OK" & tab & encoded
	end tell
end run
'''


_EDIT_PLAYLIST_MEMBERSHIP_SCRIPT = r'''
on split_text(valueText, delimiterText)
	if valueText is "" then return {}
	set oldDelimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to delimiterText
	set values to text items of valueText
	set AppleScript's text item delimiters to oldDelimiters
	return values
end split_text

on run argv
	set requestedName to item 1 of argv
	set knownPID to item 2 of argv
	set expectedIDs to my split_text(item 3 of argv, ASCII character 31)
	set removalValues to my split_text(item 4 of argv, ",")
	set appendIDs to my split_text(item 5 of argv, ASCII character 31)
	tell application "Music"
		set targetPlaylist to missing value
		repeat with candidate in every user playlist
			try
				if (persistent ID of candidate as text) is knownPID then set targetPlaylist to candidate
			end try
		end repeat
		if targetPlaylist is missing value then error "The importer-owned Music playlist no longer exists."
		repeat with candidate in every user playlist
			try
				if (persistent ID of candidate as text) is not knownPID and (name of candidate as text) is requestedName then error "A different Music playlist now uses the requested name."
			end try
		end repeat
		set currentTracks to every track of targetPlaylist
		if (count of currentTracks) is not (count of expectedIDs) then error "Playlist membership changed before the guarded update."
		repeat with trackIndex from 1 to count of expectedIDs
			set currentID to persistent ID of (item trackIndex of currentTracks) as text
			if currentID is not (item trackIndex of expectedIDs as text) then error "Playlist membership changed before the guarded update."
		end repeat
		set sourceTracks to {}
		repeat with requestedID in appendIDs
			set wantedID to requestedID as text
			set matches to every track of library playlist 1 whose persistent ID is wantedID
			if (count of matches) is not 1 then error "A planned Music track is missing or colliding: " & wantedID
			set end of sourceTracks to item 1 of matches
		end repeat
		repeat with removalValue in removalValues
			if (removalValue as text) is not "" then
				set removalPosition to (removalValue as integer) + 1
				set currentTracks to every track of targetPlaylist
				delete (item removalPosition of currentTracks)
			end if
		end repeat
		repeat with sourceTrack in sourceTracks
			duplicate sourceTrack to targetPlaylist
		end repeat
		set finalIDs to {}
		repeat with playlistTrack in every track of targetPlaylist
			set end of finalIDs to (persistent ID of playlistTrack as text)
		end repeat
		set AppleScript's text item delimiters to ASCII character 31
		set encoded to finalIDs as text
		set AppleScript's text item delimiters to ""
		return "OK" & tab & encoded
	end tell
end run
'''


def _osascript(script: str, args: list[str] | None = None, *, timeout: int = 300) -> str:
	command = ["/usr/bin/osascript", "-e", script]
	command.extend(args or [])
	try:
		result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise MusicAutomationError(f"Could not run Music automation: {exc}") from exc
	if result.returncode != 0:
		detail = (result.stderr or result.stdout or "unknown AppleScript error").strip()
		if "-1743" in detail:
			message = (
				"Music automation was denied. Allow Raycast to control Music in "
				"System Settings > Privacy & Security > Automation. "
			)
		elif "-1712" in detail:
			message = "Music automation timed out while Music was busy or loading the Cloud Library. Restart Music and try again. "
		else:
			message = "Music automation failed (this is not an Automation-permission error). "
		raise MusicAutomationError(message + detail)
	return result.stdout.rstrip("\r\n")


def _parse_scan_output(output: str, *, deduplicate: bool = True, strict: bool = False) -> list[dict[str, Any]]:
	tracks: list[dict[str, Any]] = []
	seen_persistent_ids: set[str] = set()
	for row in output.split(chr(30)):
		if not row:
			continue
		fields = row.split(chr(31))
		if strict and len(fields) < 14:
			raise MusicAutomationError("Music returned an incomplete health scan row; no complete audit can be claimed.")
		if len(fields) < 8:
			continue
		try:
			duration = float(fields[3] or 0)
		except ValueError:
			duration = 0.0
		persistent_id = fields[4]
		if deduplicate and persistent_id and persistent_id in seen_persistent_ids:
			continue
		if persistent_id:
			seen_persistent_ids.add(persistent_id)
		tracks.append({
			"title": fields[0],
			"artist": fields[1],
			"album": fields[2],
			"duration_s": duration,
			"persistent_id": persistent_id,
			"database_id": fields[5],
			"location": _music_location_to_posix(fields[6]),
			"comment": fields[7],
			"album_artist": fields[8] if len(fields) > 8 else "",
			"track_no": _integer(fields[9]) if len(fields) > 9 else 0,
			"track_total": _integer(fields[10]) if len(fields) > 10 else 0,
			"disc_no": _integer(fields[11]) if len(fields) > 11 else 0,
			"disc_total": _integer(fields[12]) if len(fields) > 12 else 0,
			"compilation": fields[13].casefold() == "true" if len(fields) > 13 else False,
			"rating": _integer(fields[14]) if len(fields) > 14 else 0,
			"favorited": fields[15].casefold() == "true" if len(fields) > 15 else False,
			"played_count": _integer(fields[16]) if len(fields) > 16 else 0,
			"skipped_count": _integer(fields[17]) if len(fields) > 17 else 0,
		})
	return tracks


def _music_location_to_posix(value: str) -> str | None:
	"""Convert Music's HFS alias text without asking Music to perform a POSIX coercion."""
	text = str(value or "").strip()
	if not text:
		return None
	if text.startswith("/"):
		return text
	parts = text.split(":")
	if len(parts) < 2 or not parts[0]:
		return text
	# HFS uses ':' as a path separator and represents a literal POSIX ':' as '/'.
	volume, rest = parts[0], [part.replace("/", ":") for part in parts[1:] if part]
	if volume == "Macintosh HD":
		return "/" + "/".join(rest)
	return "/Volumes/" + "/".join([volume, *rest])


def scan_music_library() -> list[dict[str, Any]]:
	return _parse_scan_output(_osascript(_SCAN_SCRIPT, timeout=600))



def scan_music_library_for_health() -> list[dict[str, Any]]:
	"""Preserve duplicate IDs and cloud locations as audit evidence without changing normal scans."""
	script = _SCAN_SCRIPT.replace(
		"set trackLocation to my clean_field(item trackIndex of trackLocations as text)",
		"""if item trackIndex of trackLocations is missing value then
			set trackLocation to ""
		else
			set trackLocation to my clean_field(item trackIndex of trackLocations as text)
		end if""",
	)
	return _parse_scan_output(_osascript(script, timeout=600), deduplicate=False, strict=True)

def scan_playlist_imports(persistent_ids: list[str] | None = None) -> list[dict[str, Any]]:
	"""Read Playlist Imports plus exact saved IDs without enumerating the whole library."""
	requested = list(dict.fromkeys(str(value) for value in (persistent_ids or []) if str(value)))
	return _parse_scan_output(_osascript(_SCAN_PLAYLIST_IMPORTS_SCRIPT, requested, timeout=300))


def lookup_music_track(persistent_id: str) -> dict[str, Any] | None:
	"""Look up one exact Music persistent ID without enumerating the library."""
	if not persistent_id:
		raise MusicAutomationError("A Music persistent ID is required for exact validation.")
	tracks = _parse_scan_output(_osascript(_LOOKUP_TRACK_BY_ID_SCRIPT, [persistent_id], timeout=60))
	if not tracks:
		return None
	if len(tracks) != 1 or tracks[0].get("persistent_id") != persistent_id:
		raise MusicAutomationError(f"Music returned an unexpected exact-ID result for {persistent_id}.")
	return tracks[0]


def lookup_importer_owned_music_track(recording_id: str) -> dict[str, Any] | None:
	"""Find one importer-owned Music track by its durable recording marker."""
	if not recording_id:
		raise MusicAutomationError("A recording ID is required for importer-owned lookup.")
	tracks = _parse_scan_output(_osascript(_LOOKUP_TRACK_BY_RECORDING_ID_SCRIPT, [recording_id], timeout=60))
	if not tracks:
		return None
	if len(tracks) != 1 or f"recording_id={recording_id}" not in str(tracks[0].get("comment") or ""):
		raise MusicAutomationError(f"Music returned an unexpected importer-owned result for {recording_id}.")
	return tracks[0]


def delete_importer_owned_music_track(persistent_id: str, recording_id: str) -> bool:
	"""Delete one exact Music item only after Music verifies its importer marker."""
	if not persistent_id or not recording_id:
		raise MusicAutomationError("A persistent ID and recording ID are required for safe Music deletion.")
	result = _osascript(_DELETE_IMPORTER_OWNED_TRACK_SCRIPT, [persistent_id, recording_id], timeout=60)
	if result == "MISSING":
		return False
	if result != "DELETED":
		raise MusicAutomationError(f"Music returned an unexpected deletion result for {persistent_id}.")
	return True


def _integer(value: Any) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return 0


def load_music_fixture(path: Path) -> list[dict[str, Any]]:
	with path.open("r", encoding="utf-8") as handle:
		data = json.load(handle)
	if not isinstance(data, list):
		raise ValueError("Music fixture must contain a JSON list of tracks.")
	return [dict(item) for item in data]


class MusicIndex:
	def __init__(self, tracks: list[dict[str, Any]]):
		self.tracks = tracks
		self.by_title: dict[str, list[dict[str, Any]]] = {}
		self.by_persistent_id: dict[str, dict[str, Any]] = {}
		self.stale_by_persistent_id: dict[str, dict[str, Any]] = {}
		self.by_recording_comment: dict[str, dict[str, Any]] = {}
		for track in tracks:
			persistent_id = str(track.get("persistent_id") or "")
			if track.get("stale"):
				if persistent_id:
					self.stale_by_persistent_id[persistent_id] = track
				continue
			self.by_title.setdefault(normalize_recording_title(track.get("title")), []).append(track)
			if persistent_id:
				self.by_persistent_id[persistent_id] = track
			comment = str(track.get("comment") or "")
			if "recording_id=" in comment:
				key = comment.split("recording_id=", 1)[1].split()[0].strip(";,")
				self.by_recording_comment[key] = track

	def candidates(self, source_track: dict[str, Any]) -> list[dict[str, Any]]:
		title = normalize_recording_title(source_track.get("title"))
		exact = self.by_title.get(title, [])
		if exact:
			return exact
		wanted_tokens = set(title.split())
		if not wanted_tokens:
			return []
		pool: list[dict[str, Any]] = []
		for candidate_title, candidates in self.by_title.items():
			candidate_tokens = set(candidate_title.split())
			if len(wanted_tokens & candidate_tokens) / max(1, len(wanted_tokens)) >= 0.85:
				pool.extend(candidates)
				if len(pool) >= 100:
					break
		return pool[:100]

	def match(self, source_track: dict[str, Any]) -> dict[str, Any]:
		candidates = [candidate for candidate in self.candidates(source_track) if candidate.get("persistent_id")]
		return match_music_track(source_track, candidates)


def import_managed_file(path: Path, recording_id: str | None = None) -> dict[str, Any]:
	if not path.is_file():
		raise MusicAutomationError(f"Managed MP3 is missing: {path}")
	arguments = [str(path)]
	if recording_id:
		arguments.append(f"Managed by Crate Music Importer; recording_id={recording_id}")
	fields = _osascript(_ADD_FILE_SCRIPT, arguments).split("\t", 2)
	if not fields or not fields[0]:
		raise MusicAutomationError("Music imported the file but did not return a persistent ID.")
	return {
		"persistent_id": fields[0],
		"database_id": fields[1] if len(fields) > 1 else "",
		"location": fields[2] if len(fields) > 2 and fields[2] else None,
	}


def set_music_ownership_marker(persistent_id: str, recording_id: str) -> None:
	"""Persist the importer marker after Music has stabilized a new addition."""
	if not persistent_id or not recording_id:
		raise MusicAutomationError("A persistent ID and recording ID are required for the ownership marker.")
	comment = f"Managed by Crate Music Importer; recording_id={recording_id}"
	result = _osascript(_SET_OWNERSHIP_MARKER_SCRIPT, [persistent_id, comment], timeout=60)
	if result != "OK":
		raise MusicAutomationError(f"Music returned an unexpected ownership-marker result: {result}")


def verify_music_tracks(persistent_ids: list[str], *, settle_seconds: float = 10.0) -> dict[str, dict[str, Any]]:
	"""Verify recently added Music IDs after its Cloud Library has had time to react."""
	requested = list(dict.fromkeys(str(value) for value in persistent_ids if str(value)))
	if not requested:
		return {}
	if settle_seconds > 0:
		time.sleep(settle_seconds)
	requested_set = set(requested)
	return {
		str(track["persistent_id"]): track
		for track in scan_playlist_imports(requested)
		if str(track.get("persistent_id") or "") in requested_set
	}


def update_managed_music_track(
	persistent_id: str,
	recording: dict[str, Any],
	artwork_path: Path | None,
) -> None:
	album = recording.get("album_metadata") or {}
	meta = recording.get("source_metadata") or {}
	if not album:
		raise MusicAutomationError("Album metadata is missing; refusing to update the Music track.")
	comment = f"Managed by Crate Music Importer; recording_id={recording['recording_id']}"
	result = _osascript(_UPDATE_ALBUM_TRACK_SCRIPT, [
		persistent_id,
		str(meta.get("title") or ""),
		str(meta.get("artists") or ""),
		str(album.get("album") or ""),
		str(album.get("album_artist") or ""),
		str(int(album.get("track_no") or 0)),
		str(int(album.get("track_total") or 0)),
		str(int(album.get("disc_no") or 1)),
		str(int(album.get("disc_total") or 1)),
		str(int(album.get("release_year") or 0)),
		"true" if album.get("is_compilation") else "false",
		comment,
		str(artwork_path or ""),
	])
	if result != "OK":
		raise MusicAutomationError(f"Music returned an unexpected album metadata result: {result}")


def update_managed_music_artwork(
	persistent_id: str,
	recording_id: str,
	artwork_path: Path,
) -> None:
	result = _osascript(_UPDATE_MANAGED_ARTWORK_SCRIPT, [
		persistent_id,
		f"recording_id={recording_id}",
		str(artwork_path),
	])
	if result != "OK":
		raise MusicAutomationError(f"Music returned an unexpected artwork update result: {result}")


def playlist_status(name: str, known_persistent_id: str | None) -> tuple[str, str | None]:
	fields = _osascript(_PLAYLIST_STATUS_SCRIPT, [name, known_persistent_id or ""]).split("\t", 1)
	return fields[0], fields[1] if len(fields) > 1 and fields[1] else None


def sync_music_playlist(name: str, known_persistent_id: str | None, track_persistent_ids: list[str]) -> str:
	if not track_persistent_ids:
		raise MusicAutomationError("Refusing to create an empty Music playlist.")
	output = _osascript(_SYNC_PLAYLIST_SCRIPT, [name, known_persistent_id or "", *track_persistent_ids], timeout=600)
	fields = output.split("\t", 1)
	if fields[0] != "OK" or len(fields) < 2 or not fields[1]:
		raise MusicAutomationError(f"Music returned an unexpected playlist result: {output}")
	return fields[1]


def playlist_membership(name: str, known_persistent_id: str) -> list[str]:
	"""Read one exact saved user playlist without scanning the Music library."""
	if not known_persistent_id:
		raise MusicAutomationError("A saved Music playlist persistent ID is required.")
	output = _osascript(_PLAYLIST_MEMBERSHIP_SCRIPT, [name, known_persistent_id], timeout=120)
	fields = output.split("\t", 1)
	if fields[0] != "OK":
		raise MusicAutomationError(f"Music returned an unexpected playlist membership result: {output}")
	return fields[1].split(chr(31)) if len(fields) > 1 and fields[1] else []


def edit_music_playlist_membership(
	name: str,
	known_persistent_id: str,
	expected_ids: list[str],
	remove_indexes: list[int],
	append_ids: list[str],
) -> list[str]:
	"""Guard an append/remove mutation with an exact sequence comparison."""
	if any(index < 0 or index >= len(expected_ids) for index in remove_indexes):
		raise MusicAutomationError("A planned playlist removal index is invalid.")
	arguments = [
		name,
		known_persistent_id,
		chr(31).join(expected_ids),
		",".join(str(index) for index in sorted(set(remove_indexes), reverse=True)),
		chr(31).join(append_ids),
	]
	output = _osascript(_EDIT_PLAYLIST_MEMBERSHIP_SCRIPT, arguments, timeout=600)
	fields = output.split("\t", 1)
	if fields[0] != "OK":
		raise MusicAutomationError(f"Music returned an unexpected guarded playlist result: {output}")
	return fields[1].split(chr(31)) if len(fields) > 1 and fields[1] else []
