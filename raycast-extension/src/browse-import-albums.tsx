import { Action, ActionPanel, Color, Icon, List, Toast, type LaunchProps } from "@raycast/api";
import { useCallback, useEffect, useRef, useState } from "react";

import { compactText } from "./notification-model";
import { scanAlbumLibrary } from "./album-library";
import { openAlbumLibraryChecker } from "./backend";
import { showCompactToast } from "./notifications";
import { SourcePreviewView } from "./source-preview";
import { fetchSpotifyAlbumTracks, searchSpotifyAlbums, spotifyAccessToken } from "./spotify";
import type { AlbumLibraryStatus, SpotifyAlbumSummary } from "./types";

function libraryAccessory(status: AlbumLibraryStatus | undefined, frame: number): List.Item.Accessory {
  const count = `${status?.matched || 0} of ${status?.total || 0} tracks in this album`;
  const stage = status?.status || "pending";
  const indicators = {
    pending: { source: Icon.Minus, tintColor: Color.SecondaryText, tooltip: "Waiting to check the Music cache" },
    scanning: {
      source: [Icon.CircleProgress25, Icon.CircleProgress50, Icon.CircleProgress75][frame],
      tintColor: Color.SecondaryText,
      tooltip: "Checking the Music cache…",
    },
    complete: { source: Icon.CheckCircle, tintColor: Color.Green, tooltip: `In Library — ${count}` },
    partial: { source: Icon.CircleProgress50, tintColor: Color.Orange, tooltip: `Partly in Library — ${count}` },
    none: {
      source: Icon.Xmark,
      tintColor: Color.SecondaryText,
      tooltip: "No tracks from this album in the Music cache",
    },
    error: {
      source: Icon.QuestionMarkCircle,
      tintColor: Color.Orange,
      tooltip: status?.error || "Library status unavailable. Retry the library checks.",
    },
  };
  const { source, tintColor, tooltip } = indicators[stage];
  // An icon-only final accessory reserves the same native slot for every state.
  // No changing label can shift the status away from the right edge.
  return { icon: { source, tintColor }, tooltip };
}

export default function Command({ fallbackText }: LaunchProps) {
  // Mount the input immediately. Suspending the entire command for OAuth can
  // leave native startup typing visible without ever delivering it to React.
  const [query, setQuery] = useState(fallbackText || "");
  const [albums, setAlbums] = useState<SpotifyAlbumSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const [retry, setRetry] = useState(0);
  const [libraryStatuses, setLibraryStatuses] = useState<Record<string, AlbumLibraryStatus>>({});
  const [scanRetry, setScanRetry] = useState(0);
  const [spinnerFrame, setSpinnerFrame] = useState(0);
  const scanController = useRef<AbortController | undefined>(undefined);
  const albumToken = useRef("");
  const searchRequest = useRef(0);
  const inputText = useRef(query);
  const authorization = useRef<Promise<string> | undefined>(undefined);

  const onSearchTextChange = useCallback((text: string) => {
    if (text === inputText.current) return;
    inputText.current = text;
    // Invalidate in-flight work at the input event, including the debounce gap.
    searchRequest.current += 1;
    scanController.current?.abort();
    setQuery(text);
    setAlbums([]);
    setLibraryStatuses({});
    setError(undefined);
    setLoading(text.trim().length >= 2);
  }, []);

  useEffect(() => {
    const request = ++searchRequest.current;
    const text = query.trim();
    if (text.length < 2) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    const isCurrent = () => !cancelled && request === searchRequest.current;
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        // Share pending authorization when the user keeps typing or signs in.
        // Read the stored token again for each subsequent search so expiry works.
        authorization.current ??= spotifyAccessToken().finally(() => {
          authorization.current = undefined;
        });
        const token = await authorization.current;
        if (!isCurrent()) return;
        const result = await searchSpotifyAlbums(text, token);
        if (!isCurrent()) return;
        albumToken.current = token;
        setLibraryStatuses({});
        setAlbums(result);
        setError(undefined);
      } catch (caught) {
        if (!isCurrent()) return;
        const message = caught instanceof Error ? caught.message : String(caught);
        setError(message);
        await showCompactToast(Toast.Style.Failure, "Spotify search failed", message);
      } finally {
        if (isCurrent()) setLoading(false);
      }
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query, retry]);

  useEffect(() => {
    if (!albums.length) return;
    const controller = new AbortController();
    scanController.current = controller;
    setLibraryStatuses({});
    void (async () => {
      const token = scanRetry ? await spotifyAccessToken() : albumToken.current;
      if (controller.signal.aborted) return;
      await scanAlbumLibrary(
        albums,
        token,
        controller.signal,
        (id, status) => {
          if (!controller.signal.aborted) setLibraryStatuses((current) => ({ ...current, [id]: status }));
        },
        { open: openAlbumLibraryChecker, tracks: fetchSpotifyAlbumTracks },
      );
    })().catch((caught) => {
      if (controller.signal.aborted) return;
      const error = caught instanceof Error ? caught.message : String(caught);
      setLibraryStatuses(Object.fromEntries(albums.map((album) => [album.id, { status: "error", error }])));
    });
    return () => controller.abort();
  }, [albums, scanRetry]);

  const checking = Object.values(libraryStatuses).some((status) => status.status === "scanning");
  useEffect(() => {
    if (!checking) return;
    const timer = setInterval(() => setSpinnerFrame((frame) => (frame + 1) % 3), 250);
    return () => clearInterval(timer);
  }, [checking]);

  return (
    <List
      isLoading={loading}
      onSearchTextChange={onSearchTextChange}
      filtering={false}
      throttle={false}
      searchText={query}
      navigationTitle="Albums"
      searchBarPlaceholder="Search Spotify albums"
    >
      {albums.length ? (
        <List.Section title="Spotify Albums" subtitle={`${albums.length}`}>
          {albums.map((album) => (
            <List.Item
              key={album.id}
              title={album.name}
              subtitle={album.artists}
              icon={album.imageUrl || Icon.Music}
              accessories={[
                { text: album.releaseDate?.slice(0, 4) || "" },
                { text: `${album.totalTracks} tracks` },
                libraryAccessory(libraryStatuses[album.id], spinnerFrame),
              ]}
              actions={
                <ActionPanel>
                  <Action.Push
                    title="Preview Album"
                    icon={Icon.List}
                    target={<SourcePreviewView type="album" url={album.url} />}
                  />
                  <Action.OpenInBrowser title="Open in Spotify" url={album.url} />
                  <Action
                    title="Retry Library Checks"
                    icon={Icon.ArrowClockwise}
                    onAction={() => {
                      scanController.current?.abort();
                      setScanRetry((value) => value + 1);
                    }}
                  />
                </ActionPanel>
              }
            />
          ))}
        </List.Section>
      ) : null}
      {error ? (
        <List.EmptyView
          title="Spotify search unavailable"
          description={compactText(error, 180)}
          actions={
            <ActionPanel>
              <Action title="Retry Search" icon={Icon.ArrowClockwise} onAction={() => setRetry((value) => value + 1)} />
            </ActionPanel>
          }
        />
      ) : null}
      {!error && query.trim().length < 2 ? (
        <List.EmptyView
          title="Search Spotify albums"
          description="Type an artist or album title. Spotify sign-in appears once."
          icon={Icon.MagnifyingGlass}
        />
      ) : null}
      {!error && query.trim().length >= 2 && !loading && !albums.length ? (
        <List.EmptyView title="No albums found" description="Try another artist or album title." />
      ) : null}
      {!error && query.trim().length >= 2 && loading && !albums.length ? (
        <List.EmptyView title="Searching Spotify albums" description="Searching for matching albums…" />
      ) : null}
    </List>
  );
}
