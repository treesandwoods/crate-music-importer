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
import { useCallback, useEffect, useState } from "react";

import { previewSource, queueSource } from "./backend";
import { compactText } from "./notification-model";
import { showCompactToast, updateCompactToast } from "./notifications";
import type { PreviewRow, SourcePreview } from "./types";

function previewStage(row: PreviewRow): { label: string; icon: Icon; color: Color } {
  if (row.status === "not_started" || row.status === "search_required") {
    return { label: "Not started", icon: Icon.Circle, color: Color.SecondaryText };
  }
  if (row.status === "youtube_match_found") {
    return { label: "YouTube match found", icon: Icon.CheckCircle, color: Color.Blue };
  }
  if (row.status === "no_youtube_matches") {
    return { label: "No YouTube matches", icon: Icon.XMarkCircle, color: Color.Orange };
  }
  if (row.status === "matches_need_approval") {
    return { label: "YouTube matches need approval", icon: Icon.ExclamationMark, color: Color.Yellow };
  }
  if (row.status === "needs_approval" || row.status === "review_music") {
    return { label: "Possible conflict", icon: Icon.ExclamationMark, color: Color.Orange };
  }
  if (row.status === "downloaded" || row.status === "managed_existing") {
    return { label: "Previously downloaded", icon: Icon.Download, color: Color.Purple };
  }
  if (row.status === "complete") {
    return { label: "Complete", icon: Icon.CheckCircle, color: Color.Green };
  }
  if (row.status === "reused_music") {
    return {
      label: "Existing Music match",
      icon: Icon.Music,
      color: Color.Green,
    };
  }
  if (row.status === "upgrade_managed") {
    return { label: "Ready to upgrade album metadata", icon: Icon.ArrowClockwise, color: Color.Blue };
  }
  if (row.status === "artwork_update") {
    return { label: "Ready to repair album artwork", icon: Icon.Image, color: Color.Blue };
  }
  if (row.status === "failed") {
    return { label: "Failed", icon: Icon.XMarkCircle, color: Color.Red };
  }
  return { label: "Ready to download", icon: Icon.Download, color: Color.Purple };
}

function previewSummary(preview: SourcePreview): string {
  const entries = [
    ["Not started", preview.counts.not_started || preview.counts.search_required || 0],
    ["Matched", preview.counts.youtube_match_found || 0],
    ["Previously downloaded", preview.counts.downloaded || preview.counts.managed_existing || 0],
    ["Existing Music match", preview.counts.reused_music || 0],
    ["Ready to upgrade metadata", preview.counts.upgrade_managed || 0],
    ["Ready to repair artwork", preview.counts.artwork_update || 0],
    ["Complete", preview.counts.complete || 0],
    [
      "Needs approval",
      (preview.counts.matches_need_approval || 0) +
        (preview.counts.no_youtube_matches || 0) +
        (preview.counts.needs_approval || 0) +
        (preview.counts.review_music || 0),
    ],
    ["Failed", preview.counts.failed || 0],
  ].filter(([, count]) => Number(count) > 0);
  return entries.map(([label, count]) => `${label} ${count}`).join(" · ");
}

function albumLibraryStage(row: PreviewRow): { label: "In Library" | "Not in Library"; icon: Icon; color: Color } {
  return row.status === "in_library"
    ? { label: "In Library", icon: Icon.Music, color: Color.Green }
    : { label: "Not in Library", icon: Icon.Circle, color: Color.SecondaryText };
}

function albumLibrarySummary(preview: SourcePreview): string {
  return `In Library ${preview.counts.in_library || 0} · Not in Library ${preview.counts.not_in_library || 0}`;
}

export function SourcePreviewView({ type, url }: { type: "album" | "playlist"; url: string }) {
  const [preview, setPreview] = useState<SourcePreview>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setPreview(await previewSource(type, url));
      setError(undefined);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }, [type, url]);

  useEffect(() => {
    void load();
  }, [load]);

  async function startImport() {
    if (!preview || preview.source.warning) return;
    const confirmed = await confirmAlert({
      title: `Import this ${type}?`,
      message: `Queue “${preview.source.name}”. YouTube matching and downloading run in the background; saved Music IDs are checked individually immediately before the final Music changes.`,
      primaryAction: { title: "Download + Add to Music" },
      dismissAction: { title: "Cancel", style: Alert.ActionStyle.Cancel },
    });
    if (!confirmed) return;
    const toast = await showCompactToast(Toast.Style.Animated, "Queueing import");
    try {
      const job = await queueSource(type, url, {
        name: preview.source.name,
        total: preview.rows.length,
        tracks: preview.rows.map((row) => ({
          position: row.position,
          trackNumber: row.track_no || row.position,
          discNumber: row.disc_no || 1,
          recordingId: row.recording_id,
          title: row.title,
          artists: row.artists,
        })),
      });
      updateCompactToast(
        toast,
        Toast.Style.Success,
        `${type === "album" ? "Album" : "Playlist"} queued`,
        `${job.source.total || preview.rows.length} tracks`,
      );
    } catch (caught) {
      updateCompactToast(
        toast,
        Toast.Style.Failure,
        "Could not queue import",
        caught instanceof Error ? caught.message : String(caught),
      );
    }
  }

  return (
    <List
      isLoading={loading}
      isShowingDetail
      navigationTitle={preview?.source.name || `${type === "album" ? "Album" : "Playlist"} Preview`}
      searchBarPlaceholder="Filter preview tracks"
      actions={
        preview ? (
          <ActionPanel>
            {!preview.source.warning ? (
              <Action title="Download + Add to Music" icon={Icon.Download} onAction={startImport} />
            ) : null}
            <Action title="Refresh Preview" icon={Icon.ArrowClockwise} onAction={load} />
            <Action.OpenInBrowser title="Open in Spotify" url={preview.source.url || url} />
          </ActionPanel>
        ) : undefined
      }
    >
      {preview?.source.warning ? (
        <List.Section title="Spotify Data Incomplete">
          <List.Item
            title="Import blocked"
            subtitle={preview.source.warning}
            icon={{ source: Icon.ExclamationMark, tintColor: Color.Orange }}
          />
        </List.Section>
      ) : null}
      {preview ? (
        <List.Section
          title="Tracks"
          subtitle={type === "album" ? albumLibrarySummary(preview) : previewSummary(preview)}
        >
          {preview.rows.map((row) => {
            const stage = type === "album" ? albumLibraryStage(row) : previewStage(row);
            return (
              <List.Item
                key={`${row.position}-${row.recording_id}`}
                title={row.title}
                subtitle={row.artists}
                icon={{ source: stage.icon, tintColor: stage.color }}
                accessories={[{ tag: { value: stage.label, color: stage.color } }]}
                detail={
                  <List.Item.Detail
                    markdown={`# ${row.artists} — ${row.title}\n\n**${stage.label}**${type === "album" ? "" : `\n\n${row.detail}`}`}
                    metadata={
                      <List.Item.Detail.Metadata>
                        <List.Item.Detail.Metadata.Label
                          title="Position"
                          text={`${row.disc_no || 1}.${row.track_no || row.position}`}
                        />
                        <List.Item.Detail.Metadata.Label title="Status" text={stage.label} />
                      </List.Item.Detail.Metadata>
                    }
                  />
                }
                actions={
                  <ActionPanel>
                    <Action title="Download + Add to Music" icon={Icon.Download} onAction={startImport} />
                    <Action.OpenInBrowser title="Open in Spotify" url={preview.source.url || url} />
                  </ActionPanel>
                }
              />
            );
          })}
        </List.Section>
      ) : null}
      {error ? (
        <List.EmptyView
          title="Could not build preview"
          description={compactText(error, 180)}
          actions={
            <ActionPanel>
              <Action title="Try Again" icon={Icon.ArrowClockwise} onAction={load} />
              {error.includes("Music library cache") ? (
                <Action
                  title="Open Library Health & Updates"
                  icon={Icon.HardDrive}
                  onAction={() => launchCommand({ name: "update-dependencies", type: LaunchType.UserInitiated })}
                />
              ) : null}
            </ActionPanel>
          }
        />
      ) : null}
    </List>
  );
}
