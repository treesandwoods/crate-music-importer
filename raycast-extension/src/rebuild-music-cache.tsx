import { Action, ActionPanel, Alert, confirmAlert, Detail, Icon, Toast } from "@raycast/api";
import { useState } from "react";

import { rebuildMusicCache } from "./backend";
import { compactText, compactToast } from "./notification-model";
import { showCompactToast } from "./notifications";
import type { MusicCacheRebuildResult } from "./types";

export default function Command() {
  const [isLoading, setIsLoading] = useState(false);
  const [result, setResult] = useState<MusicCacheRebuildResult>();
  const [error, setError] = useState<string>();

  async function rebuild() {
    const confirmed = await confirmAlert({
      title: "Rebuild the Music library cache?",
      message:
        "This performs one read-only scan of your complete Music library. It does not download media, edit tracks or playlists, access the iPod, or change Finder sync settings.",
      primaryAction: { title: "Run Read-Only Scan" },
      dismissAction: { title: "Cancel", style: Alert.ActionStyle.Cancel },
    });
    if (!confirmed) return;

    setIsLoading(true);
    setError(undefined);
    const toast = await showCompactToast(
      Toast.Style.Animated,
      "Scanning Music read-only",
      "Keeping the previous cache until this finishes",
    );
    try {
      const value = await rebuildMusicCache();
      setResult(value);
      const message = `${value.cached_tracks} tracks · ${value.elapsed_seconds.toFixed(1)} seconds`;
      const compact = compactToast("Music cache rebuilt", message);
      toast.style = Toast.Style.Success;
      toast.title = compact.title;
      toast.message = compact.message;
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : String(caught);
      setError(message);
      const compact = compactToast("Cache rebuild failed", message);
      toast.style = Toast.Style.Failure;
      toast.title = compact.title;
      toast.message = compact.message;
    } finally {
      setIsLoading(false);
    }
  }

  const markdown = result
    ? `# Music cache ready\n\n**${result.cached_tracks} tracks** were cached in **${result.elapsed_seconds.toFixed(1)} seconds**.\n\nNormal album and playlist previews now read this local index instead of scanning Music.\n\nCache: \`${result.cache_path}\``
    : error
      ? `# Cache rebuild failed\n\n${compactText(error, 500)}\n\nThe previous valid cache was preserved. You can fix the reported problem and retry.`
      : "# Rebuild Music Library Cache\n\nCreate or refresh the local index used by fast album and playlist previews.\n\nThis deliberately reads the complete Music library once. It does **not** download anything, change Music, touch the iPod, or alter Finder sync settings. The existing cache remains in place unless the scan completes successfully.\n\nRun this after adding, deleting, moving, or retagging tracks outside the importer.";

  return (
    <Detail
      isLoading={isLoading}
      navigationTitle="Rebuild Music Library Cache"
      markdown={markdown}
      actions={
        <ActionPanel>
          <Action title={result ? "Rebuild Again" : "Run Read-Only Scan"} icon={Icon.HardDrive} onAction={rebuild} />
        </ActionPanel>
      }
    />
  );
}
