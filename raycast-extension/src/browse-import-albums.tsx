import { Action, ActionPanel, Icon, List, Toast, type LaunchProps } from "@raycast/api";
import { useCallback, useEffect, useRef, useState } from "react";

import { compactText } from "./notification-model";
import { showCompactToast } from "./notifications";
import { SourcePreviewView } from "./source-preview";
import { searchSpotifyAlbums, spotifyAccessToken } from "./spotify";
import type { SpotifyAlbumSummary } from "./types";

export default function Command({ fallbackText }: LaunchProps) {
  // Mount the input immediately. Suspending the entire command for OAuth can
  // leave native startup typing visible without ever delivering it to React.
  const [query, setQuery] = useState(fallbackText || "");
  const [albums, setAlbums] = useState<SpotifyAlbumSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const [retry, setRetry] = useState(0);
  const searchRequest = useRef(0);
  const inputText = useRef(query);
  const authorization = useRef<Promise<string> | undefined>(undefined);

  const onSearchTextChange = useCallback((text: string) => {
    if (text === inputText.current) return;
    inputText.current = text;
    // Invalidate in-flight work at the input event, including the debounce gap.
    searchRequest.current += 1;
    setQuery(text);
    setAlbums([]);
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

  return (
    <List
      isLoading={loading}
      onSearchTextChange={onSearchTextChange}
      filtering={false}
      throttle={false}
      searchText={query}
      navigationTitle="Albums Browse & Import"
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
              accessories={[{ text: album.releaseDate?.slice(0, 4) || "" }, { text: `${album.totalTracks} tracks` }]}
              actions={
                <ActionPanel>
                  <Action.Push
                    title="Preview Album"
                    icon={Icon.List}
                    target={<SourcePreviewView type="album" url={album.url} />}
                  />
                  <Action.OpenInBrowser title="Open in Spotify" url={album.url} />
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
