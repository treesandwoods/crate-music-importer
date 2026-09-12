import { mergeYouTubeCandidates } from "./youtube-review";
import { randomUUID } from "node:crypto";
import {
  Action,
  ActionPanel,
  Alert,
  Color,
  confirmAlert,
  Form,
  Icon,
  LaunchType,
  List,
  LaunchProps,
  LocalStorage,
  Toast,
  useNavigation,
} from "@raycast/api";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  acknowledgeJobNotification,
  cancelIncompleteJob,
  cancelSourceProgress,
  loadJobs,
  loadSnapshot,
  queueSource,
  resolveMusic,
  resolveYouTube,
  retryJob,
  searchYouTube,
} from "./backend";
import { clearActivityOpen, isActivityOpen, markActivityOpen } from "./activity-presence";
import { deliverPendingNotifications } from "./notification-delivery";
import { showCompactToast, showTerminalJobToast, updateCompactToast } from "./notifications";
import type {
  ImportJob,
  JobsSnapshot,
  ResolverCandidate,
  ResolverProblem,
  ResolverSnapshot,
  SourceReference,
} from "./types";
import {
  hasVisibleItems,
  jobPhaseLabel,
  jobProgressSummary,
  jobStageSummary,
  problemSections,
  sameSnapshot,
  trackStateLabel,
} from "./view-model";

function score(candidate: ResolverCandidate): string {
  return typeof candidate.score === "number" ? candidate.score.toFixed(4) : "Not scored";
}

function seconds(value: number): string {
  const rounded = Math.max(0, Math.round(value));
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, "0")}`;
}

function markdownText(value: string): string {
  return value.replace(/[\\`*_[\]<>]/g, "\\$&");
}

function problemKind(kind: string): string {
  const labels: Record<string, string> = {
    youtube_missing: "YouTube match needs review",
    youtube_ambiguity: "YouTube match needs review",
    music_ambiguity: "Choose a Music-library copy",
    album_duplicate_conflict: "Choose the proper album copy",
    album_conflict: "Blocked by a user-owned album conflict",
    album_release_conflict: "Blocked by another canonical album",
    retryable_failure: "Download can be retried",
  };
  return labels[kind] || kind.replaceAll("_", " ");
}

function problemIcon(problem: ResolverProblem): Icon {
  if (problem.state === "blocked") return Icon.Lock;
  if (problem.state === "retryable") return Icon.RotateClockwise;
  return problem.kind.startsWith("music") || problem.kind.includes("album_duplicate") ? Icon.Music : Icon.Video;
}

function problemDetail(problem: ResolverProblem): string {
  const artwork = problem.coverUrl
    ? `![Album artwork](${problem.coverUrl}?raycast-width=180&raycast-height=180)\n\n`
    : "";
  const sources = problem.sources
    .map((source) => `- ${source.type === "album" ? "Album" : "Playlist"}: ${markdownText(source.name)}`)
    .join("\n");
  const failure = problem.lastError ? `\n\n### Last failure\n\n${markdownText(problem.lastError)}` : "";
  return `${artwork}# ${markdownText(problem.artists)} — ${markdownText(problem.title)}

${markdownText(problem.message)}

### Recording

- Album: ${markdownText(problem.album || "Unknown")}
- Spotify duration: ${seconds(problem.durationSeconds)}
- Resolution state: ${problem.state.replaceAll("_", " ")}

### Affected sources

${sources || "- Saved recording only"}${failure}`;
}

function candidateDetail(candidate: ResolverCandidate, problem: ResolverProblem): string {
  const delta = Math.round(candidate.durationSeconds - problem.durationSeconds);
  const reasons = candidate.reasons.length
    ? candidate.reasons.map((reason) => `- ${markdownText(reason)}`).join("\n")
    : "- No scoring warnings";
  if (candidate.kind === "youtube") {
    return `# ${markdownText(candidate.title)}

**Expected:** ${markdownText(problem.artists)} — ${markdownText(problem.title)} (${seconds(problem.durationSeconds)})  
**Uploader:** ${markdownText(candidate.uploader || "Unknown")}  
**Candidate duration:** ${seconds(candidate.durationSeconds)} (${delta >= 0 ? "+" : ""}${delta}s vs Spotify)  
**Ranking score:** ${score(candidate)}${"  "}
**Automatic eligibility:** ${candidate.automaticEligible ? "Eligible" : "Review only"}

### Why it needs review

${reasons}`;
  }
  return `# ${markdownText(candidate.artist || "")} — ${markdownText(candidate.title)}

**Album:** ${markdownText(candidate.album || "Unknown")}  
**Duration:** ${seconds(candidate.durationSeconds)} (${delta >= 0 ? "+" : ""}${delta}s vs Spotify)  
**Score:** ${score(candidate)}  
**Ownership:** ${candidate.importerOwned ? "Importer-owned" : "User-owned"}  
**Location:** ${markdownText(candidate.location || "No local file location")}

### Match notes

${reasons}`;
}

async function confirmQueue(source: SourceReference): Promise<boolean> {
  return confirmAlert({
    title: `Continue ${source.type}?`,
    message: `Download any remaining tracks for “${source.name}”, then update Music only if every track is ready.`,
    primaryAction: { title: "Continue Download + Add to Music" },
    dismissAction: { title: "Cancel", style: Alert.ActionStyle.Cancel },
  });
}

function ContinueSourceAction({
  source,
  onQueued,
  title,
}: {
  source: SourceReference;
  onQueued: () => Promise<void>;
  title?: string;
}) {
  return (
    <Action
      title={title || "Continue Download + Add to Music"}
      icon={Icon.Play}
      onAction={async () => {
        if (!(await confirmQueue(source))) return;
        const toast = await showCompactToast(Toast.Style.Animated, "Queueing import");
        try {
          const job = await queueSource(source.type, source.url, {
            name: source.name,
            total: source.itemCount,
            mode: source.mode,
            savedPlaylistId: source.mode === "update" ? source.id : undefined,
          });
          updateCompactToast(
            toast,
            Toast.Style.Success,
            `${source.type === "album" ? "Album" : "Playlist"} queued`,
            `${job.source.total || source.itemCount} tracks`,
          );
          await onQueued();
        } catch (error) {
          updateCompactToast(
            toast,
            Toast.Style.Failure,
            "Could not queue import",
            error instanceof Error ? error.message : String(error),
          );
        }
      }}
    />
  );
}

async function continueSourcesMadeReady(problem: ResolverProblem): Promise<number> {
  const affected = new Set(problem.sources.map((source) => `${source.type}:${source.id}`));
  const snapshot = await loadSnapshot();
  const ready = snapshot.readySources.filter((source) => affected.has(`${source.type}:${source.id}`));
  for (const source of ready) {
    await queueSource(source.type, source.url, {
      name: source.name,
      total: source.itemCount,
      mode: source.mode,
      savedPlaylistId: source.mode === "update" ? source.id : undefined,
    });
  }
  return ready.length;
}

async function resolveYouTubeAndContinue(
  problem: ResolverProblem,
  url: string,
  onResolved: () => Promise<void>,
): Promise<void> {
  await resolveYouTube(problem.recordingId, url);
  let queued = 0;
  let queueMessage = "Choice saved; status updated";
  try {
    queued = await continueSourcesMadeReady(problem);
  } catch (error) {
    queueMessage = `Choice kept; queue later: ${error instanceof Error ? error.message : String(error)}`;
  }
  await onResolved();
  await showCompactToast(
    Toast.Style.Success,
    "Recording saved",
    queued ? (queued === 1 ? "Import queued" : `${queued} imports queued`) : queueMessage,
  );
}

function DirectYouTubeLinkForm({ problem, onResolved }: { problem: ResolverProblem; onResolved: () => Promise<void> }) {
  const [loading, setLoading] = useState(false);
  const { pop } = useNavigation();
  return (
    <Form
      isLoading={loading}
      navigationTitle={`YouTube Link for ${problem.title}`}
      actions={
        <ActionPanel>
          <Action.SubmitForm
            title="Use Direct YouTube Link"
            icon={Icon.Link}
            onSubmit={async (values: { url: string }) => {
              setLoading(true);
              try {
                await resolveYouTubeAndContinue(problem, values.url, onResolved);
                pop();
                pop();
              } catch (error) {
                await showCompactToast(
                  Toast.Style.Failure,
                  "Could not save YouTube link",
                  error instanceof Error ? error.message : String(error),
                );
              } finally {
                setLoading(false);
              }
            }}
          />
        </ActionPanel>
      }
    >
      <Form.TextField id="url" title="YouTube URL" placeholder="https://www.youtube.com/watch?v=…" autoFocus />
      <Form.Description
        text={`Paste the direct YouTube video link for ${problem.artists} — ${problem.title}. The video is inspected and saved only for this song.`}
      />
    </Form>
  );
}

function YouTubeCandidates({ problem, onResolved }: { problem: ResolverProblem; onResolved: () => Promise<void> }) {
  const [candidates, setCandidates] = useState(
    problem.candidates.filter((candidate) => candidate.kind === "youtube" && candidate.matchingEvidence),
  );
  const [query, setQuery] = useState(problem.defaultSearchQuery);
  const [loading, setLoading] = useState(false);
  const searchInFlight = useRef(false);
  const [searchStatus, setSearchStatus] = useState(problem.searchSummary?.status || "");
  const { pop } = useNavigation();

  const searchDefault = useCallback(
    async (more = false) => {
      if (searchInFlight.current) return;
      searchInFlight.current = true;
      setLoading(true);
      try {
        const result = await searchYouTube(problem.recordingId, query, more);
        setCandidates((previous) =>
          more
            ? mergeYouTubeCandidates(previous, result.candidates, result.searchSummary?.rejected_video_ids)
            : result.candidates,
        );
        setSearchStatus(result.searchSummary?.status || "");
        setQuery(result.query);
      } catch (error) {
        await showCompactToast(
          Toast.Style.Failure,
          "YouTube search failed",
          error instanceof Error ? error.message : String(error),
        );
      } finally {
        searchInFlight.current = false;
        setLoading(false);
      }
    },
    [problem.recordingId, query],
  );

  useEffect(() => {
    if (!candidates.length) void searchDefault();
  }, []);

  async function choose(candidate: ResolverCandidate) {
    const confirmed = await confirmAlert({
      title: "Choose this recording?",
      message: `${candidate.uploader || "Unknown uploader"} — ${candidate.title}\nScore ${score(candidate)}; ${seconds(candidate.durationSeconds)} versus Spotify ${seconds(problem.durationSeconds)}.`,
      primaryAction: { title: "Choose Recording" },
      dismissAction: { title: "Cancel", style: Alert.ActionStyle.Cancel },
    });
    if (!confirmed || !candidate.url) return;
    try {
      await resolveYouTubeAndContinue(problem, candidate.url, onResolved);
      pop();
    } catch (error) {
      await showCompactToast(
        Toast.Style.Failure,
        "Could not save recording",
        error instanceof Error ? error.message : String(error),
      );
    }
  }

  return (
    <List
      isLoading={loading}
      isShowingDetail
      navigationTitle={`${problem.artists} — ${problem.title}${searchStatus === "partial" ? " · Search partially completed" : ""}`}
      searchBarPlaceholder={`Filter ${query} results`}
    >
      {candidates.map((candidate) => (
        <List.Item
          key={candidate.id || candidate.url}
          title={candidate.title}
          subtitle={candidate.uploader}
          icon={Icon.Video}
          detail={<List.Item.Detail markdown={candidateDetail(candidate, problem)} />}
          actions={
            <ActionPanel>
              {candidate.url ? <Action.OpenInBrowser title="Preview on YouTube" url={candidate.url} /> : null}
              <Action title="Choose Recording" icon={Icon.CheckCircle} onAction={() => choose(candidate)} />
              <Action title="Search More on YouTube" icon={Icon.MagnifyingGlass} onAction={() => searchDefault(true)} />
              <Action.Push
                title="Provide Direct YouTube Link"
                icon={Icon.Link}
                target={<DirectYouTubeLinkForm problem={problem} onResolved={onResolved} />}
              />
            </ActionPanel>
          }
        />
      ))}
      {!candidates.length && !loading ? (
        <List.EmptyView
          title={
            searchStatus === "partial"
              ? "Search partially completed"
              : searchStatus === "no_results"
                ? "No results returned"
                : "No relevant recordings"
          }
          description="Paste a direct YouTube video link for this song."
          actions={
            <ActionPanel>
              <Action title="Search More on YouTube" icon={Icon.MagnifyingGlass} onAction={() => searchDefault(true)} />
              <Action.Push
                title="Provide Direct YouTube Link"
                icon={Icon.Link}
                target={<DirectYouTubeLinkForm problem={problem} onResolved={onResolved} />}
              />
            </ActionPanel>
          }
        />
      ) : null}
    </List>
  );
}

function MusicCandidates({ problem, onResolved }: { problem: ResolverProblem; onResolved: () => Promise<void> }) {
  const { pop } = useNavigation();
  const candidates = problem.candidates.filter((candidate) => candidate.kind === "music");

  async function choose(candidate: ResolverCandidate) {
    if (!candidate.selectable) return;
    const confirmed = await confirmAlert({
      title: candidate.promotionEligible ? "Promote this track to the album?" : "Use this Music track?",
      message: candidate.promotionEligible
        ? `${candidate.artist || ""} — ${candidate.title}\nThe importer-owned Playlist Imports track will be updated in place with ${problem.album} metadata. Its Music ID and current playlist memberships will be preserved.`
        : `${candidate.artist || ""} — ${candidate.title}\nAlbum: ${candidate.album || "Unknown"}. The existing file will not be retagged or moved.`,
      primaryAction: { title: candidate.promotionEligible ? "Approve Album Promotion" : "Use This Music Track" },
      dismissAction: { title: "Cancel", style: Alert.ActionStyle.Cancel },
    });
    if (!confirmed) return;
    try {
      await resolveMusic(problem.recordingId, candidate.id);
      await onResolved();
      pop();
    } catch (error) {
      await showCompactToast(
        Toast.Style.Failure,
        "Could not select track",
        error instanceof Error ? error.message : String(error),
      );
    }
  }

  return (
    <List isShowingDetail navigationTitle={`${problem.artists} — ${problem.title}`}>
      {candidates.map((candidate) => (
        <List.Item
          key={candidate.id}
          title={candidate.title}
          subtitle={`${candidate.artist || ""} — ${candidate.album || "Unknown album"}`}
          icon={candidate.selectable ? Icon.Music : Icon.XMarkCircle}
          detail={<List.Item.Detail markdown={candidateDetail(candidate, problem)} />}
          actions={
            <ActionPanel>
              {candidate.selectable ? (
                <Action
                  title={candidate.promotionEligible ? "Approve Album Promotion" : "Use This Music Track"}
                  icon={Icon.CheckCircle}
                  onAction={() => choose(candidate)}
                />
              ) : null}
              {candidate.location ? <Action.ShowInFinder path={candidate.location} /> : null}
            </ActionPanel>
          }
        />
      ))}
    </List>
  );
}

function ProblemActions({ problem, refresh }: { problem: ResolverProblem; refresh: () => Promise<void> }) {
  const youtubeProblem = problem.kind === "youtube_missing" || problem.kind === "youtube_ambiguity";
  const musicProblem =
    problem.kind === "music_ambiguity" ||
    problem.kind === "album_conflict" ||
    problem.kind === "album_duplicate_conflict";
  const sourceTypeCounts = problem.sources.reduce<Record<SourceReference["type"], number>>(
    (counts, source) => ({ ...counts, [source.type]: counts[source.type] + 1 }),
    { album: 0, playlist: 0 },
  );
  return (
    <ActionPanel>
      {problem.state === "needs_choice" && youtubeProblem ? (
        <Action.Push
          title="Review YouTube Candidates"
          icon={Icon.Video}
          target={<YouTubeCandidates problem={problem} onResolved={refresh} />}
        />
      ) : null}
      {problem.state === "needs_choice" && musicProblem ? (
        <Action.Push
          title="Review Music Candidates"
          icon={Icon.Music}
          target={<MusicCandidates problem={problem} onResolved={refresh} />}
        />
      ) : null}
      {problem.state === "retryable" && problem.hasChosenYouTube && problem.canChooseDifferentYouTube ? (
        <Action.Push
          title="Choose a Different Recording"
          icon={Icon.Video}
          target={<YouTubeCandidates problem={{ ...problem, candidates: [] }} onResolved={refresh} />}
        />
      ) : null}
      {problem.state === "retryable"
        ? problem.sources.map((source) => (
            <ContinueSourceAction
              key={`${source.type}-${source.id}`}
              source={source}
              title={`Retry ${source.type === "album" ? "Album" : "Playlist"}${sourceTypeCounts[source.type] > 1 ? `: ${source.name}` : ""}`}
              onQueued={refresh}
            />
          ))
        : null}
      {problem.sources.map((source) => (
        <Action.OpenInBrowser
          key={`open-${source.type}-${source.id}`}
          title={`Open ${source.type === "album" ? "Album" : "Playlist"}${sourceTypeCounts[source.type] > 1 ? `: ${source.name}` : ""} in Spotify`}
          url={source.url}
        />
      ))}
      <Action title="Refresh Problems" icon={Icon.ArrowClockwise} onAction={refresh} />
    </ActionPanel>
  );
}

function jobIcon(job: ImportJob): { source: Icon; tintColor: Color } {
  if (job.status === "complete") return { source: Icon.CheckCircle, tintColor: Color.Green };
  if (job.status === "needs_attention") return { source: Icon.ExclamationMark, tintColor: Color.Orange };
  if (job.status === "failed") return { source: Icon.XMarkCircle, tintColor: Color.Red };
  if (job.status === "queued") return { source: Icon.List, tintColor: Color.SecondaryText };
  return { source: Icon.Clock, tintColor: Color.Blue };
}

function jobDetail(job: ImportJob): string {
  const current = job.currentTrack?.title
    ? `\n\n**Current track:** ${markdownText(job.currentTrack.artists ? `${job.currentTrack.artists} — ${job.currentTrack.title}` : job.currentTrack.title)}`
    : "";
  const tracks = job.tracks.length
    ? job.tracks
        .map((track) =>
          job.status === "complete"
            ? `${track.position}. ${markdownText(track.artists)} — ${markdownText(track.title)}`
            : `${track.position}. **${trackStateLabel(track.state).toUpperCase()}** — ${markdownText(track.artists)} — ${markdownText(track.title)}`,
        )
        .join("\n")
    : "Track details will appear after Spotify metadata loads.";
  const error = job.errorSummary ? `\n\n### Attention\n\n${markdownText(job.errorSummary)}` : "";
  const progress = jobProgressSummary(job);
  const summary = job.status === "complete" || !progress ? "" : `\n\n${progress}`;
  return `# ${markdownText(job.source.name)}

**${job.mode === "update" ? "Playlist Update" : job.source.type === "album" ? "Album" : "Playlist"} · ${jobPhaseLabel(job)}**${summary}${current}

### Tracks

${tracks}${error}`;
}

function CancelProgressAction({ job, refresh }: { job: ImportJob; refresh: () => Promise<void> }) {
  const update = job.mode === "update";
  return (
    <Action
      title={update ? "Cancel Update and Keep Progress" : "Cancel and Delete Progress"}
      icon={Icon.Trash}
      style={Action.Style.Destructive}
      onAction={async () => {
        const confirmed = await confirmAlert({
          title: update ? "Cancel this update?" : "Cancel and delete progress?",
          message: update
            ? "This stops the update job and keeps the saved playlist, downloaded additions, manifest checkpoints, Music items, and managed files for a later retry."
            : "This removes the unfinished source and every unshared track created by this attempt from Music, the cache, the manifest, and managed files. Tracks that were already cached before the attempt and shared tracks are kept.",
          primaryAction: { title: "Cancel and Delete", style: Alert.ActionStyle.Destructive },
          dismissAction: { title: "Keep", style: Alert.ActionStyle.Cancel },
        });
        if (!confirmed) return;
        try {
          await cancelIncompleteJob(job.jobId);
          await refresh();
        } catch (error) {
          await showCompactToast(
            Toast.Style.Failure,
            "Could not cancel import",
            error instanceof Error ? error.message : String(error),
          );
        }
      }}
    />
  );
}

function CancelSourceProgressAction({ source, refresh }: { source: SourceReference; refresh: () => Promise<void> }) {
  return (
    <Action
      title="Cancel and Delete Progress"
      icon={Icon.Trash}
      style={Action.Style.Destructive}
      onAction={async () => {
        const confirmed = await confirmAlert({
          title: "Cancel and delete progress?",
          message:
            "This removes the unfinished source and every unshared track created by this attempt from Music, the cache, the manifest, and managed files. Tracks that were already cached before the attempt and shared tracks are kept.",
          primaryAction: { title: "Cancel and Delete", style: Alert.ActionStyle.Destructive },
          dismissAction: { title: "Keep", style: Alert.ActionStyle.Cancel },
        });
        if (!confirmed) return;
        try {
          await cancelSourceProgress(source.type, source.id);
          await refresh();
        } catch (error) {
          await showCompactToast(
            Toast.Style.Failure,
            "Could not cancel import",
            error instanceof Error ? error.message : String(error),
          );
        }
      }}
    />
  );
}

function JobActions({ job, refresh }: { job: ImportJob; refresh: () => Promise<void> }) {
  return (
    <ActionPanel>
      <Action title="Refresh Activity" icon={Icon.ArrowClockwise} onAction={refresh} />
      {job.status !== "complete" ? <CancelProgressAction job={job} refresh={refresh} /> : null}
      {job.status === "failed" && job.retryable ? (
        <Action
          title="Retry Download + Add to Music"
          icon={Icon.ArrowClockwise}
          onAction={async () => {
            const confirmed = await confirmAlert({
              title: "Retry this import?",
              message:
                "Resume saved work, download remaining tracks, then update Music only when every track is ready.",
              primaryAction: { title: "Retry Download + Add to Music" },
              dismissAction: { title: "Cancel", style: Alert.ActionStyle.Cancel },
            });
            if (!confirmed) return;
            const toast = await showCompactToast(Toast.Style.Animated, "Queueing import");
            try {
              const retried = await retryJob(job.jobId);
              updateCompactToast(
                toast,
                Toast.Style.Success,
                `${job.source.type === "album" ? "Album" : "Playlist"} queued`,
                `${retried.source.total || job.counts.total} tracks`,
              );
              await refresh();
            } catch (error) {
              updateCompactToast(
                toast,
                Toast.Style.Failure,
                "Could not queue import",
                error instanceof Error ? error.message : String(error),
              );
            }
          }}
        />
      ) : null}
      <Action.OpenInBrowser title="Open in Spotify" url={job.source.url} />
      {job.logPath ? <Action.ShowInFinder title="Show Job Log in Finder" path={job.logPath} /> : null}
    </ActionPanel>
  );
}

interface ActivityContext {
  jobId?: string;
  terminalEvent?: boolean;
}

export default function Command(props: LaunchProps<{ launchContext: ActivityContext }>) {
  const [snapshot, setSnapshot] = useState<ResolverSnapshot>();
  const [jobs, setJobs] = useState<JobsSnapshot>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const shownNotifications = useRef(new Set<string>());
  const refreshInFlight = useRef(false);
  const activitySession = useRef(randomUUID());

  const handlePending = useCallback(async (values: ImportJob[], show: boolean) => {
    await deliverPendingNotifications(values, {
      show,
      handled: shownNotifications.current,
      showJob: showTerminalJobToast,
      acknowledge: acknowledgeJobNotification,
      pauseBetweenToasts: () => new Promise((resolve) => setTimeout(resolve, 2_000)),
    });
  }, []);

  const refresh = useCallback(
    async (acknowledgeNotifications = true, showLoading = true) => {
      if (refreshInFlight.current) return;
      refreshInFlight.current = true;
      if (showLoading) setLoading(true);
      try {
        const [nextSnapshot, nextJobs] = await Promise.all([loadSnapshot(), loadJobs()]);
        setSnapshot((current) => (sameSnapshot(current, nextSnapshot) ? current : nextSnapshot));
        setJobs((current) => (sameSnapshot(current, nextJobs) ? current : nextJobs));
        setError(undefined);
        if (acknowledgeNotifications && props.launchType === LaunchType.UserInitiated) {
          await handlePending(nextJobs.pendingNotifications, false);
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        refreshInFlight.current = false;
        if (showLoading) setLoading(false);
      }
    },
    [handlePending, props.launchType],
  );

  useEffect(() => {
    async function initialLoad() {
      if (props.launchContext?.jobId && props.launchContext.terminalEvent) {
        try {
          const pending = (await loadJobs()).pendingNotifications;
          let activityOpen = false;
          try {
            activityOpen = await isActivityOpen(LocalStorage);
          } catch {
            // Local presence is only a toast-suppression hint; delivery remains the safe default.
          }
          await handlePending(pending, !activityOpen);
        } catch {
          // Pending events remain durable for the next successful background or foreground launch.
        }
      }
      await refresh(props.launchType === LaunchType.UserInitiated);
    }
    void initialLoad();
  }, [handlePending, props.launchContext?.jobId, props.launchContext?.terminalEvent, props.launchType, refresh]);

  useEffect(() => {
    if (props.launchType !== LaunchType.UserInitiated) return;
    const sessionId = activitySession.current;
    const heartbeat = () => void markActivityOpen(LocalStorage, sessionId).catch(() => undefined);
    heartbeat();
    const timer = setInterval(heartbeat, 2_000);
    return () => {
      clearInterval(timer);
      void clearActivityOpen(LocalStorage, sessionId).catch(() => undefined);
    };
  }, [props.launchType]);

  useEffect(() => {
    const active = jobs?.jobs.some((job) => job.status === "queued" || job.status === "running");
    if (!active) return;
    const timer = setInterval(() => void refresh(true, false), 1_000);
    return () => clearInterval(timer);
  }, [jobs?.jobs, refresh]);

  const sections = snapshot ? problemSections(snapshot) : [];
  const activeJobs = jobs?.jobs.filter((job) => job.status === "queued" || job.status === "running") || [];
  const attentionJobs = jobs?.jobs.filter((job) => job.status === "needs_attention" || job.status === "failed") || [];
  const recentJobs = jobs?.jobs.filter((job) => job.status === "complete").slice(0, 50) || [];

  return (
    <List
      isLoading={loading}
      isShowingDetail
      navigationTitle="Review Activity & Problems"
      searchBarPlaceholder="Filter imports and tracks"
    >
      {activeJobs.length ? (
        <List.Section title="Active & Queued" subtitle={`${activeJobs.length}`}>
          {activeJobs.map((job) => (
            <List.Item
              key={job.jobId}
              title={job.source.name}
              subtitle={jobStageSummary(job)}
              icon={jobIcon(job)}
              detail={<List.Item.Detail markdown={jobDetail(job)} />}
              actions={<JobActions job={job} refresh={refresh} />}
            />
          ))}
        </List.Section>
      ) : null}
      {attentionJobs.length ? (
        <List.Section title="Imports Needing Attention" subtitle={`${attentionJobs.length}`}>
          {attentionJobs.map((job) => (
            <List.Item
              key={job.jobId}
              title={job.source.name}
              subtitle={jobStageSummary(job)}
              icon={jobIcon(job)}
              detail={<List.Item.Detail markdown={jobDetail(job)} />}
              actions={<JobActions job={job} refresh={refresh} />}
            />
          ))}
        </List.Section>
      ) : null}
      {sections.map((section) => (
        <List.Section key={section.key} title={section.title} subtitle={`${section.problems.length}`}>
          {section.problems.map((problem) => (
            <List.Item
              key={problem.recordingId}
              title={`${problem.artists} — ${problem.title}`}
              subtitle={problemKind(problem.kind)}
              icon={{
                source: problemIcon(problem),
                tintColor:
                  problem.state === "blocked" ? Color.Red : problem.state === "retryable" ? Color.Orange : Color.Yellow,
              }}
              detail={<List.Item.Detail markdown={problemDetail(problem)} />}
              actions={<ProblemActions problem={problem} refresh={refresh} />}
            />
          ))}
        </List.Section>
      ))}
      {snapshot?.readySources.length ? (
        <List.Section title="Ready to Continue" subtitle={`${snapshot.readySources.length}`}>
          {snapshot.readySources.map((source) => {
            return (
              <List.Item
                key={`${source.type}-${source.id}`}
                title={source.name}
                subtitle={`${source.type === "album" ? "Album" : "Playlist"} · ${source.itemCount} tracks`}
                icon={{ source: Icon.CheckCircle, tintColor: Color.Green }}
                detail={
                  <List.Item.Detail
                    markdown={`# ${markdownText(source.name)}\n\nEvery saved problem for this ${source.type} is resolved. Continue to download remaining tracks and update Music.`}
                  />
                }
                actions={
                  <ActionPanel>
                    <ContinueSourceAction source={source} onQueued={refresh} />
                    <CancelSourceProgressAction source={source} refresh={refresh} />
                    <Action.OpenInBrowser title="Open in Spotify" url={source.url} />
                    <Action title="Refresh Problems" icon={Icon.ArrowClockwise} onAction={refresh} />
                  </ActionPanel>
                }
              />
            );
          })}
        </List.Section>
      ) : null}
      {recentJobs.length ? (
        <List.Section title="Recently Completed" subtitle={`${recentJobs.length}`}>
          {recentJobs.map((job) => (
            <List.Item
              key={job.jobId}
              title={job.source.name}
              subtitle={jobStageSummary(job)}
              icon={jobIcon(job)}
              detail={<List.Item.Detail markdown={jobDetail(job)} />}
              actions={<JobActions job={job} refresh={refresh} />}
            />
          ))}
        </List.Section>
      ) : null}
      {error ? (
        <List.EmptyView
          title="Could not load import problems"
          description={error}
          actions={
            <ActionPanel>
              <Action title="Try Again" icon={Icon.ArrowClockwise} onAction={refresh} />
            </ActionPanel>
          }
        />
      ) : null}
      {snapshot && jobs && !hasVisibleItems(snapshot) && jobs.jobs.length === 0 && !error ? (
        <List.EmptyView
          title="No import activity"
          description="Search for an album or import a playlist link to begin."
          icon={Icon.CheckCircle}
        />
      ) : null}
    </List>
  );
}
