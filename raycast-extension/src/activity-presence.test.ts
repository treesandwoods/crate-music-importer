import assert from "node:assert/strict";
import test from "node:test";

import { ACTIVITY_PRESENCE_MAX_AGE_MS, clearActivityOpen, isActivityOpen, markActivityOpen } from "./activity-presence";

function storage() {
  const values = new Map<string, string>();
  return {
    values,
    async getItem(key: string) {
      return values.get(key);
    },
    async setItem(key: string, value: string) {
      values.set(key, value);
    },
    async removeItem(key: string) {
      values.delete(key);
    },
  };
}

test("activity presence stays fresh only while the foreground view heartbeats", async () => {
  const store = storage();
  await markActivityOpen(store, "session", 1_000);
  assert.equal(await isActivityOpen(store, 1_000 + ACTIVITY_PRESENCE_MAX_AGE_MS), true);
  assert.equal(await isActivityOpen(store, 1_001 + ACTIVITY_PRESENCE_MAX_AGE_MS), false);
});

test("an older activity instance cannot clear a newer view marker", async () => {
  const store = storage();
  await markActivityOpen(store, "old", 1_000);
  await markActivityOpen(store, "new", 2_000);
  await clearActivityOpen(store, "old");
  assert.equal(await isActivityOpen(store, 2_000), true);
  await clearActivityOpen(store, "new");
  assert.equal(await isActivityOpen(store, 2_000), false);
});
