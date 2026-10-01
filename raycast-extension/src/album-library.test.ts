import assert from "node:assert/strict";
import test from "node:test";
import { scanAlbumLibrary, type AlbumLibraryServices } from "./album-library";
import { SpotifyRequestError } from "./spotify-model";
import type { AlbumLibraryStatus, SpotifyAlbumSummary } from "./types";

const albums: SpotifyAlbumSummary[] = ["first", "second", "third"].map((id) => ({
  id,
  name: id,
  artists: "Artist",
  url: "https://example.test",
  totalTracks: 2,
}));

function services(check: (album: SpotifyAlbumSummary & { tracks?: unknown[] }) => Promise<unknown>) {
  let closed = false;
  const fetched: string[] = [];
  const value: AlbumLibraryServices = {
    open: async () => ({
      read: async <T>() => ({ ready: true }) as T,
      request: async <T>(album: unknown) => (await check(album as SpotifyAlbumSummary)) as T,
      close: () => {
        closed = true;
      },
    }),
    tracks: async (album) => {
      fetched.push(album.id);
      return [];
    },
  };
  return { value, fetched, closed: () => closed };
}

test("albums check serially in Spotify order and absent albums skip track requests", async () => {
  const requests: string[] = [];
  const io = services(async (album) => {
    requests.push(`${album.id}:${album.tracks ? "tracks" : "summary"}`);
    if (album.id === "first" && !album.tracks) return { status: "needs_tracks" };
    return { status: album.id === "first" ? "partial" : "none", matched: album.id === "first" ? 1 : 0, total: 2 };
  });
  const updates: Array<[string, string]> = [];
  await scanAlbumLibrary(
    albums,
    "token",
    new AbortController().signal,
    (id, result) => updates.push([id, result.status]),
    io.value,
  );
  assert.deepEqual(requests, ["first:summary", "first:tracks", "second:summary", "third:summary"]);
  assert.deepEqual(io.fetched, ["first"]);
  assert.deepEqual(
    updates.filter(([, status]) => status !== "scanning"),
    [
      ["first", "partial"],
      ["second", "none"],
      ["third", "none"],
    ],
  );
  assert.ok(io.closed());
});

test("a pending album never starts until the preceding album finishes and abort stops the queue", async () => {
  let finish!: (value: unknown) => void;
  const response = new Promise((resolve) => {
    finish = resolve;
  });
  const requests: string[] = [];
  const io = services(async (album) => {
    requests.push(album.id);
    return response;
  });
  const controller = new AbortController();
  const updates: string[] = [];
  const work = scanAlbumLibrary(
    albums,
    "token",
    controller.signal,
    (id, result) => updates.push(`${id}:${result.status}`),
    io.value,
  );
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(requests, ["first"]);
  assert.ok(updates.every((item) => item === "first:scanning"));
  controller.abort();
  finish({ status: "complete", matched: 2, total: 2 });
  await work;
  assert.deepEqual(requests, ["first"]);
  assert.ok(updates.every((item) => item === "first:scanning"));
  assert.ok(io.closed());
});

test("cache failures are unknown for every row and Spotify throttling stops further API requests", async () => {
  const updates: AlbumLibraryStatus[] = [];
  const io = services(async () => ({ status: "needs_tracks" }));
  io.value.open = async () => {
    throw new Error("Refresh Library Health");
  };
  await scanAlbumLibrary(albums, "token", new AbortController().signal, (_, result) => updates.push(result), io.value);
  assert.equal(updates.filter((item) => item.status === "error").length, 3);
  assert.deepEqual(io.fetched, []);

  const throttled = services(async () => ({ status: "needs_tracks" }));
  let calls = 0;
  throttled.value.tracks = async () => {
    calls++;
    throw new SpotifyRequestError(429, "Retry in 30s");
  };
  updates.length = 0;
  await scanAlbumLibrary(
    albums,
    "token",
    new AbortController().signal,
    (_, result) => updates.push(result),
    throttled.value,
  );
  assert.equal(calls, 1);
  assert.equal(updates.filter((item) => item.status === "error").length, 3);
  assert.ok(throttled.closed());
});

test("an individual album failure does not misclassify it or block later albums", async () => {
  const io = services(async (album) => {
    if (album.id === "first") throw new Error("Incomplete tracks");
    return { status: "complete", matched: 2, total: 2 };
  });
  const updates: string[] = [];
  await scanAlbumLibrary(
    albums,
    "token",
    new AbortController().signal,
    (id, result) => updates.push(`${id}:${result.status}`),
    io.value,
  );
  assert.deepEqual(
    updates.filter((item) => !item.endsWith(":scanning")),
    ["first:error", "second:complete", "third:complete"],
  );
});
