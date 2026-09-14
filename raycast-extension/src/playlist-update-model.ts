import type { PlaylistUpdatePreview, PlaylistUpdateRemoval, SavedPlaylistSummary } from "./types";

export function removalLabel(removal: PlaylistUpdateRemoval): "unlink" | "delete permanently" {
  return removal.action === "delete" ? "delete permanently" : "unlink";
}

export function canQueuePlaylistUpdate(preview?: PlaylistUpdatePreview): boolean {
  return Boolean(preview && !preview.blocked && !preview.up_to_date);
}

export function playlistUpdateConfirmation(preview: PlaylistUpdatePreview): string {
  const removals = preview.removals.map((row) => `${row.artists} — ${row.title}: ${removalLabel(row)}`);
  const musicRemovals = preview.music_changes
    .filter((row) => row.kind === "remove")
    .map((row) => `${row.artists ? `${row.artists} — ` : ""}${row.title}: remove from this playlist only`);
  return [
    `This will make the Music.app playlist exactly match Spotify: ${preview.additions.length} additions, ${preview.removals.length} Spotify removals, ${preview.reorders.length} Spotify position changes, and ${preview.music_changes.length} Music.app corrections. Unexpected manual playlist entries will be removed from this playlist but not deleted from the Music library.`,
    ...(removals.length ? ["Removals:", ...removals] : []),
    ...(musicRemovals.length ? ["Music.app-only entries:", ...musicRemovals] : []),
  ].join("\n");
}

export function playlistUpdateQueueArgs(playlist: SavedPlaylistSummary, preview: PlaylistUpdatePreview): string[] {
  const tracks = [
    ...preview.additions.map((row) => ({
      position: row.position,
      trackNumber: row.position,
      discNumber: 1,
      recordingId: row.recording_id,
      title: row.title,
      artists: row.artists,
    })),
    ...preview.removals.map((row) => ({
      position: row.saved_position,
      trackNumber: row.saved_position,
      discNumber: 1,
      recordingId: row.recording_id,
      title: row.title,
      artists: row.artists,
    })),
    ...preview.reorders.map((row) => ({
      position: row.to_position,
      trackNumber: row.to_position,
      discNumber: 1,
      recordingId: row.recording_id,
      title: row.title,
      artists: row.artists,
    })),
    ...preview.music_changes.map((row) => ({
      position: row.to_position ?? row.from_position ?? 0,
      trackNumber: row.to_position ?? row.from_position ?? 0,
      discNumber: 1,
      recordingId: row.recording_id,
      title: row.title,
      artists: row.artists,
    })),
  ];
  return [
    "queue-json",
    "playlist_update_combined",
    playlist.spotify_url,
    JSON.stringify({
      savedPlaylistId: playlist.id,
      name: playlist.name,
      total: tracks.length,
      tracks,
      confirmationToken: preview.confirmation_token,
    }),
  ];
}
