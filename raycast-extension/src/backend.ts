import { backendCommand } from "./runtime";
import { playlistUpdateQueueArgs } from "./playlist-update-model";

import type {
  ImportJob,
  JobsSnapshot,
  PlaylistUpdatePreview,
  ResolverSnapshot,
  SavedPlaylistSummary,
  SourcePreview,
  YouTubeSearchResult,
} from "./types";

function errorMessage(error: unknown): string {
  if (error && typeof error === "object") {
    const stderr = "stderr" in error ? String(error.stderr || "").trim() : "";
    const stdout = "stdout" in error ? String(error.stdout || "").trim() : "";
    if (stderr || stdout) {
      try {
        const result = JSON.parse(stdout) as { error?: string };
        if (result.error) return result.error;
      } catch {
        /* Non-JSON tool failures use stderr below. */
      }
      return (stderr || stdout).replace(/^ERROR:\s*/, "");
    }
  }
  return error instanceof Error ? error.message : String(error);
}

async function run(args: string[], raycast = false, timeout = 120_000): Promise<string> {
  try {
    return await backendCommand(args, raycast, timeout);
  } catch (error) {
    throw new Error(errorMessage(error));
  }
}

async function runJson<T>(args: string[], timeout?: number): Promise<T> {
  const output = await run(args, false, timeout);
  try {
    return JSON.parse(output) as T;
  } catch {
    throw new Error(`The resolver returned invalid JSON: ${output.slice(0, 300)}`);
  }
}

async function runRaycastJson<T>(args: string[], timeout?: number): Promise<T> {
  const output = await run(args, true, timeout);
  try {
    return JSON.parse(output) as T;
  } catch {
    throw new Error(`The job runner returned invalid JSON: ${output.slice(0, 300)}`);
  }
}

export function loadSnapshot(): Promise<ResolverSnapshot> {
  return runJson<ResolverSnapshot>(["review", "--json"]);
}

export function previewSource(type: "album" | "playlist", url: string): Promise<SourcePreview> {
  return runJson<SourcePreview>([type === "album" ? "album-preview" : "preview", url, "--json"], 660_000);
}

export async function loadSavedPlaylists(): Promise<SavedPlaylistSummary[]> {
  return (await runJson<{ playlists: SavedPlaylistSummary[] }>(["saved-playlists", "--json"])).playlists;
}

export function previewPlaylistUpdate(playlistId: string): Promise<PlaylistUpdatePreview> {
  return runJson<PlaylistUpdatePreview>(["playlist-update-preview", playlistId, "--json"], 660_000);
}

export function changePlaylistLink(
  playlistId: string,
  url: string,
): Promise<{ playlist_id: string; spotify_url: string }> {
  return runJson(["playlist-link-set", playlistId, url, "--confirm"]);
}

export function queuePlaylistUpdate(
  playlist: SavedPlaylistSummary,
  preview: PlaylistUpdatePreview,
): Promise<ImportJob> {
  return runRaycastJson<ImportJob>(playlistUpdateQueueArgs(playlist, preview));
}

export function loadJobs(): Promise<JobsSnapshot> {
  return runRaycastJson<JobsSnapshot>(["jobs-json"]);
}

export function acknowledgeJobNotification(jobId: string): Promise<ImportJob> {
  return runRaycastJson<ImportJob>(["ack-notification", jobId]);
}

export function cancelIncompleteJob(jobId: string): Promise<ImportJob> {
  return runRaycastJson<ImportJob>(["cancel-job", jobId]);
}

export function cancelSourceProgress(type: "album" | "playlist", sourceId: string): Promise<unknown> {
  return runRaycastJson(["cancel-source", type, sourceId]);
}

export function retryJob(jobId: string): Promise<ImportJob> {
  return runRaycastJson<ImportJob>(["retry-job", jobId]);
}

export function searchYouTube(recordingId: string, query: string, more = false): Promise<YouTubeSearchResult> {
  return runJson<YouTubeSearchResult>(
    ["search-youtube", recordingId, "--query", query, ...(more ? ["--more"] : [])],
    180_000,
  );
}

export function resolveYouTube(recordingId: string, url: string): Promise<unknown> {
  return runJson(["resolve-youtube", recordingId, url], 180_000);
}

export function resolveMusic(recordingId: string, persistentId: string): Promise<unknown> {
  return runJson(["resolve-music", recordingId, persistentId], 660_000);
}

export function queueSource(
  type: "album" | "playlist",
  url: string,
  seed?: {
    name: string;
    total: number;
    mode?: "import" | "update";
    savedPlaylistId?: string;
    tracks?: Array<{
      position: number;
      trackNumber: number;
      discNumber: number;
      recordingId: string;
      title: string;
      artists: string;
    }>;
  },
): Promise<ImportJob> {
  const action =
    seed?.mode === "update" ? "playlist_update_combined" : type === "album" ? "album_combined" : "playlist_combined";
  const args = ["queue-json", action, url];
  if (seed) args.push(JSON.stringify(seed));
  return runRaycastJson<ImportJob>(args);
}

export interface DependencyResult {
  operation: string;
  operationTime: string;
  planId?: string;
  error?: string;
  note?: string;
  dependencies: Array<{
    name: string;
    resolvedPath: string | null;
    installationMethod: string;
    previousVersion: string | null;
    availableVersion: string | null;
    resultingVersion: string | null;
    status: string;
    error: string | null;
    command: string[] | null;
  }>;
  validation?: Record<string, { path?: string | null; version?: string | null; status?: string; error: string | null }>;
}

export function dependencyStatus(): Promise<DependencyResult> {
  return runJson(["dependencies", "status"], 300_000);
}

export function dependencyUpdate(planId: string): Promise<DependencyResult> {
  return runJson(["dependencies", "update", "--confirm-plan", planId], 2_700_000);
}

export interface HealthIssue {
  id: string;
  severity: "critical" | "warning" | "informational";
  category: string;
  ownership: "importer_owned" | "importer_referenced" | "user_owned" | "untracked" | "ambiguous";
  title: string;
  detail: string;
  persistentIds: string[];
  recordingIds: string[];
  paths: string[];
  tracks: Array<{ artist: string; title: string; album: string; persistent_id: string }>;
  evidence: unknown;
  suggestedAction: string;
  suggestedNextStep: string;
}

export interface HealthResult {
  schemaVersion: number;
  checkedAt: string;
  status: "healthy" | "attention" | "failed";
  error?: string;
  summary: Record<string, number>;
  checks: Record<string, string>;
  issues: HealthIssue[];
}

export interface HealthAuditState {
  status: "idle" | "running" | "complete" | "failed";
  mode: "normal" | "deep";
  startedAt: string | null;
  finishedAt: string | null;
  updatedAt: string | null;
  phase: string;
  checked: number;
  total: number;
  pid: number | null;
  running: boolean;
  error?: string | null;
  lastReport?: HealthResult | null;
}

export function loadHealthAudit(includeReport = true): Promise<HealthAuditState> {
  return runJson(["health-audit", "status", ...(includeReport ? [] : ["--status-only"])], 15_000);
}

export function startHealthAudit(deepAll = false): Promise<HealthAuditState> {
  return runJson(["health-audit", "start", ...(deepAll ? ["--deep-all"] : [])], 15_000);
}
