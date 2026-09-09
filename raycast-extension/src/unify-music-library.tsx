import { Action, ActionPanel, Alert, confirmAlert, Icon, List } from "@raycast/api";
import { useEffect, useState } from "react";
import { unification, UnificationState } from "./backend";

export default function Command() {
  const [state, setState] = useState<UnificationState>();
  const [error, setError] = useState<string>();
  async function refresh(start = false) {
    try {
      setState(await unification(start ? "start" : "status"));
      setError(undefined);
    } catch (caught) {
      setError(String(caught));
    }
  }
  useEffect(() => {
    void refresh();
  }, []);
  useEffect(() => {
    if (state?.status !== "running") return;
    const timer = setInterval(() => {
      void refresh();
    }, 5000);
    return () => clearInterval(timer);
  }, [state?.status]);
  async function confirmSettings() {
    if (
      !(await confirmAlert({
        title: "Are both organization options off?",
        message:
          "Open Music Settings > Files. Confirm that Keep Media folder organized and Copy files to Media folder when adding to library are both unchecked.",
        primaryAction: { title: "Both Are Off" },
      }))
    )
      return;
    try {
      setState(await unification("confirm-settings"));
    } catch (caught) {
      setError(String(caught));
    }
  }
  async function organize(operation: "organize" | "resume" | "rollback") {
    const preview = state?.preview;
    if (!preview) return;
    if (
      !(await confirmAlert({
        title: `${operation === "rollback" ? "Roll back" : "Organize"} this exact preview?`,
        message: `${preview.preview_id}: ${preview.tracks.length} Music entries. Files will be copied, verified, and relinked. Original sources move to Trash only after preservation verification.`,
        primaryAction: {
          title: operation === "rollback" ? "Roll Back" : "Apply Verified Plan",
          style: Alert.ActionStyle.Destructive,
        },
      }))
    )
      return;
    try {
      setState(await unification(operation, preview.preview_id));
    } catch (caught) {
      setError(String(caught));
    }
  }
  async function acceptResult() {
    if (
      !state?.preview ||
      !(await confirmAlert({
        title: "Does the unified library look correct?",
        message:
          "Accept the verified result and move the recovery package backup to Trash? The migration journal and original audio recovery files remain available. Trash is never emptied automatically.",
        primaryAction: { title: "Accept and Trash Backup", style: Alert.ActionStyle.Destructive },
      }))
    )
      return;
    try {
      await unification("release-backup", state.preview.preview_id);
      await refresh();
    } catch (caught) {
      setError(String(caught));
    }
  }
  const preview = state?.preview;
  const markdown = [
    "# Unify Music Library",
    "Preview organization using current Music metadata. Existing Music entries and playlists must pass preservation checks before any move.",
    `**Scan:** ${state?.status || "Loading saved result"}${state?.total ? ` · ${state.checked || 0}/${state.total}` : ""}`,
    state?.journal
      ? `**Organization:** ${state.journal.state}. Recovery backup ${state.journal.backup_retained ? "retained until you accept the result" : "moved to Trash"}.`
      : "",
    error || state?.error || "",
    preview
      ? `**Saved preview:** ${preview.preview_id}\n\n${preview.tracks.length} Music entries · ${preview.collisions.length} resolved filename collisions · ${preview.orphans.length} unregistered MP3s · ${preview.possible_recording_duplicates.length} possible recording duplicate groups\n\nTemporary audio copy space: ${(preview.temporary_audio_bytes / 1024 ** 3).toFixed(2)} GiB; recovery package: ${(preview.backup_bytes / 1024 ** 2).toFixed(1)} MiB.`
      : "Run a fresh read-only preview to inspect your library. This scan continues in the background.",
    preview?.blockers.length ? `## Prerequisites\n\n${preview.blockers.map((item) => `- ${item}`).join("\n")}` : "",
  ]
    .filter(Boolean)
    .join("\n\n");
  const actions = (
    <ActionPanel>
      {state?.journal?.state === "verified_awaiting_user_acceptance" && (
        <Action title="Accept Result and Review Backup" icon={Icon.Checkmark} onAction={acceptResult} />
      )}
      {preview && <Action.ShowInFinder title="Show Full Preservation Report" path={preview.report_path} />}
      <Action title="Confirm Music Organization Options" icon={Icon.Checkmark} onAction={confirmSettings} />
      {preview?.ready && state?.status !== "running" && (
        <>
          <Action title="Apply Verified Organization Plan" icon={Icon.Folder} onAction={() => organize("organize")} />
          <Action title="Resume Organization" icon={Icon.ArrowClockwise} onAction={() => organize("resume")} />
          <Action
            title="Roll Back Organization"
            icon={Icon.ArrowCounterClockwise}
            onAction={() => organize("rollback")}
          />
        </>
      )}
      <Action title="Run Fresh Read-Only Preview" icon={Icon.MagnifyingGlass} onAction={() => refresh(true)} />
      <Action title="Refresh Saved Result" icon={Icon.ArrowClockwise} onAction={() => refresh()} />
    </ActionPanel>
  );
  return (
    <List isLoading={!state && !error} isShowingDetail searchBarPlaceholder="Search song, album, path, or Music ID">
      <List.Item
        title="Unification Overview"
        icon={Icon.HardDrive}
        detail={<List.Item.Detail markdown={markdown} />}
        actions={actions}
      />
      <List.Section title={`${preview?.tracks.length || 0} Music entries`}>
        {preview?.tracks.map((row) => (
          <List.Item
            key={row.persistent_id}
            title={(row.target || row.source || row.persistent_id).split("/").pop() || row.persistent_id}
            subtitle={row.status.replaceAll("_", " ")}
            keywords={[row.persistent_id, row.source || "", row.target || ""]}
            detail={
              <List.Item.Detail
                markdown={`# ${row.status.replaceAll("_", " ")}\n\nMusic ID: ${row.persistent_id}\n\n**Current file**\n\n${row.source || "No local file"}\n\n**Proposed file**\n\n${row.target || "No destination"}`}
              />
            }
            actions={actions}
          />
        ))}
      </List.Section>
    </List>
  );
}
