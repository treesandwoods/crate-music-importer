import assert from "node:assert/strict";
import test from "node:test";

import { deliverPendingNotifications, notificationEventKey } from "./notification-delivery";
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
    handled,
    showJob: async (value) => shown.push(value.jobId),
    acknowledge: async (jobId) => acknowledged.push(jobId),
    pauseBetweenToasts: async () => {
      pauses++;
    },
  });
  assert.deepEqual(shown, ["one", "two"]);
  assert.deepEqual(acknowledged, ["one", "two"]);
  assert.deepEqual([...handled], [notificationEventKey(job("one")), notificationEventKey(job("two"))]);
  assert.equal(pauses, 1);
});

test("failed display remains pending for a later delivery attempt", async () => {
  const acknowledged: string[] = [];
  const handled = new Set<string>();
  await assert.rejects(
    deliverPendingNotifications([job("retry")], {
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

test("retry completion is delivered after review for the same job", async () => {
  const handled = new Set<string>();
  const shown: string[] = [];
  const delivery = {
    handled,
    showJob: async (value: ImportJob) => {
      shown.push(value.status);
    },
    acknowledge: async () => {},
  };
  const review = { ...job("same"), status: "needs_attention" as const, finishedAt: "first" };
  const complete = { ...job("same"), finishedAt: "second" };
  await deliverPendingNotifications([review], delivery);
  await deliverPendingNotifications([review, complete], delivery);
  assert.deepEqual(shown, ["needs_attention", "complete"]);
});
