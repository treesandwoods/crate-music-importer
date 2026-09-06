import type { SpotifyAlbumSummary } from "./types";

export interface StoredSpotifyTokens {
  accessToken: string;
  refreshToken?: string;
  isExpired(): boolean;
}

export interface SpotifyAccessTokenLifecycle {
  getTokens(): Promise<StoredSpotifyTokens | undefined>;
  removeTokens(): Promise<void>;
  refresh(refreshToken: string): Promise<string>;
  connect(): Promise<string>;
}

export async function acquireSpotifyAccessToken(lifecycle: SpotifyAccessTokenLifecycle): Promise<string> {
  const tokens = await lifecycle.getTokens();
  if (!tokens?.accessToken) return lifecycle.connect();
  if (!tokens.isExpired()) return tokens.accessToken;

  if (tokens.refreshToken) {
    try {
      return await lifecycle.refresh(tokens.refreshToken);
    } catch {
      await lifecycle.removeTokens();
      return lifecycle.connect();
    }
  }

  await lifecycle.removeTokens();
  return lifecycle.connect();
}

export function authorizationCodeParameters(input: {
  clientId: string;
  code: string;
  redirectUri: string;
  codeVerifier: string;
}): URLSearchParams {
  return new URLSearchParams({
    client_id: input.clientId,
    grant_type: "authorization_code",
    code: input.code,
    redirect_uri: input.redirectUri,
    code_verifier: input.codeVerifier,
  });
}

export function refreshTokenParameters(clientId: string, refreshToken: string): URLSearchParams {
  return new URLSearchParams({
    client_id: clientId,
    grant_type: "refresh_token",
    refresh_token: refreshToken,
  });
}

export function spotifySearchParameters(query: string): URLSearchParams {
  return new URLSearchParams({
    q: query.trim(),
    type: "album",
    limit: "10",
  });
}

function artists(value: unknown): string {
  if (!Array.isArray(value)) return "";
  return value
    .map((artist) => (artist && typeof artist === "object" && "name" in artist ? String(artist.name || "") : ""))
    .filter(Boolean)
    .join(", ");
}

function albumSummary(value: unknown): SpotifyAlbumSummary | null {
  if (!value || typeof value !== "object") return null;
  const album = value as Record<string, unknown>;
  const id = String(album.id || "");
  const name = String(album.name || "");
  if (!id || !name) return null;
  const external = (album.external_urls || {}) as Record<string, unknown>;
  const images = Array.isArray(album.images) ? (album.images as Array<Record<string, unknown>>) : [];
  return {
    id,
    name,
    artists: artists(album.artists),
    url: String(external.spotify || `https://open.spotify.com/album/${id}`),
    imageUrl: images.length ? String(images[0].url || "") : undefined,
    releaseDate: album.release_date ? String(album.release_date) : undefined,
    totalTracks: Number(album.total_tracks || 0),
    albumType: album.album_type ? String(album.album_type) : undefined,
  };
}

export function parseSpotifyAlbums(value: Record<string, unknown>): SpotifyAlbumSummary[] {
  const albums = (value.albums || {}) as Record<string, unknown>;
  const items = Array.isArray(albums.items) ? albums.items : [];
  return items.map(albumSummary).filter((album): album is SpotifyAlbumSummary => album !== null);
}

export function spotifyApiError(status: number, retryAfter?: string | null): string {
  if (status === 401) return "Spotify sign-in expired. Reopen to reconnect.";
  if (status === 403) return "Spotify denied album search for this developer app.";
  if (status === 429) {
    const seconds = Number.parseInt(String(retryAfter || ""), 10);
    return Number.isFinite(seconds) && seconds > 0
      ? `Spotify limit reached. Retry in ${seconds}s.`
      : "Spotify search limit reached. Try again shortly.";
  }
  return `Spotify search returned HTTP ${status}.`;
}
