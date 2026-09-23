import assert from "node:assert/strict";
import test from "node:test";

import { compactText, jobToast, TOAST_MESSAGE_LIMIT, TOAST_TITLE_LIMIT } from "./notification-model";
import type { ImportJob } from "./types";

function length(value: string): number {
  const Segmenter = (
    Intl as unknown as {
      Segmenter: new (
        locale?: string,
        options?: { granularity: "grapheme" },
      ) => {
        segment(text: string): Iterable<unknown>;
      };
    }
  ).Segmenter;
  return Array.from(new Segmenter(undefined, { granularity: "grapheme" }).segment(value)).length;
}

function fixture(status: ImportJob["status"], type: "album" | "playlist" = "album"): ImportJob {
  return {
    version: 1,
    jobId: "job",
    action: `${type}_combined`,
    source: { type, id: "source", url: "url", name: "A very long source name", total: 12 },
    status,
    phase: status,
    createdAt: "now",
    updatedAt: "now",
    counts: { total: 12, ready: 10, downloaded: 8, reused: 2, complete: 0, review: 2, failed: 0, pending: 0 },
    tracks: [],
    retryable: false,
  };
}

test("toast text truncates by grapheme within both visual budgets", () => {
  const title = compactText("🎵".repeat(80), TOAST_TITLE_LIMIT);
  const message = compactText("👨‍👩‍👧‍👦".repeat(80), TOAST_MESSAGE_LIMIT);
  assert.equal(length(title), TOAST_TITLE_LIMIT);
  assert.equal(length(message), TOAST_MESSAGE_LIMIT);
  assert.ok(title.endsWith("…"));
  assert.ok(message.endsWith("…"));
});

test("terminal job toasts are concise and preserve names plus counts", () => {
  for (const status of ["complete", "needs_attention", "failed"] as const) {
    const value = jobToast(fixture(status));
    assert.ok(length(value.title) <= TOAST_TITLE_LIMIT);
    assert.ok(length(value.message || "") <= TOAST_MESSAGE_LIMIT);
    assert.match(value.message || "", /A very long source name/);
  }
  assert.deepEqual(jobToast(fixture("complete")), {
    style: "success",
    title: "Album added to Music",
    message: "A very long source name · 12 tracks",
  });
  assert.deepEqual(jobToast(fixture("complete", "playlist")), {
    style: "success",
    title: "Playlist added to Music",
    message: "A very long source name · 12 tracks",
  });
});

test("playlist update jobs use update language and change counts", () => {
  const job = fixture("complete", "playlist");
  job.mode = "update";
  job.action = "playlist_update_combined";
  assert.deepEqual(jobToast(job), {
    style: "success",
    title: "Playlist update complete",
    message: "A very long source name · 12 changes",
  });
});

test("long names truncate without cutting off the event count", () => {
  const job = fixture("needs_attention");
  job.source.name = "A name made deliberately much longer than the toast message can display without truncation";
  const value = jobToast(job);
  assert.equal(value.title, "Album needs review");
  assert.ok((value.message || "").endsWith(" · 2 tracks"));
  assert.ok(length(value.message || "") <= TOAST_MESSAGE_LIMIT);
});
