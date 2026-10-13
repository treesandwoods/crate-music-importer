import {
  Action,
  ActionPanel,
  Alert,
  Color,
  confirmAlert,
  Icon,
  launchCommand,
  LaunchType,
  List,
  Toast,
} from "@raycast/api";
import { useEffect, useRef, useState } from "react";

import { loadSavedPlaylists, previewPlaylistUpdate, queuePlaylistUpdate } from "./backend";
import { compactText } from "./notification-model";
import { showCompactToast, updateCompactToast } from "./notifications";
import {
  loadPlaylistBatch,
  pendingPlaylistUpdates,
  playlistBatchConfirmation,
  queuePlaylistBatch,
  type PlaylistBatchEntry,
} from "./playlist-batch-update-model";
import { removalLabel } from "./playlist-update-model";

export function PlaylistBatchUpdatePreviewView() {
  const [entries, setEntries] = useState<PlaylistBatchEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [queueing, setQueueing] = useState(false);
  const [completed, setCompleted] = useState(0);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string>();
  const [revision, setRevision] = useState(0);
  const busy = useRef(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setEntries([]);
      setCompleted(0);
      setTotal(0);
      setError(undefined);
      try {
        const playlists = await loadSavedPlaylists();
        if (cancelled) return;
        setTotal(playlists.length);
        const next = await loadPlaylistBatch(playlists, previewPlaylistUpdate, setCompleted, () => cancelled);
        if (!cancelled) setEntries(next);
      } catch (caught) {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [revision]);

  const pending = pendingPlaylistUpdates(entries);
  const queued = entries.filter((entry) => entry.job).length;
  const skipped = entries.length - pending.length - queued;
  const deletes = pending.some((entry) => entry.preview?.removals.some((row) => row.action === "delete"));

  async function acceptAll() {
    if (busy.current || loading || !pending.length) return;
    busy.current = true;
    setQueueing(true);
    try {
      const confirmed = await confirmAlert({
        title: `Update ${pending.length} Playlists?`,
        message: playlistBatchConfirmation(entries),
        primaryAction: {
          title: deletes ? "Update and Delete" : "Accept All Updates",
          style: deletes ? Alert.ActionStyle.Destructive : Alert.ActionStyle.Default,
        },
        dismissAction: { title: "Cancel", style: Alert.ActionStyle.Cancel },
      });
      if (!confirmed) return;
      const toast = await showCompactToast(Toast.Style.Animated, "Queueing playlist updates");
      const results = await queuePlaylistBatch(entries, queuePlaylistUpdate, (result) => {
        setEntries((current) => current.map((entry) => (entry.playlist.id === result.playlist.id ? result : entry)));
      });
      const failures = results.filter((entry) => entry.queueError).length;
      updateCompactToast(
        toast,
        failures ? Toast.Style.Failure : Toast.Style.Success,
        failures ? "Some updates could not be queued" : "Playlist updates queued",
        `${results.length - failures} queued · ${failures} failed`,
      );
    } finally {
      busy.current = false;
      setQueueing(false);
    }
  }

  const actions = (
    <ActionPanel>
      {!loading && !queueing && pending.length ? (
        <Action title="Accept All Updates" icon={Icon.Checkmark} onAction={acceptAll} />
      ) : null}
      {!loading && !queueing ? (
        <Action
          title="Refresh All Previews"
          icon={Icon.ArrowClockwise}
          onAction={() => {
            if (!busy.current) setRevision((value) => value + 1);
          }}
        />
      ) : null}
      <Action
        title="Open Review Activity"
        icon={Icon.List}
        onAction={() => launchCommand({ name: "import-activity-problems", type: LaunchType.UserInitiated })}
      />
    </ActionPanel>
  );

  return (
    <List isLoading={loading || queueing} navigationTitle="Update All · Preview">
      <List.Section title="All Playlist Updates">
        <List.Item
          title={
            loading
              ? "Checking playlists…"
              : queueing
                ? "Queueing updates…"
                : pending.length
                  ? "Accept All Updates"
                  : queued
                    ? "Updates queued"
                    : "No updates to accept"
          }
          subtitle={
            loading
              ? `${completed} of ${total} checked`
              : `${pending.length} ready · ${queued} queued · ${skipped} skipped`
          }
          icon={Icon.ArrowClockwise}
          actions={actions}
        />
        {error ? (
          <List.Item
            title="Could not load playlists"
            subtitle={compactText(error, 220)}
            icon={Icon.ExclamationMark}
            actions={actions}
          />
        ) : null}
        {!loading && !error && !entries.length ? (
          <List.Item
            title="No saved playlists"
            subtitle="Import a playlist first."
            icon={Icon.List}
            actions={actions}
          />
        ) : null}
      </List.Section>
      {entries.map(({ playlist, preview, error: previewError, job, queueError }) => (
        <List.Section key={playlist.id} title={playlist.name}>
          <List.Item
            title={
              job
                ? "Update queued"
                : previewError
                  ? "Could not preview update"
                  : preview?.blocked
                    ? "Update blocked"
                    : preview?.up_to_date
                      ? "Up to date"
                      : "Update ready"
            }
            subtitle={
              queueError
                ? compactText(queueError, 220)
                : previewError
                  ? compactText(previewError, 220)
                  : preview
                    ? `${preview.additions.length} additions · ${preview.removals.length} removals · ${preview.reorders.length} position changes · ${preview.music_changes.length} Music.app corrections`
                    : undefined
            }
            icon={{
              source: job
                ? Icon.Checkmark
                : previewError || preview?.blocked || queueError
                  ? Icon.ExclamationMark
                  : preview?.up_to_date
                    ? Icon.Checkmark
                    : Icon.ArrowClockwise,
              tintColor:
                previewError || preview?.blocked || queueError
                  ? Color.Orange
                  : job || preview?.up_to_date
                    ? Color.Green
                    : Color.Blue,
            }}
            accessories={queueError ? [{ tag: { value: "Queue failed", color: Color.Red } }] : undefined}
            actions={actions}
          />
          {preview?.warning ? (
            <List.Item
              title={preview.incomplete_data ? "Spotify Data Incomplete" : "Update Notice"}
              subtitle={preview.warning}
              icon={{ source: Icon.ExclamationMark, tintColor: Color.Orange }}
              actions={actions}
            />
          ) : null}
          {preview?.additions.map((row) => (
            <List.Item
              key={`add-${row.position}-${row.recording_id}`}
              title={row.title}
              subtitle={row.artists}
              icon={{ source: Icon.Plus, tintColor: Color.Green }}
              accessories={[{ tag: `Add · Spotify #${row.position}` }]}
            />
          ))}
          {preview?.removals.map((row) => (
            <List.Item
              key={`remove-${row.saved_position}-${row.recording_id}`}
              title={row.title}
              subtitle={row.artists}
              icon={{
                source: row.action === "delete" ? Icon.Trash : Icon.Minus,
                tintColor: row.action === "delete" ? Color.Red : Color.Orange,
              }}
              accessories={[
                { tag: { value: removalLabel(row), color: row.action === "delete" ? Color.Red : Color.Orange } },
              ]}
            />
          ))}
          {preview?.reorders.map((row) => (
            <List.Item
              key={`reorder-${row.from_position}-${row.to_position}-${row.recording_id}`}
              title={row.title}
              subtitle={row.artists}
              icon={{ source: Icon.ArrowClockwise, tintColor: Color.Blue }}
              accessories={[{ tag: `Spotify #${row.from_position} → #${row.to_position}` }]}
            />
          ))}
          {preview?.music_changes.map((row, index) => (
            <List.Item
              key={`music-${index}`}
              title={row.title}
              subtitle={row.artists || "Music.app playlist entry"}
              icon={{
                source: row.kind === "remove" ? Icon.Minus : row.kind === "restore" ? Icon.Plus : Icon.ArrowClockwise,
                tintColor: row.kind === "remove" ? Color.Orange : row.kind === "restore" ? Color.Green : Color.Blue,
              }}
              accessories={[
                {
                  tag:
                    row.kind === "remove"
                      ? `Remove Music #${row.from_position} · playlist only`
                      : row.kind === "restore"
                        ? `Restore #${row.to_position}`
                        : `Music #${row.from_position} → #${row.to_position}`,
                },
              ]}
            />
          ))}
        </List.Section>
      ))}
    </List>
  );
}
