import { Action, ActionPanel, Alert, confirmAlert, Detail } from "@raycast/api";
import { useState } from "react";
import { dependencyStatus, dependencyUpdate, type DependencyResult } from "./backend";

export default function Command() {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<DependencyResult>();
  const [error, setError] = useState("");
  async function check() {
    setBusy(true);
    setError("");
    try {
      setResult(await dependencyStatus());
    } catch (error) {
      setError(String(error));
    } finally {
      setBusy(false);
    }
  }
  async function update() {
    if (!result?.planId || busy) return;
    const commands = result.dependencies.filter((tool) => tool.command).map((tool) => tool.command!.join(" "));
    const confirmed = await confirmAlert({
      title: "Update Crate Music Importer downloader tools?",
      message: `${commands.join("\n") || "No eligible updates. Run version checks and metadata validation only."}\n\nOnly reviewed Homebrew downloader formulae will be updated. Active imports block updates.`,
      primaryAction: { title: "Update and Validate" },
      dismissAction: { title: "Cancel", style: Alert.ActionStyle.Cancel },
    });
    if (!confirmed) return;
    setBusy(true);
    setError("");
    try {
      setResult(await dependencyUpdate(result.planId));
    } catch (error) {
      setError(String(error));
    } finally {
      setBusy(false);
    }
  }
  const markdown = result
    ? [
        "# Crate Music Importer Dependencies",
        result.operationTime,
        ...result.dependencies.map(
          (tool) =>
            `## ${tool.name}: ${tool.status}\n\nInstalled: ${tool.previousVersion || "unknown"} · Available: ${tool.availableVersion || "unknown"} · Result: ${tool.resultingVersion || "unknown"}\n\nPath: \`${tool.resolvedPath || "not found"}\`\n\nInstallation: ${tool.installationMethod}\n\n${tool.command ? `Command: \`${tool.command.join(" ")}\`` : "No update command."}\n\n${tool.error || ""}`,
        ),
        ...(result.validation
          ? Object.entries(result.validation).map(
              ([name, check]) => `**${name} validation:** ${check.error || check.status || check.version || "unknown"}`,
            )
          : []),
        result.note || "",
        result.error || "",
      ].join("\n\n")
    : "# Crate Music Importer Dependencies\n\nCheck installed and available stable versions of yt-dlp, FFmpeg/ffprobe, and Deno. Review the commands and skipped tools before confirming an update. Python and application packages are managed separately.";
  return (
    <Detail
      isLoading={busy}
      markdown={`${markdown}\n\n${error}`}
      actions={
        <ActionPanel>
          <Action
            title="Check Dependency Versions"
            onAction={() => {
              if (!busy) void check();
            }}
          />
          {result?.planId && <Action title="Review and Confirm Updates" onAction={update} />}
        </ActionPanel>
      }
    />
  );
}
