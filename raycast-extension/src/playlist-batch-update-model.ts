import { canQueuePlaylistUpdate, playlistUpdateConfirmation } from "./playlist-update-model";
import type { ImportJob, PlaylistUpdatePreview, SavedPlaylistSummary } from "./types";

export interface PlaylistBatchEntry {
  playlist: SavedPlaylistSummary;
  preview?: PlaylistUpdatePreview;
  error?: string;
  job?: ImportJob;
  queueError?: string;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export async function loadPlaylistBatch(
  playlists: SavedPlaylistSummary[],
  preview: (id: string) => Promise<PlaylistUpdatePreview>,
  onProgress: (completed: number) => void,
  isCancelled: () => boolean = () => false,
): Promise<PlaylistBatchEntry[]> {
  const entries: PlaylistBatchEntry[] = [];
  // Preview serially so cache refreshes and saved artwork writes do not race.
  for (const playlist of playlists) {
    if (isCancelled()) break;
    try {
      entries.push({ playlist, preview: await preview(playlist.id) });
    } catch (error) {
      entries.push({ playlist, error: errorMessage(error) });
    }
    if (!isCancelled()) onProgress(entries.length);
  }
  return entries;
}

export function pendingPlaylistUpdates(entries: PlaylistBatchEntry[]): PlaylistBatchEntry[] {
  return entries.filter((entry) => !entry.job && canQueuePlaylistUpdate(entry.preview));
}

export function playlistBatchConfirmation(entries: PlaylistBatchEntry[]): string {
  const pending = pendingPlaylistUpdates(entries);
  const skipped = entries.filter((entry) => !entry.job && !canQueuePlaylistUpdate(entry.preview));
  return [
    `Queue updates for ${pending.length} playlists. Updates run in order; follow progress and resolve any tracks needing review in Review Activity.`,
    ...(skipped.length ? [`${skipped.length} up-to-date, blocked, or failed playlists will be skipped.`] : []),
    ...pending.map((entry) => `“${entry.playlist.name}”\n${playlistUpdateConfirmation(entry.preview!)}`),
  ].join("\n\n");
}

export async function queuePlaylistBatch(
  entries: PlaylistBatchEntry[],
  queue: (playlist: SavedPlaylistSummary, preview: PlaylistUpdatePreview) => Promise<ImportJob>,
  onResult: (entry: PlaylistBatchEntry) => void,
): Promise<PlaylistBatchEntry[]> {
  const results: PlaylistBatchEntry[] = [];
  for (const entry of pendingPlaylistUpdates(entries)) {
    let result: PlaylistBatchEntry;
    try {
      result = { ...entry, job: await queue(entry.playlist, entry.preview!), queueError: undefined };
    } catch (error) {
      result = { ...entry, queueError: errorMessage(error) };
    }
    results.push(result);
    onResult(result);
  }
  return results;
}
