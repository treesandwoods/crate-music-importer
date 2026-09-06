import { Action, ActionPanel, Alert, confirmAlert, List } from "@raycast/api";
import { useEffect, useRef, useState } from "react";
import { dependencyStatus, dependencyUpdate, libraryHealth, type DependencyResult, type HealthResult } from "./backend";
import {
  canUpdate,
  confirmationMessage,
  dependencyLabel,
  dependencySummary,
  groupIssues,
  healthLabel,
  healthSummary,
  issueMarkdown,
  updateTitle,
} from "./health-view-model";

export default function Command() {
  const [healthBusy, setHealthBusy] = useState(true);
  const [dependenciesBusy, setDependenciesBusy] = useState(true);
  const [health, setHealth] = useState<HealthResult>();
  const [result, setResult] = useState<DependencyResult>();
  const [healthError, setHealthError] = useState("");
  const [dependencyError, setDependencyError] = useState("");
  const [updateResult, setUpdateResult] = useState<DependencyResult>();
  const started = useRef(false);
  const operation = useRef(false);
  const healthRunning = useRef(false);
  const dependenciesRunning = useRef(false);

  async function checkHealth(deepAll = false) {
    if (healthRunning.current || operation.current) return;
    healthRunning.current = true;
    setHealthBusy(true);
    setHealthError("");
    try {
      setHealth(await libraryHealth(deepAll));
    } catch (error) {
      setHealthError(String(error));
    } finally {
      healthRunning.current = false;
      setHealthBusy(false);
    }
  }
  async function checkDependencies() {
    if (dependenciesRunning.current || operation.current) return;
    dependenciesRunning.current = true;
    setDependenciesBusy(true);
    setDependencyError("");
    try {
      setResult(await dependencyStatus());
    } catch (error) {
      setResult(undefined);
      setDependencyError(String(error));
    } finally {
      dependenciesRunning.current = false;
      setDependenciesBusy(false);
    }
  }
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void checkHealth();
    void checkDependencies();
  }, []);

  async function update() {
    if (
      !canUpdate(result, healthBusy, dependenciesBusy) ||
      !result?.planId ||
      operation.current ||
      healthRunning.current ||
      dependenciesRunning.current
    )
      return;
    operation.current = true;
    setDependenciesBusy(true);
    try {
      const confirmed = await confirmAlert({
        title: updateTitle(result),
        message: `${confirmationMessage(result)}\n\nOnly these reviewed Homebrew downloader formulae will be updated. The plan is rechecked and active imports block updates.`,
        primaryAction: { title: "Update and Validate" },
        dismissAction: { title: "Cancel", style: Alert.ActionStyle.Cancel },
      });
      if (!confirmed) return;
      setDependencyError("");
      setUpdateResult(undefined);
      const updated = await dependencyUpdate(result.planId);
      setUpdateResult(updated);
      // Keep the completed health report and any update/validation failures visible.
      setResult(undefined);
      setResult(await dependencyStatus());
    } catch (error) {
      setResult(undefined);
      setDependencyError(String(error));
    } finally {
      operation.current = false;
      setDependenciesBusy(false);
    }
  }

  const actions = (
    <ActionPanel>
      {canUpdate(result, healthBusy, dependenciesBusy) && <Action title={updateTitle(result)} onAction={update} />}
      {!dependenciesBusy && <Action title="Check Dependency Versions" onAction={checkDependencies} />}
      {!healthBusy && !dependenciesBusy && <Action title="Refresh Library Health" onAction={() => checkHealth()} />}
      {!healthBusy && !dependenciesBusy && (
        <Action title="Deep Check All Local Music" onAction={() => checkHealth(true)} />
      )}
    </ActionPanel>
  );
  const healthMarkdown = healthError
    ? `# Check Failed\n\n${healthError}`
    : health
      ? `# ${healthLabel(health.status)}\n\n${health.checkedAt}\n\n${health.error || ""}\n\n${healthSummary(health)}\n\n**Checks**\n\n${Object.entries(
          health.checks,
        )
          .map(([name, status]) => `- ${name}: ${status}`)
          .join("\n")}`
      : "Reading Music and checking local files. No library changes will be made.";
  return (
    <List
      isLoading={healthBusy || dependenciesBusy}
      isShowingDetail
      searchBarPlaceholder="Search library health and updates"
    >
      <List.Section title="Library Health">
        <List.Item
          title={healthBusy ? "Checking library…" : healthError ? "Check Failed" : healthLabel(health?.status)}
          detail={<List.Item.Detail markdown={healthMarkdown} />}
          actions={actions}
        />
        {health &&
          groupIssues(health.issues).flatMap((group) =>
            group.issues.map((issue) => (
              <List.Item
                key={issue.id}
                title={issue.title}
                subtitle={issue.tracks[0]?.title}
                accessories={[{ text: issue.severity }]}
                detail={<List.Item.Detail markdown={issueMarkdown(issue)} />}
                actions={
                  <ActionPanel>
                    <Action.CopyToClipboard title="Copy Diagnostic Details" content={JSON.stringify(issue, null, 2)} />
                    {issue.paths.map((path) => (
                      <Action.ShowInFinder key={path} title={`Reveal ${path.split("/").pop()} in Finder`} path={path} />
                    ))}
                  </ActionPanel>
                }
              />
            )),
          )}
      </List.Section>
      <List.Section title="Dependency Updates">
        <List.Item
          title={dependencySummary(result, dependencyError)}
          detail={
            <List.Item.Detail
              markdown={`# Dependency Updates\n\n${dependencySummary(result, dependencyError)}\n\n${dependencyError || result?.error || ""}\n\n${result?.note || ""}`}
            />
          }
          actions={actions}
        />
        {result?.dependencies.map((tool) => (
          <List.Item
            key={tool.name}
            title={tool.name === "ffmpeg" ? "FFmpeg/ffprobe" : tool.name === "deno" ? "Deno" : tool.name}
            accessories={[{ text: dependencyLabel(tool.status) }]}
            detail={
              <List.Item.Detail
                markdown={`# ${tool.name}: ${dependencyLabel(tool.status)}\n\nInstalled: ${tool.previousVersion || "unknown"}\n\nLatest stable: ${tool.availableVersion || "unknown"}\n\nPath: ${tool.resolvedPath || "not found"}\n\nInstallation: ${tool.installationMethod}\n\n${tool.command?.join(" ") || "No eligible update command."}\n\n${tool.error || ""}`}
              />
            }
            actions={actions}
          />
        ))}
        {updateResult && (
          <List.Item
            title="Last Update and Validation Results"
            detail={
              <List.Item.Detail
                markdown={`# Update and Validation Results\n\n\`\`\`json\n${JSON.stringify(updateResult, null, 2)}\n\`\`\``}
              />
            }
            actions={actions}
          />
        )}
      </List.Section>
    </List>
  );
}
