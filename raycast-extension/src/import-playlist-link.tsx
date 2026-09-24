import {
  Action,
  ActionPanel,
  Alert,
  Clipboard,
  Color,
  confirmAlert,
  Form,
  environment,
  Icon,
  List,
  Toast,
} from "@raycast/api";
import { join } from "node:path";
import { squarePlaylistArtwork } from "./playlist-artwork";
import { useCallback, useEffect, useState } from "react";

import {
  changePlaylistLink,
  loadSavedPlaylists,
  previewPlaylistUpdate,
  queuePlaylistUpdate,
  refreshPlaylistArtwork,
} from "./backend";
import { compactText } from "./notification-model";
import { showCompactToast, updateCompactToast } from "./notifications";
import { canQueuePlaylistUpdate, playlistUpdateConfirmation, removalLabel } from "./playlist-update-model";
import { SourcePreviewView } from "./source-preview";
import type { PlaylistUpdatePreview, SavedPlaylistSummary } from "./types";

const PLAYLIST_URL = /^https?:\/\/(open|play)\.spotify\.com\/playlist\/[A-Za-z0-9]{16,32}/;

export function NewPlaylistForm() {
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string>();
  useEffect(() => {
    void Clipboard.readText().then((value) => {
      if (value?.includes("open.spotify.com/playlist/")) setUrl(value.trim());
    });
  }, []);
  const valid = PLAYLIST_URL.test(url.trim());
  return (
    <Form
      navigationTitle="Import a New Playlist"
      actions={
        <ActionPanel>
          {valid ? (
            <Action.Push
              title="Preview Playlist"
              icon={Icon.List}
              target={<SourcePreviewView type="playlist" url={url.trim()} />}
            />
          ) : null}
        </ActionPanel>
      }
    >
      <Form.TextField
        id="url"
        title="Spotify Playlist"
        placeholder="https://open.spotify.com/playlist/…"
        value={url}
        onChange={(value) => {
          setUrl(value);
          setError(value && !PLAYLIST_URL.test(value) ? "Use a public Spotify playlist link." : undefined);
        }}
        error={error}
        autoFocus
      />
      <Form.Description text="Preview loads Spotify metadata and checks the local Music cache. YouTube matching and downloading begin only after a second confirmation." />
    </Form>
  );
}

function ChangePlaylistLinkForm({ playlist, onChanged }: { playlist: SavedPlaylistSummary; onChanged: () => void }) {
  const [url, setUrl] = useState(playlist.spotify_url);
  const [saving, setSaving] = useState(false);
  const valid = PLAYLIST_URL.test(url.trim());
  async function save() {
    if (!valid) return;
    setSaving(true);
    try {
      await changePlaylistLink(playlist.id, url.trim());
      await showCompactToast(Toast.Style.Success, "Spotify link updated", playlist.name);
      onChanged();
    } catch (error) {
      await showCompactToast(
        Toast.Style.Failure,
        "Could not update link",
        error instanceof Error ? error.message : String(error),
      );
    } finally {
      setSaving(false);
    }
  }
  return (
    <Form
      isLoading={saving}
      navigationTitle={`Change Link · ${playlist.name}`}
      actions={
        <ActionPanel>
          {valid ? <Action title="Save Spotify Link" icon={Icon.SaveDocument} onAction={save} /> : null}
        </ActionPanel>
      }
    >
      <Form.TextField
        id="url"
        title="Spotify Playlist"
        value={url}
        onChange={setUrl}
        error={url && !valid ? "Use a Spotify playlist link." : undefined}
      />
      <Form.Description text="Use this when the playlist receives a new public link. Its saved import history and Music playlist identity stay unchanged." />
    </Form>
  );
}

export function PlaylistUpdatePreviewView({
  playlist,
  onArtworkChanged,
}: {
  playlist: SavedPlaylistSummary;
  onArtworkChanged?: () => void;
}) {
  const [preview, setPreview] = useState<PlaylistUpdatePreview>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await previewPlaylistUpdate(playlist.id);
      setPreview(next);
      if (next.source.cover_url && next.source.cover_url !== playlist.cover_url) onArtworkChanged?.();
      setError(undefined);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }, [playlist.id, playlist.cover_url, onArtworkChanged]);
  useEffect(() => void load(), [load]);

  async function queue() {
    if (!preview || preview.blocked || preview.up_to_date) return;
    const confirmed = await confirmAlert({
      title: `Update “${playlist.name}”?`,
      message: playlistUpdateConfirmation(preview),
      primaryAction: {
        title: preview.removals.some((row) => row.action === "delete")
          ? "Confirm Update and Permanent Deletions"
          : "Confirm Update",
        style: preview.removals.some((row) => row.action === "delete")
          ? Alert.ActionStyle.Destructive
          : Alert.ActionStyle.Default,
      },
      dismissAction: { title: "Cancel", style: Alert.ActionStyle.Cancel },
    });
    if (!confirmed) return;
    const toast = await showCompactToast(Toast.Style.Animated, "Queueing playlist update");
    try {
      const job = await queuePlaylistUpdate(playlist, preview);
      updateCompactToast(toast, Toast.Style.Success, "Playlist update queued", `${job.source.total} changes`);
    } catch (caught) {
      updateCompactToast(
        toast,
        Toast.Style.Failure,
        "Could not queue update",
        caught instanceof Error ? caught.message : String(caught),
      );
    }
  }

  const primaryActionTitle = "Sync Playlist with Spotify";
  const primaryActionIcon = Icon.ArrowClockwise;
  const utilityActions = (
    <ActionPanel>
      <Action title="Refresh Preview" icon={Icon.ArrowClockwise} onAction={load} />
      <Action.Push
        title="Change Stored Spotify Link"
        icon={Icon.Link}
        target={<ChangePlaylistLinkForm playlist={playlist} onChanged={load} />}
      />
      {playlist.spotify_url ? <Action.OpenInBrowser title="Open in Spotify" url={playlist.spotify_url} /> : null}
    </ActionPanel>
  );
  const queueActions = (
    <ActionPanel>
      {preview && canQueuePlaylistUpdate(preview) ? (
        <Action title={primaryActionTitle} icon={primaryActionIcon} onAction={queue} />
      ) : null}
      <Action title="Refresh Preview" icon={Icon.ArrowClockwise} onAction={load} />
      <Action.Push
        title="Change Stored Spotify Link"
        icon={Icon.Link}
        target={<ChangePlaylistLinkForm playlist={playlist} onChanged={load} />}
      />
      {playlist.spotify_url ? <Action.OpenInBrowser title="Open in Spotify" url={playlist.spotify_url} /> : null}
    </ActionPanel>
  );
  return (
    <List isLoading={loading} navigationTitle={`${playlist.name} · Update Preview`}>
      {preview?.warning ? (
        <List.Section title={preview.incomplete_data ? "Spotify Data Incomplete" : "Update Notice"}>
          <List.Item
            title={preview.blocked ? "Update blocked" : "Removals deferred"}
            subtitle={preview.warning}
            icon={{ source: Icon.ExclamationMark, tintColor: Color.Orange }}
            actions={utilityActions}
          />
        </List.Section>
      ) : null}
      {preview?.up_to_date ? (
        <List.EmptyView
          title="Playlist is up to date"
          description="Spotify order and the Music.app playlist membership match exactly."
          actions={utilityActions}
        />
      ) : null}
      {preview && canQueuePlaylistUpdate(preview) ? (
        <List.Section title="Playlist Update">
          <List.Item
            title={primaryActionTitle}
            subtitle={`${preview.additions.length} additions · ${preview.removals.length} removals · ${preview.reorders.length} position changes · ${preview.music_changes.length} Music.app corrections`}
            icon={{ source: primaryActionIcon, tintColor: Color.Green }}
            actions={queueActions}
          />
        </List.Section>
      ) : null}
      {preview?.additions.length ? (
        <List.Section title="Additions" subtitle={`${preview.additions.length} · Spotify order`}>
          {preview.additions.map((row) => (
            <List.Item
              key={`add-${row.position}-${row.recording_id}`}
              title={row.title}
              subtitle={row.artists}
              icon={{ source: Icon.Plus, tintColor: Color.Green }}
              accessories={[{ tag: `Spotify #${row.position}` }]}
            />
          ))}
        </List.Section>
      ) : null}
      {preview?.removals.length ? (
        <List.Section title="Removals" subtitle={`${preview.removals.length} · removed from Spotify`}>
          {preview.removals.map((row) => (
            <List.Item
              key={`remove-${row.saved_position}-${row.recording_id}`}
              title={row.title}
              subtitle={row.artists}
              icon={{
                source: row.action === "delete" ? Icon.Trash : Icon.Minus,
                tintColor: row.action === "delete" ? Color.Red : Color.Orange,
              }}
              accessories={[
                {
                  tag: {
                    value: removalLabel(row),
                    color: row.action === "delete" ? Color.Red : Color.Orange,
                  },
                },
              ]}
            />
          ))}
        </List.Section>
      ) : null}
      {preview?.reorders.length ? (
        <List.Section title="Spotify Position Changes" subtitle={`${preview.reorders.length} · exact occurrence order`}>
          {preview.reorders.map((row) => (
            <List.Item
              key={`reorder-${row.from_position}-${row.to_position}-${row.recording_id}`}
              title={row.title}
              subtitle={row.artists}
              icon={{ source: Icon.ArrowClockwise, tintColor: Color.Blue }}
              accessories={[{ tag: `#${row.from_position} → #${row.to_position}` }]}
            />
          ))}
        </List.Section>
      ) : null}
      {preview?.music_changes.length ? (
        <List.Section
          title="Music.app Corrections"
          subtitle={`${preview.music_changes.length} · current playlist differs from Spotify`}
        >
          {preview.music_changes.map((row, index) => (
            <List.Item
              key={`music-${row.kind}-${row.persistent_id}-${row.from_position ?? "missing"}-${row.to_position ?? index}`}
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
                      ? `remove Music #${row.from_position}`
                      : row.kind === "restore"
                        ? `missing from saved #${row.to_position}`
                        : `Music #${row.from_position} · saved #${row.to_position}`,
                },
              ]}
            />
          ))}
        </List.Section>
      ) : null}
      {error ? (
        <List.EmptyView
          title="Could not preview update"
          description={compactText(error, 220)}
          actions={utilityActions}
        />
      ) : null}
    </List>
  );
}

export default function Command() {
  const [artwork, setArtwork] = useState<Record<string, string>>({});
  const [playlists, setPlaylists] = useState<SavedPlaylistSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const saved = await loadSavedPlaylists();
      setPlaylists(saved);
      await Promise.all(
        saved.map(async (playlist) => {
          if (!playlist.cover_url) return;
          try {
            const path = await squarePlaylistArtwork(
              playlist.cover_url,
              join(environment.supportPath, "playlist-artwork-v1"),
            );
            setArtwork((current) => ({ ...current, [playlist.cover_url!]: path }));
          } catch (error) {
            console.error("Could not crop playlist artwork", error);
          }
        }),
      );
      setError(undefined);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => void load(), [load]);

  async function refreshArtwork(playlist: SavedPlaylistSummary) {
    const toast = await showCompactToast(Toast.Style.Animated, "Refreshing playlist artwork", playlist.name);
    try {
      const result = await refreshPlaylistArtwork(playlist.id);
      const path = await squarePlaylistArtwork(
        result.cover_url,
        join(environment.supportPath, "playlist-artwork-v1"),
        true,
      );
      setArtwork((current) => ({ ...current, [result.cover_url]: path }));
      setPlaylists((current) =>
        current.map((row) => (row.id === playlist.id ? { ...row, cover_url: result.cover_url } : row)),
      );
      updateCompactToast(toast, Toast.Style.Success, "Playlist artwork refreshed", playlist.name);
    } catch (caught) {
      updateCompactToast(
        toast,
        Toast.Style.Failure,
        "Could not refresh artwork",
        caught instanceof Error ? caught.message : String(caught),
      );
    }
  }
  return (
    <List isLoading={loading} navigationTitle="Playlists" searchBarPlaceholder="Filter saved playlists">
      <List.Section>
        <List.Item
          title="Import a New Playlist"
          subtitle="Paste a public Spotify playlist link"
          icon={Icon.Plus}
          actions={
            <ActionPanel>
              <Action.Push title="Open Import Page" icon={Icon.Plus} target={<NewPlaylistForm />} />
            </ActionPanel>
          }
        />
      </List.Section>
      <List.Section title="Saved Imported Playlists" subtitle={`${playlists.length}`}>
        {playlists.map((playlist) => (
          <List.Item
            key={playlist.id}
            title={playlist.name}
            subtitle={`${playlist.track_count} saved tracks`}
            icon={
              playlist.cover_url && artwork[playlist.cover_url] ? { source: artwork[playlist.cover_url] } : Icon.List
            }
            accessories={[{ text: `${playlist.track_count} tracks` }]}
            actions={
              <ActionPanel>
                <Action.Push
                  title="Preview Update"
                  icon={Icon.ArrowClockwise}
                  target={<PlaylistUpdatePreviewView playlist={playlist} onArtworkChanged={load} />}
                />
                <Action title="Refresh Playlist Artwork" icon={Icon.Image} onAction={() => refreshArtwork(playlist)} />
                <Action.Push
                  title="Change Stored Spotify Link"
                  icon={Icon.Link}
                  target={<ChangePlaylistLinkForm playlist={playlist} onChanged={load} />}
                />
                {playlist.spotify_url ? (
                  <Action.OpenInBrowser title="Open in Spotify" url={playlist.spotify_url} />
                ) : null}
              </ActionPanel>
            }
          />
        ))}
      </List.Section>
      {error ? (
        <List.EmptyView
          title="Could not load saved playlists"
          description={compactText(error, 180)}
          actions={
            <ActionPanel>
              <Action title="Try Again" icon={Icon.ArrowClockwise} onAction={load} />
            </ActionPanel>
          }
        />
      ) : null}
    </List>
  );
}
