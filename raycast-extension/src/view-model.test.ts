import assert from "node:assert/strict";
import test from "node:test";

import {
  activeSources,
  hasVisibleItems,
  jobPhaseLabel,
  jobProgressSummary,
  jobStageSummary,
  problemSections,
  sameJobs,
  sameSnapshot,
  trackStateLabel,
} from "./view-model";
import type { ImportJob, ResolverSnapshot } from "./types";

function snapshot(): ResolverSnapshot {
  return {
    version: 1,
    managedRoot: "/managed",
    problems: [
      {
        recordingId: "choice",
        title: "Choice",
        artists: "Artist",
        album: "Album",
        durationSeconds: 180,
        kind: "youtube_missing",
        state: "needs_choice",
        message: "Choose",
        candidates: [],
        sources: [],
        defaultSearchQuery: "Artist Choice",
        hasChosenYouTube: false,
      },
      {
        recordingId: "blocked",
        title: "Blocked",
        artists: "Artist",
        album: "Album",
        durationSeconds: 180,
        kind: "album_conflict",
        state: "blocked",
        message: "Blocked",
        candidates: [],
        sources: [],
        defaultSearchQuery: "Artist Blocked",
        hasChosenYouTube: false,
      },
    ],
    readySources: [],
    sources: [],
  };
}

test("groups problems in stable workflow order", () => {
  assert.deepEqual(
    problemSections(snapshot()).map((section) => [section.key, section.problems.map((problem) => problem.recordingId)]),
    [
      ["needs_choice", ["choice"]],
      ["blocked", ["blocked"]],
    ],
  );
});

test("recognizes ready and active source rows as visible", () => {
  const value = snapshot();
  value.problems = [];
  value.sources = [
    {
      type: "album",
      id: "album",
      name: "Album",
      url: "https://open.spotify.com/album/album",
      itemCount: 1,
      activeJob: { pid: 12 },
    },
  ];
  assert.equal(activeSources(value).length, 1);
  assert.equal(hasVisibleItems(value), true);
});

test("polling snapshots reuse unchanged state", () => {
  const job = {
    jobId: "job",
    updatedAt: "2026-09-01T12:00:00Z",
    status: "running",
    phase: "downloading",
  } as ImportJob;
  assert.equal(sameJobs([job], [{ ...job }]), true);
  assert.equal(sameJobs([job], [{ ...job, updatedAt: "2026-09-01T12:00:01Z" }]), false);
  assert.equal(sameSnapshot({ jobs: [job] }, { jobs: [{ ...job }] }), true);
});

test("album progress uses explicit YouTube and Music stages", () => {
  const job = {
    version: 1,
    jobId: "job",
    action: "album_combined",
    status: "running",
    phase: "checking_music_ids",
    source: { type: "album", id: "album", url: "url", name: "Album", total: 5 },
    counts: {
      total: 5,
      notStarted: 1,
      searching: 0,
      matched: 1,
      noMatches: 1,
      downloading: 1,
      approval: 1,
      adding: 0,
      checking: 1,
      ready: 1,
      downloaded: 1,
      reused: 0,
      complete: 0,
      review: 1,
      failed: 0,
      pending: 3,
    },
    tracks: [],
    createdAt: "2026-09-01T12:00:00Z",
    updatedAt: "2026-09-01T12:00:01Z",
    retryable: false,
  } satisfies ImportJob;
  assert.equal(jobPhaseLabel(job), "Checking Music IDs");
  assert.doesNotMatch(jobProgressSummary(job), /^Checking Music IDs/);
  assert.match(jobProgressSummary(job), /Not started 1/);
  assert.match(jobStageSummary(job), /Match found 1/);
  assert.match(jobStageSummary(job), /No match 1/);
  assert.match(jobStageSummary(job), /Needs approval 1/);
  assert.equal(trackStateLabel("youtube_match_found"), "YouTube match found");
  assert.equal(trackStateLabel("matches_need_approval"), "YouTube matches need approval");
  assert.equal(trackStateLabel("checking_music_ids"), "Checking Music IDs");
});

test("completed job summary names completion only once", () => {
  const job = {
    status: "complete",
    phase: "complete",
    counts: { complete: 12 },
  } as ImportJob;
  assert.equal(jobStageSummary(job), "Complete");
});
