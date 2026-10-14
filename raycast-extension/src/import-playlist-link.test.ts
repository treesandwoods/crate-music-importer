import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { buildSync } from "esbuild";
import { createElement, type ComponentType, type ElementType } from "react";
import { act, create } from "react-test-renderer";

import type { SavedPlaylistSummary } from "./types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const source = readFileSync(resolve("src/import-playlist-link.tsx"), "utf8")
  .replace(
    /import\s*{[^}]*}\s*from "@raycast\/api";/s,
    `
    const host = (name) => (props) => require("react").createElement(name, props, props.children);
    const Action = Object.assign(host("Action"), { Push: host("Push"), OpenInBrowser: host("OpenInBrowser") });
    const ActionPanel = host("ActionPanel");
    const Alert = { ActionStyle: { Destructive: "destructive", Default: "default", Cancel: "cancel" } };
    const Clipboard = { readText: async () => "" };
    const Color = new Proxy({}, { get: (_, key) => key });
    const confirmAlert = (...args) => harness.confirm(...args);
    const Form = Object.assign(host("Form"), { TextField: host("TextField"), Description: host("Description") });
    const environment = { supportPath: "/tmp/cmi-artwork-test" };
    const Icon = new Proxy({}, { get: (_, key) => key });
    const List = Object.assign(host("List"), { Item: host("Item"), Section: host("Section"), EmptyView: host("EmptyView") });
    const Toast = { Style: { Animated: "animated", Success: "success", Failure: "failure" } };
  `,
  )
  .replace(/import .* from "node:path";/, 'const join = require("node:path").join;')
  .replace(/import .* from "\.\/playlist-artwork";/, "const squarePlaylistArtwork = harness.squareArtwork;")
  .replace(
    /import\s*{[^}]*}\s*from "\.\/backend";/s,
    "const { changePlaylistLink, loadSavedPlaylists, previewPlaylistUpdate, queuePlaylistUpdate, refreshPlaylistArtwork } = harness.backend;",
  )
  .replace(/import .* from "\.\/notification-model";/, "const compactText = (value) => value;")
  .replace(
    /import .* from "\.\/notifications";/,
    "const showCompactToast = harness.showToast, updateCompactToast = harness.updateToast;",
  )
  .replace(
    /import .* from "\.\/playlist-update-model";/,
    "const canQueuePlaylistUpdate = () => true, playlistUpdateConfirmation = () => '', removalLabel = () => '';",
  )
  .replace(/import .* from "\.\/source-preview";/, 'const SourcePreviewView = "SourcePreviewView";')
  .replace(
    /import .* from "\.\/playlist-batch-update";/,
    'const PlaylistBatchUpdatePreviewView = "PlaylistBatchUpdatePreviewView";',
  );
const bundled = buildSync({
  stdin: { contents: source, loader: "tsx", resolveDir: resolve("src") },
  bundle: true,
  jsx: "automatic",
  write: false,
  platform: "node",
  format: "cjs",
  external: ["react", "react/jsx-runtime"],
}).outputFiles[0].text;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => {
    resolve = yes;
  });
  return { promise, resolve };
}

function playlist(link: string, cover: string): SavedPlaylistSummary {
  return {
    id: "playlist-1",
    name: "Saved Playlist",
    track_count: 2,
    spotify_url: link,
    cover_url: cover,
  };
}

interface TestAction {
  props: Record<string, unknown>;
}

function findAction(node: unknown, title: string): TestAction | undefined {
  if (!node || typeof node !== "object") return undefined;
  const element = node as { props?: Record<string, unknown> };
  if (element.props?.title === title) return element as TestAction;
  const children = element.props?.children;
  if (Array.isArray(children)) {
    for (const child of children) {
      const action = findAction(child, title);
      if (action) return action;
    }
  } else {
    return findAction(children, title);
  }
  return undefined;
}

test("saved playlists render from cached rows while artwork discovery runs, and stale-link artwork is ignored", async () => {
  const oldLink = "https://open.spotify.com/playlist/oldplaylist000000";
  const newLink = "https://open.spotify.com/playlist/newplaylist000000";
  const oldCover = "https://images.example.test/old.png";
  const newCover = "https://images.example.test/new.png";
  const refreshedCover = "https://images.example.test/refreshed.png";
  const cachedRows = [deferred<SavedPlaylistSummary[]>(), deferred<SavedPlaylistSummary[]>()];
  let cachedCalls = 0;
  const freshRows = [deferred<SavedPlaylistSummary[]>(), deferred<SavedPlaylistSummary[]>()];
  let freshCalls = 0;
  const staleRefresh = deferred<{ playlist_id: string; cover_url: string; spotify_url: string }>();
  const currentRefresh = deferred<{ playlist_id: string; cover_url: string; spotify_url: string }>();
  let artworkRefreshCalls = 0;
  const loadSignals: AbortSignal[] = [];
  const toastUpdates: unknown[][] = [];
  const module = { exports: {} as { default: ComponentType } };
  new Function("require", "module", "exports", "harness", bundled)(
    createRequire(resolve("package.json")),
    module,
    module.exports,
    {
      backend: {
        changePlaylistLink: async () => ({}),
        loadSavedPlaylists: (cached: boolean, signal?: AbortSignal) => {
          if (cached) return cachedRows[cachedCalls++].promise;
          if (signal) loadSignals.push(signal);
          return freshRows[freshCalls++].promise;
        },
        previewPlaylistUpdate: async () => ({}),
        queuePlaylistUpdate: async () => ({}),
        refreshPlaylistArtwork: () => (artworkRefreshCalls++ === 0 ? staleRefresh.promise : currentRefresh.promise),
      },
      squareArtwork: async (url: string) => `/artwork/${url.split("/").pop()}`,
      showToast: async () => ({}),
      updateToast: (...args: unknown[]) => toastUpdates.push(args),
    },
  );

  let view!: ReturnType<typeof create>;
  await act(async () => {
    view = create(createElement(module.exports.default));
  });
  try {
    await act(async () => cachedRows[0].resolve([playlist(oldLink, oldCover)]));
    await act(async () => new Promise((resolve) => setImmediate(resolve)));
    assert.equal(view.root.findByType("List" as ElementType).props.isLoading, false);
    const playlistRow = () =>
      view.root.findAllByType("Item" as ElementType).find((item) => item.props.title === "Saved Playlist")!;
    assert.equal(playlistRow().props.title, "Saved Playlist");
    const refresh = findAction(playlistRow().props.actions, "Refresh Playlist Artwork");
    assert.ok(refresh);
    await act(async () => {
      void (refresh.props.onAction as () => unknown)();
    });
    await act(async () => new Promise((resolve) => setImmediate(resolve)));

    const change = findAction(playlistRow().props.actions, "Change Stored Spotify Link");
    assert.ok(change);
    const target = change.props.target as { props?: { onChanged?: () => Promise<void> } };
    const onChanged = target.props?.onChanged;
    assert.ok(onChanged);
    let reload!: Promise<void>;
    await act(async () => {
      reload = onChanged();
    });
    await act(async () => cachedRows[1].resolve([playlist(newLink, newCover)]));
    await act(async () => reload);
    await act(async () => new Promise((resolve) => setImmediate(resolve)));
    assert.ok(loadSignals[0].aborted);

    const refreshCurrent = findAction(playlistRow().props.actions, "Refresh Playlist Artwork");
    assert.ok(refreshCurrent);
    await act(async () => {
      void (refreshCurrent.props.onAction as () => unknown)();
    });
    await act(async () => new Promise((resolve) => setImmediate(resolve)));
    await act(async () =>
      currentRefresh.resolve({ playlist_id: "playlist-1", cover_url: refreshedCover, spotify_url: newLink }),
    );
    await act(async () => new Promise((resolve) => setImmediate(resolve)));

    // The older background list response must not replace a cover refreshed while it was pending.
    await act(async () => freshRows[1].resolve([playlist(newLink, newCover)]));
    await act(async () => new Promise((resolve) => setImmediate(resolve)));
    assert.equal(playlistRow().props.icon.source, `/artwork/${refreshedCover.split("/").pop()}`);

    await act(async () =>
      staleRefresh.resolve({ playlist_id: "playlist-1", cover_url: oldCover, spotify_url: oldLink }),
    );
    await act(async () => new Promise((resolve) => setImmediate(resolve)));
    assert.equal(playlistRow().props.icon.source, `/artwork/${refreshedCover.split("/").pop()}`);
    assert.ok(toastUpdates.some((entry) => entry[2] === "Could not refresh artwork"));
  } finally {
    await act(async () => view.unmount());
  }
});

test("playlist update previews supersede older requests and repeated sync submits queue once", async () => {
  const playlistRow = playlist("https://open.spotify.com/playlist/playlist0000000000", "cover");
  const preview = (title: string) => ({
    source: {
      type: "playlist" as const,
      id: "spotify-playlist",
      name: playlistRow.name,
      url: playlistRow.spotify_url,
      saved_total: 1,
      current_total: 1,
    },
    playlist_id: playlistRow.id,
    additions: [{ position: 1, recording_id: title, title, artists: "Artist" }],
    removals: [],
    reorders: [],
    music_changes: [],
    up_to_date: false,
    incomplete_data: false,
    blocked: false,
    state: "ready" as const,
    baseline_backfilled: false,
    removals_deferred: false,
    confirmation_token: title,
  });
  const older = deferred<ReturnType<typeof preview>>();
  const newer = deferred<ReturnType<typeof preview>>();
  const initial = deferred<ReturnType<typeof preview>>();
  const confirmation = deferred<boolean>();
  const requests: Array<{ signal: AbortSignal; promise: Promise<ReturnType<typeof preview>> }> = [];
  let confirms = 0;
  let queued = 0;
  const module = { exports: {} as { PlaylistUpdatePreviewView: ComponentType<{ playlist: SavedPlaylistSummary }> } };
  new Function("require", "module", "exports", "harness", bundled)(
    createRequire(resolve("package.json")),
    module,
    module.exports,
    {
      confirm: () => {
        confirms++;
        return confirmation.promise;
      },
      backend: {
        changePlaylistLink: async () => ({}),
        loadSavedPlaylists: async () => [],
        previewPlaylistUpdate: (_id: string, signal: AbortSignal) => {
          const promise =
            requests.length === 0 ? initial.promise : requests.length === 1 ? older.promise : newer.promise;
          requests.push({ signal, promise });
          return promise;
        },
        queuePlaylistUpdate: async () => {
          queued++;
          return { source: { total: 1 } };
        },
        refreshPlaylistArtwork: async () => ({}),
      },
      squareArtwork: async () => "",
      showToast: async () => ({}),
      updateToast: () => {},
    },
  );
  let view!: ReturnType<typeof create>;
  await act(async () => {
    view = create(createElement(module.exports.PlaylistUpdatePreviewView, { playlist: playlistRow }));
  });
  try {
    const list = () => view.root.findByType("List" as ElementType);
    await act(async () => initial.resolve(preview("Initial Track")));
    const refresh = () => findAction(list().findAllByType("Item" as ElementType)[0].props.actions, "Refresh Preview");
    assert.ok(refresh());
    await act(async () => {
      void (refresh()!.props.onAction as () => unknown)();
      void (refresh()!.props.onAction as () => unknown)();
    });
    assert.equal(requests.length, 3);
    assert.ok(requests[1].signal.aborted);

    await act(async () => newer.resolve(preview("Newest Track")));
    await act(async () => older.resolve(preview("Older Track")));
    assert.ok(
      list()
        .findAllByType("Item" as ElementType)
        .some((item) => item.props.title === "Newest Track"),
    );
    assert.ok(
      !list()
        .findAllByType("Item" as ElementType)
        .some((item) => item.props.title === "Older Track"),
    );

    const submit = findAction(
      list().findAllByType("Item" as ElementType)[0].props.actions,
      "Sync Playlist with Spotify",
    );
    assert.ok(submit);
    await act(async () => {
      void (submit.props.onAction as () => unknown)();
      void (submit.props.onAction as () => unknown)();
    });
    assert.equal(confirms, 1);
    await act(async () => confirmation.resolve(true));
    await act(async () => new Promise((resolve) => setImmediate(resolve)));
    assert.equal(queued, 1);
  } finally {
    await act(async () => view.unmount());
  }
});
