import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("review activity renders every completed job returned by the backend", () => {
  const source = readFileSync("src/review-problems.tsx", "utf8");
  const completedJobs = source.match(/const recentJobs = ([^;]+);/)?.[1] || "";

  assert.match(completedJobs, /filter\(\(job\) => job\.status === "complete"\)/);
  assert.doesNotMatch(completedJobs, /slice\(/);
});
