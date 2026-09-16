import type { DependencyResult, HealthIssue, HealthResult } from "./backend";

export function eligibleUpdates(result?: DependencyResult) {
  if (result?.error) return [];
  return result?.dependencies.filter((tool) => tool.status === "outdated" && tool.command?.length) || [];
}

export function canUpdate(result: DependencyResult | undefined, healthBusy: boolean, dependenciesBusy: boolean) {
  return eligibleUpdates(result).length > 0 && Boolean(result?.planId) && !healthBusy && !dependenciesBusy;
}

export function updateTitle(result?: DependencyResult) {
  const count = eligibleUpdates(result).length;
  return `Update ${count} Stable ${count === 1 ? "Dependency" : "Dependencies"}…`;
}

export function confirmationMessage(result: DependencyResult) {
  return eligibleUpdates(result)
    .map(
      (tool) =>
        `${tool.name}: ${tool.previousVersion || "unknown"} → ${tool.availableVersion}\n${tool.command!.join(" ")}`,
    )
    .join("\n\n");
}

export function dependencySummary(result?: DependencyResult, error = "") {
  if (error || result?.error || result?.dependencies.some((tool) => tool.status === "failed"))
    return "Update availability could not be checked";
  if (!result) return "Checking stable dependency updates…";
  if (eligibleUpdates(result).length) return updateTitle(result);
  if (result.dependencies.some((tool) => tool.status === "current")) return "All eligible dependencies are current";
  return "No safe stable updates are eligible";
}

export function dependencyLabel(status: string) {
  return (
    {
      current: "Current",
      outdated: "Update Available",
      skipped: "Skipped",
      failed: "Check Failed",
      updated: "Updated",
    }[status] || status
  );
}

export function healthLabel(status?: HealthResult["status"]) {
  return status
    ? { healthy: "Healthy", attention: "Attention Needed", failed: "Check Failed" }[status]
    : "Checking library…";
}

export function healthSummary(result: HealthResult) {
  const labels: Record<string, string> = {
    musicTracks: "Music tracks scanned",
    localFiles: "Local files checked",
    managedFiles: "Crate-managed files",
    bindingRepairs: "Music bindings repaired",
    missingFiles: "Missing or unreadable files",
    corruptFiles: "Corrupt or truncated audio",
    exactDuplicateGroups: "Exact duplicate groups",
    possibleRecordingDuplicates: "Possible recording duplicates",
    manifestInconsistencies: "Manifest/Music inconsistencies",
    metadataDiscrepancies: "Duration, tag, or artwork discrepancies",
    deepDecodedFiles: "Files fully decoded",
  };
  return Object.entries(labels)
    .map(([key, label]) => `- ${label}: ${result.summary[key] ?? "Not checked"}`)
    .join("\n");
}

export function groupIssues(issues: HealthIssue[]) {
  return (["critical", "warning", "informational"] as const)
    .map((severity) => ({
      severity,
      issues: issues
        .filter((issue) => issue.severity === severity)
        .sort((a, b) => a.category.localeCompare(b.category) || a.id.localeCompare(b.id)),
    }))
    .filter((group) => group.issues.length);
}

export function issueMarkdown(issue: HealthIssue) {
  return [
    `# ${issue.title}`,
    `**Severity:** ${issue.severity}`,
    issue.detail,
    ...issue.tracks.map(
      (track) =>
        `Artist: ${track.artist || "Unknown"}\n\nTitle: ${track.title || "Unknown"}\n\nAlbum: ${track.album || "Unknown"}`,
    ),
    `**Music persistent IDs:** ${issue.persistentIds.join(", ") || "None"}`,
    `**Manifest recording IDs:** ${issue.recordingIds.join(", ") || "None"}`,
    `**File paths:**\n\n${issue.paths.join("\n\n") || "No local file"}`,
    `**Evidence**\n\n\`\`\`json\n${JSON.stringify(issue.evidence, null, 2)}\n\`\`\``,
    `**Suggested next step:** ${issue.suggestedNextStep}`,
    "Report only. No automatic deletion or metadata changes are offered.",
  ].join("\n\n");
}
