import assert from "node:assert/strict";
import { test } from "node:test";
import { mergeYouTubeCandidates } from "./youtube-review";
import type { ResolverCandidate } from "./types";

function candidate(id: string, delta = 0): ResolverCandidate {
  return {
    kind: "youtube",
    id,
    title: id,
    durationSeconds: 180 + delta,
    reasons: [],
    selectable: true,
    matchingEvidence: {
      title_similarity: 1,
      artist_evidence: 0.94,
      duration_difference_s: delta,
      version_agreement: true,
      metadata_verified: true,
    },
  };
}
test("Search More populates an empty review without padding", () => {
  assert.deepEqual(
    mergeYouTubeCandidates([], [candidate("one")]).map((c) => c.id),
    ["one"],
  );
});
test("merge deduplicates, ranks duration and discards unfiltered legacy rows", () => {
  const legacy = { ...candidate("legacy"), matchingEvidence: undefined };
  const merged = mergeYouTubeCandidates([legacy, candidate("one", 30)], [candidate("one", 20), candidate("two")]);
  assert.deepEqual(
    merged.map((c) => c.id),
    ["two", "one"],
  );
  assert.equal(merged[1].durationSeconds, 200);
});
test("review caps merged results at ten", () => {
  assert.equal(
    mergeYouTubeCandidates(
      [],
      Array.from({ length: 20 }, (_, i) => candidate(String(i))),
    ).length,
    10,
  );
});
