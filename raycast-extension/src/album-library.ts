import { SpotifyRequestError } from "./spotify-model";
import type { JsonLineSession } from "./json-line-session";
import type { AlbumLibraryStatus, SpotifyAlbumSummary, SpotifyAlbumTrack } from "./types";

type CheckResult = { status: "needs_tracks" } | (AlbumLibraryStatus & { status: "complete" | "partial" | "none" });

export interface AlbumLibraryServices {
  open(signal: AbortSignal): Promise<JsonLineSession>;
  tracks(album: SpotifyAlbumSummary, token: string, signal: AbortSignal): Promise<SpotifyAlbumTrack[]>;
}

export async function scanAlbumLibrary(
  albums: SpotifyAlbumSummary[],
  token: string,
  signal: AbortSignal,
  update: (id: string, status: AlbumLibraryStatus) => void,
  services: AlbumLibraryServices,
): Promise<void> {
  if (!albums.length || signal.aborted) return;
  let session: JsonLineSession | undefined;
  let stopped: string | undefined;
  const emit = (album: SpotifyAlbumSummary, status: AlbumLibraryStatus) => {
    if (!signal.aborted) update(album.id, status);
  };
  emit(albums[0], { status: "scanning" });
  try {
    session = await services.open(signal);
    for (const album of albums) {
      if (signal.aborted) break;
      if (stopped) {
        emit(album, { status: "error", error: stopped });
        continue;
      }
      emit(album, { status: "scanning" });
      try {
        let result = await session.request<CheckResult>(album);
        if (signal.aborted) break;
        if (result.status === "needs_tracks") {
          const tracks = await services.tracks(album, token, signal);
          if (signal.aborted) break;
          result = await session.request<CheckResult>({ ...album, tracks });
        }
        if (result.status === "needs_tracks") throw new Error("The album checker did not finish checking tracks.");
        emit(album, result);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        emit(album, { status: "error", error: message });
        // Avoid repeated requests when Spotify rejects this token or rate limits us.
        if (error instanceof SpotifyRequestError && [401, 403, 429].includes(error.status)) stopped = message;
      }
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    for (const album of albums) emit(album, { status: "error", error: message });
  } finally {
    session?.close();
  }
}
