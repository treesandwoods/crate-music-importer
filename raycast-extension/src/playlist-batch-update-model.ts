import { canQueuePlaylistUpdate } from "./playlist-update-model";
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
  const totals = { additions: 0, removals: 0, reorders: 0, corrections: 0, deletions: 0 };
  for (const { preview } of pending) {
    totals.additions += preview!.additions.length;
    totals.removals += preview!.removals.length;
    totals.reorders += preview!.reorders.length;
    totals.corrections += preview!.music_changes.length;
    totals.deletions += preview!.removals.filter((row) => row.action === "delete").length;
  }
  const changes = [
    [totals.additions, "addition"],
    [totals.removals, "removal"],
    [totals.reorders, "position change"],
    [totals.corrections, "Music.app correction"],
  ]
    .filter(([count]) => count)
    .map(([count, label]) => `${count} ${label}${count === 1 ? "" : "s"}`)
    .join(" · ");
  return [
    `Sync with Spotify: ${changes || "playlist changes"}.`,
    ...(skipped.length ? [`${skipped.length} playlist${skipped.length === 1 ? "" : "s"} skipped.`] : []),
    ...(totals.deletions
      ? [
          `${totals.deletions} track${totals.deletions === 1 ? "" : "s"} will be permanently deleted from Music and disk.`,
        ]
      : []),
    ...(totals.removals > totals.deletions ||
    pending.some(({ preview }) => preview!.music_changes.some((row) => row.kind === "remove"))
      ? [totals.deletions ? "Other removals only unlink playlist entries." : "Removals only unlink playlist entries."]
      : []),
    "Follow progress in Review Activity.",
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
