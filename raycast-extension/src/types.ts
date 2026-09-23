export type ProblemState = "needs_choice" | "retryable" | "blocked";
export type SourceState = ProblemState | "ready" | "complete";
export type JobStatus =
  "queued" | "running" | "complete" | "needs_attention" | "ready_to_continue" | "failed" | "cancelled" | "superseded";
export type JobTrackState =
  | "not_started"
  | "checking_music_ids"
  | "searching_youtube"
  | "youtube_match_found"
  | "matches_need_approval"
  | "no_youtube_matches"
  | "downloading"
  | "tagging"
  | "downloaded"
  | "reused"
  | "adding_to_music"
  | "complete"
  | "needs_approval"
  | "failed";

export interface YouTubeSearchSummary {
  rejected_video_ids?: string[];
  status: "partial" | "no_results" | "no_relevant" | "complete";
  returned: number;
  relevant: number;
  errors: string[];
  elapsed_seconds: number;
  fallback_seconds: number;
}

export interface ResolverCandidate {
  matchingEvidence?: {
    title_similarity: number;
    artist_evidence: number;
    duration_difference_s: number | null;
    version_agreement: boolean;
    metadata_verified: boolean;
  };
  automaticEligible?: boolean;
  kind: "youtube" | "music";
  id: string;
  title: string;
  uploader?: string;
  artist?: string;
  album?: string;
  durationSeconds: number;
  score?: number | null;
  url?: string;
  location?: string | null;
  reasons: string[];
  selectable: boolean;
  albumMatch?: boolean;
}

export interface SourceReference {
  type: "album" | "playlist";
  id: string;
  name: string;
  url: string;
  itemCount: number;
  mode?: "import" | "update";
  state?: SourceState;
  problemCount?: number;
  problemRecordingIds?: string[];
  activeJob?: {
    jobId?: string;
    pid?: number;
    log?: string;
    started_at?: string;
    status?: string;
    phase?: string;
    counts?: JobCounts;
  } | null;
}

export interface ResolverProblem {
  searchSummary?: YouTubeSearchSummary;
  recordingId: string;
  title: string;
  artists: string;
  album: string;
  durationSeconds: number;
  coverUrl?: string | null;
  kind: string;
  state: ProblemState;
  message: string;
  lastError?: string | null;
  candidates: ResolverCandidate[];
  sources: SourceReference[];
  defaultSearchQuery: string;
  hasChosenYouTube: boolean;
  canChooseDifferentYouTube: boolean;
}

export interface ResolverSnapshot {
  version: number;
  managedRoot: string;
  problems: ResolverProblem[];
  sources: SourceReference[];
  readySources: SourceReference[];
}

export interface YouTubeSearchResult {
  searchSummary?: YouTubeSearchSummary;
  recordingId: string;
  query: string;
  candidates: ResolverCandidate[];
}

export interface PreviewRow {
  position: number;
  recording_id: string;
  spotify_id?: string | null;
  title: string;
  artists: string;
  status:
    | "in_library"
    | "not_in_library"
    | "not_started"
    | "youtube_match_found"
    | "matches_need_approval"
    | "no_youtube_matches"
    | "downloaded"
    | "complete"
    | "needs_approval"
    | "failed"
    | "reused_music"
    | "managed_existing"
    | "artwork_update"
    | "upgrade_managed"
    | "review_music"
    | "ready_to_download"
    | "search_required";
  detail: string;
  track_no?: number;
  disc_no?: number;
}

export interface SourcePreview {
  source: {
    type: "album" | "playlist";
    id: string;
    name: string;
    artist?: string;
    year?: number | null;
    url: string;
    total: number;
    warning?: string | null;
  };
  album_id?: string;
  playlist_id?: string;
  counts: Record<string, number>;
  rows: PreviewRow[];
}

export interface SavedPlaylistSummary {
  id: string;
  name: string;
  track_count: number;
  spotify_url: string;
  cover_url?: string | null;
  music_playlist_persistent_id?: string | null;
}

export interface PlaylistUpdateRemoval {
  saved_position: number;
  spotify_id: string;
  recording_id: string;
  title: string;
  artists: string;
  action: "unlink" | "delete";
}

export interface PlaylistUpdateReorder {
  spotify_id: string;
  recording_id: string;
  title: string;
  artists: string;
  from_position: number;
  to_position: number;
}

export interface PlaylistMusicChange {
  kind: "remove" | "restore" | "reorder";
  persistent_id: string;
  recording_id: string;
  title: string;
  artists: string;
  from_position?: number | null;
  to_position?: number | null;
}

export interface PlaylistUpdatePreview {
  source: {
    type: "playlist";
    id: string;
    name: string;
    url: string;
    cover_url?: string | null;
    saved_total: number;
    current_total: number;
  };
  playlist_id: string;
  additions: PreviewRow[];
  removals: PlaylistUpdateRemoval[];
  reorders: PlaylistUpdateReorder[];
  music_changes: PlaylistMusicChange[];
  up_to_date: boolean;
  incomplete_data: boolean;
  blocked: boolean;
  state:
    | "ready"
    | "up_to_date"
    | "incomplete_data"
    | "incomplete_baseline"
    | "missing_music_playlist"
    | "playlist_collision";
  warning?: string | null;
  baseline_backfilled: boolean;
  removals_deferred: boolean;
  confirmation_token: string;
}

export interface JobCounts {
  total: number;
  notStarted?: number;
  searching?: number;
  matched?: number;
  noMatches?: number;
  downloading?: number;
  ready: number;
  downloaded: number;
  reused: number;
  approval?: number;
  adding?: number;
  checking?: number;
  complete: number;
  review: number;
  failed: number;
  pending: number;
}

export interface ImportJobTrack {
  position: number;
  trackNumber: number;
  discNumber: number;
  recordingId: string;
  title: string;
  artists: string;
  state: JobTrackState;
  detail?: string;
}

export interface ImportJob {
  version: number;
  jobId: string;
  action: string;
  mode?: "import" | "update";
  source: {
    type: "album" | "playlist";
    id: string;
    url: string;
    name: string;
    total: number;
  };
  status: JobStatus;
  phase: string;
  pid?: number | null;
  createdAt: string;
  updatedAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  currentTrack?: ImportJobTrack | null;
  counts: JobCounts;
  tracks: ImportJobTrack[];
  errorSummary?: string | null;
  retryable: boolean;
  logPath?: string | null;
  notification?: { pending: boolean; notifiedAt?: string | null };
}

export interface JobsSnapshot {
  version: number;
  managedRoot: string;
  jobs: ImportJob[];
  pendingNotifications: ImportJob[];
}

export interface SpotifyAlbumSummary {
  id: string;
  name: string;
  artists: string;
  url: string;
  imageUrl?: string;
  releaseDate?: string;
  totalTracks: number;
  albumType?: string;
}
