import type { ImportJob, ResolverProblem, ResolverSnapshot, SourceReference } from "./types";

export interface ProblemSection {
  key: "needs_choice" | "retryable" | "blocked";
  title: string;
  problems: ResolverProblem[];
}

const sectionTitles = {
  needs_choice: "Needs a Choice",
  retryable: "Retryable Failures",
  blocked: "Blocked Album Conflicts",
} as const;

export function problemSections(snapshot: ResolverSnapshot): ProblemSection[] {
  return (Object.keys(sectionTitles) as ProblemSection["key"][])
    .map((key) => ({
      key,
      title: sectionTitles[key],
      problems: snapshot.problems.filter((problem) => problem.state === key),
    }))
    .filter((section) => section.problems.length > 0);
}

export function activeSources(snapshot: ResolverSnapshot): SourceReference[] {
  return snapshot.sources.filter((source) => Boolean(source.activeJob));
}

export function hasVisibleItems(snapshot: ResolverSnapshot): boolean {
  return snapshot.problems.length > 0 || snapshot.readySources.length > 0 || activeSources(snapshot).length > 0;
}

export function sameJobs(current: ImportJob[], next: ImportJob[]): boolean {
  return (
    current.length === next.length &&
    current.every(
      (job, index) =>
        job.jobId === next[index]?.jobId &&
        job.updatedAt === next[index]?.updatedAt &&
        job.status === next[index]?.status &&
        job.phase === next[index]?.phase,
    )
  );
}

export function sameSnapshot<T>(current: T | undefined, next: T): boolean {
  return current !== undefined && JSON.stringify(current) === JSON.stringify(next);
}

export function jobPhaseLabel(job: ImportJob): string {
  const labels: Record<string, string> = {
    queued: "Queued — not started",
    loading_metadata: "Loading track names",
    loading_music_cache: "Checking saved Music library",
    preview_ready: "Track names ready",
    planning_downloads: "Preparing tracks",
    matching_youtube: "Matching tracks on YouTube",
    searching_youtube: "Searching YouTube",
    downloading: "Downloading",
    tagging: "Updating album tags",
    checking_music_ids: "Checking Music IDs",
    adding_to_music: "Adding to Music",
    complete: "Complete",
    needs_attention: "Needs approval",
    ready_to_continue: "Approved — ready to continue",
    failed: "Failed",
    cancelled: "Removed from queue",
    superseded: "Replaced by a newer attempt",
  };
  return labels[job.phase] || job.phase.replaceAll("_", " ");
}

export function trackStateLabel(state: ImportJob["tracks"][number]["state"]): string {
  const labels: Record<string, string> = {
    not_started: "Not started",
    checking_music_ids: "Checking Music IDs",
    searching_youtube: "Searching YouTube",
    youtube_match_found: "YouTube match found",
    matches_need_approval: "YouTube matches need approval",
    no_youtube_matches: "No YouTube matches",
    downloading: "Downloading",
    tagging: "Updating album tags",
    downloaded: "Downloaded",
    reused: "Existing Music match",
    adding_to_music: "Adding to Music",
    complete: "Complete",
    needs_approval: "Needs approval",
    failed: "Failed",
  };
  return labels[state] || state.replaceAll("_", " ");
}

export function jobProgressSummary(job: ImportJob): string {
  const counts = job.counts;
  const stages: Array<[string, number]> = [
    ["Not started", counts.notStarted || 0],
    ["Searching", counts.searching || 0],
    ["Match found", counts.matched || 0],
    ["No match", counts.noMatches || 0],
    ["Needs approval", counts.approval || counts.review || 0],
    ["Downloading", counts.downloading || 0],
    ["Downloaded", counts.downloaded || 0],
    ["Checking Music IDs", counts.checking || 0],
    ["Existing Music match", counts.reused || 0],
    ["Adding to Music", counts.adding || 0],
    ["Complete", counts.complete || 0],
    ["Failed", counts.failed || 0],
  ];
  const detail = stages
    .filter(([, count]) => count > 0)
    .map(([label, count]) => `${label} ${count}`)
    .join(" · ");
  return detail;
}

export function jobStageSummary(job: ImportJob): string {
  if (job.status === "complete") return "Complete";
  const counts = job.counts;
  if (job.status === "queued") {
    return `${jobPhaseLabel(job)} · ${job.source.total || counts.total || "tracks loading"}`;
  }
  const detail = jobProgressSummary(job);
  return detail ? `${jobPhaseLabel(job)} · ${detail}` : jobPhaseLabel(job);
}
