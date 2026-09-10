import type { PlaylistUpdatePreview, PlaylistUpdateRemoval, SavedPlaylistSummary } from "./types";

export function removalLabel(removal: PlaylistUpdateRemoval): "unlink" | "delete permanently" {
  return removal.action === "delete" ? "delete permanently" : "unlink";
}

export function canQueuePlaylistUpdate(preview?: PlaylistUpdatePreview): boolean {
  return Boolean(preview && !preview.blocked && !preview.up_to_date);
}

export function playlistUpdateConfirmation(preview: PlaylistUpdatePreview): string {
  const removals = preview.removals.map((row) => `${row.artists} — ${row.title}: ${removalLabel(row)}`);
  return [
    `${preview.additions.length} additions will be appended. ${preview.removals.length} imported occurrences will be removed. Manual Music entries and surviving order are preserved.`,
    ...(removals.length ? ["Removals:", ...removals] : []),
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
