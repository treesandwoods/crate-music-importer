import { Action, ActionPanel, Alert, Clipboard, Color, confirmAlert, Form, Icon, List, Toast } from "@raycast/api";
import { useCallback, useEffect, useState } from "react";

import { changePlaylistLink, loadSavedPlaylists, previewPlaylistUpdate, queuePlaylistUpdate } from "./backend";
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

export function PlaylistUpdatePreviewView({ playlist }: { playlist: SavedPlaylistSummary }) {
  const [preview, setPreview] = useState<PlaylistUpdatePreview>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const load = useCallback(async () => {
    setLoading(true);
    try {
      setPreview(await previewPlaylistUpdate(playlist.id));
      setError(undefined);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }, [playlist.id]);
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

  const actions = (
    <ActionPanel>
      {canQueuePlaylistUpdate(preview) ? (
        <Action title="Queue Additions + Removals" icon={Icon.ArrowClockwise} onAction={queue} />
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
    <List isLoading={loading} navigationTitle={`${playlist.name} · Update Preview`} actions={actions}>
      {preview?.warning ? (
        <List.Section title={preview.incomplete_data ? "Spotify Data Incomplete" : "Update Notice"}>
          <List.Item
            title={preview.blocked ? "Update blocked" : "Removals deferred"}
            subtitle={preview.warning}
            icon={{ source: Icon.ExclamationMark, tintColor: Color.Orange }}
          />
        </List.Section>
      ) : null}
      {preview?.up_to_date ? (
        <List.EmptyView
          title="Playlist is up to date"
          description="There are no additions or removals. Reorders and moves are ignored."
          actions={actions}
        />
      ) : null}
      {preview?.additions.length ? (
        <List.Section title="Additions" subtitle={`${preview.additions.length} · appended in Spotify order`}>
          {preview.additions.map((row) => (
            <List.Item
              key={`add-${row.position}-${row.recording_id}`}
              title={row.title}
              subtitle={row.artists}
              icon={{ source: Icon.Plus, tintColor: Color.Green }}
              accessories={[{ tag: "append" }]}
            />
          ))}
        </List.Section>
      ) : null}
      {preview?.removals.length ? (
        <List.Section title="Removals" subtitle={`${preview.removals.length} · existing survivor order preserved`}>
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
      {error ? (
        <List.EmptyView title="Could not preview update" description={compactText(error, 220)} actions={actions} />
      ) : null}
    </List>
  );
}

export default function Command() {
  const [playlists, setPlaylists] = useState<SavedPlaylistSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const load = useCallback(async () => {
    setLoading(true);
    try {
      setPlaylists(await loadSavedPlaylists());
      setError(undefined);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => void load(), [load]);
  return (
    <List isLoading={loading} navigationTitle="Playlists Browse & Import" searchBarPlaceholder="Filter saved playlists">
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
            icon={Icon.List}
            accessories={[{ text: `${playlist.track_count} tracks` }]}
            actions={
              <ActionPanel>
                <Action.Push
                  title="Preview Update"
                  icon={Icon.ArrowClockwise}
                  target={<PlaylistUpdatePreviewView playlist={playlist} />}
                />
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
