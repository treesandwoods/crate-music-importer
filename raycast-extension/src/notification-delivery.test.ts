import assert from "node:assert/strict";
import test from "node:test";

import { deliverPendingNotifications } from "./notification-delivery";
import type { ImportJob } from "./types";

function job(jobId: string): ImportJob {
  return {
    version: 1,
    jobId,
    action: "album_combined",
    source: { type: "album", id: jobId, url: "url", name: jobId, total: 1 },
    status: "complete",
    phase: "complete",
    createdAt: "now",
    updatedAt: "now",
    counts: { total: 1, ready: 1, downloaded: 1, reused: 0, complete: 1, review: 0, failed: 0, pending: 0 },
    tracks: [],
    retryable: false,
  };
}

test("background delivery drains every pending event and acknowledges each one", async () => {
  const shown: string[] = [];
  const acknowledged: string[] = [];
  let pauses = 0;
  const handled = new Set<string>();
  await deliverPendingNotifications([job("one"), job("two")], {
    show: true,
    handled,
    showJob: async (value) => shown.push(value.jobId),
    acknowledge: async (jobId) => acknowledged.push(jobId),
    pauseBetweenToasts: async () => {
      pauses++;
    },
  });
  assert.deepEqual(shown, ["one", "two"]);
  assert.deepEqual(acknowledged, ["one", "two"]);
  assert.deepEqual([...handled], ["one", "two"]);
  assert.equal(pauses, 1);
});

test("foreground activity acknowledges pending events without showing more toasts", async () => {
  const shown: string[] = [];
  const acknowledged: string[] = [];
  await deliverPendingNotifications([job("visible")], {
    show: false,
    handled: new Set<string>(),
    showJob: async (value) => shown.push(value.jobId),
    acknowledge: async (jobId) => acknowledged.push(jobId),
  });
  assert.deepEqual(shown, []);
  assert.deepEqual(acknowledged, ["visible"]);
});

test("failed display remains pending for a later delivery attempt", async () => {
  const acknowledged: string[] = [];
  const handled = new Set<string>();
  await assert.rejects(
    deliverPendingNotifications([job("retry")], {
      show: true,
      handled,
      showJob: async () => {
        throw new Error("toast unavailable");
      },
      acknowledge: async (jobId) => acknowledged.push(jobId),
    }),
  );
  assert.deepEqual(acknowledged, []);
  assert.deepEqual([...handled], []);
});
