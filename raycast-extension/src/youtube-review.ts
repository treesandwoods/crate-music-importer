import type { ResolverCandidate } from "./types";

export function mergeYouTubeCandidates(
  previous: ResolverCandidate[],
  incoming: ResolverCandidate[],
  rejectedIds: string[] = [],
): ResolverCandidate[] {
  const byId = new Map(previous.filter((c) => c.matchingEvidence && !rejectedIds.includes(c.id)).map((c) => [c.id, c]));
  for (const candidate of incoming) byId.set(candidate.id, candidate);
  return [...byId.values()]
    .sort((a, b) => {
      const left = a.matchingEvidence;
      const right = b.matchingEvidence;
      return (
        (right?.title_similarity ?? 0) - (left?.title_similarity ?? 0) ||
        (right?.artist_evidence ?? 0) - (left?.artist_evidence ?? 0) ||
        Math.abs(left?.duration_difference_s ?? Infinity) - Math.abs(right?.duration_difference_s ?? Infinity)
      );
    })
    .slice(0, 10);
}
