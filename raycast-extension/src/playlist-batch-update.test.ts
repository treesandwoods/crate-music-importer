import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import test from "node:test";
import { buildSync } from "esbuild";
import { createElement, type ComponentType, type ElementType } from "react";
import { act, create } from "react-test-renderer";

import { loadPlaylistBatch, pendingPlaylistUpdates, queuePlaylistBatch } from "./playlist-batch-update-model";
import type { ImportJob, PlaylistUpdatePreview, SavedPlaylistSummary } from "./types";

function playlist(id: string): SavedPlaylistSummary {
  return { id, name: id, track_count: 1, spotify_url: `https://open.spotify.com/playlist/${id}` };
}

function preview(id: string, overrides: Partial<PlaylistUpdatePreview> = {}): PlaylistUpdatePreview {
  return {
    playlist_id: id,
    source: { type: "playlist", id, name: id, url: playlist(id).spotify_url, saved_total: 1, current_total: 1 },
    additions: [],
    removals: [
      {
        saved_position: 1,
        recording_id: "removed",
        spotify_id: "removed",
        title: "Removed song",
        artists: "Artist",
        action: "delete",
      },
    ],
    reorders: [],
    music_changes: [],
    up_to_date: false,
    incomplete_data: false,
    blocked: false,
    state: "ready",
    baseline_backfilled: false,
    removals_deferred: false,
    confirmation_token: `${id}-token`,
    ...overrides,
  };
}

test("all previews are checked serially and a failure does not hide later playlists", async () => {
  let active = 0;
  const progress: number[] = [];
  const checked: string[] = [];
  const entries = await loadPlaylistBatch(
    [playlist("one"), playlist("bad"), playlist("two")],
    async (id) => {
      assert.equal(active++, 0);
      checked.push(id);
      await Promise.resolve();
      active--;
      if (id === "bad") throw new Error("Spotify unavailable");
      return preview(id);
    },
    (count) => progress.push(count),
  );
  assert.deepEqual(checked, ["one", "bad", "two"]);
  assert.deepEqual(progress, [1, 2, 3]);
  assert.equal(entries[1].error, "Spotify unavailable");
  assert.deepEqual(
    pendingPlaylistUpdates(entries).map((entry) => entry.playlist.id),
    ["one", "two"],
  );
});

test("leaving a batch preview stops subsequent playlist requests", async () => {
  let cancelled = false;
  const checked: string[] = [];
  await loadPlaylistBatch(
    [playlist("one"), playlist("two")],
    async (id) => {
      checked.push(id);
      cancelled = true;
      return preview(id);
    },
    () => assert.fail("No progress should be published after cancellation"),
    () => cancelled,
  );
  assert.deepEqual(checked, ["one"]);
});

test("queue skips unsafe and current previews, preserves tokens, and retries only failed entries", async () => {
  const entries = [
    { playlist: playlist("one"), preview: preview("one") },
    { playlist: playlist("bad"), error: "Preview failed" },
    { playlist: playlist("blocked"), preview: preview("blocked", { blocked: true, state: "incomplete_data" }) },
    { playlist: playlist("current"), preview: preview("current", { up_to_date: true, state: "up_to_date" }) },
    { playlist: playlist("two"), preview: preview("two") },
  ];
  const queued: string[] = [];
  const results = await queuePlaylistBatch(
    entries,
    async (saved, plan) => {
      queued.push(saved.id);
      assert.equal(plan.confirmation_token, `${saved.id}-token`);
      if (saved.id === "one") throw new Error("Queue failed");
      return { jobId: saved.id } as ImportJob;
    },
    () => {},
  );
  assert.deepEqual(queued, ["one", "two"]);
  assert.equal(results[0].queueError, "Queue failed");
  const retry = entries.map((entry) => results.find((result) => result.playlist.id === entry.playlist.id) ?? entry);
  await queuePlaylistBatch(
    retry,
    async (saved) => {
      queued.push(saved.id);
      return { jobId: saved.id } as ImportJob;
    },
    () => {},
  );
  assert.deepEqual(queued, ["one", "two", "one"]);
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const source = readFileSync(resolve("src/playlist-batch-update.tsx"), "utf8")
  .replace(
    /import\s*{[^}]*}\s*from "@raycast\/api";/s,
    `
    const host = (name) => (props) => require("react").createElement(name, props, props.children, props.actions);
    const Action = host("Action"), ActionPanel = host("ActionPanel");
    const List = Object.assign(host("List"), { Item: host("Item"), Section: host("Section") });
    const Icon = {}, Color = {};
    const Alert = { ActionStyle: { Default: "default", Destructive: "destructive", Cancel: "cancel" } };
    const Toast = { Style: { Animated: "animated", Success: "success", Failure: "failure" } };
    const confirmAlert = harness.confirm, launchCommand = async () => {}, LaunchType = {};
  `,
  )
  .replace(
    /import\s*{[^}]*}\s*from "\.\/backend";/s,
    "const loadSavedPlaylists = harness.saved, previewPlaylistUpdate = harness.preview, queuePlaylistUpdate = harness.queue;",
  )
  .replace(
    /import .* from "\.\/notifications";/,
    "const showCompactToast = async () => ({}), updateCompactToast = () => {};",
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

test("combined preview itemizes every playlist and accepts all with one destructive confirmation", async () => {
  let accept = false;
  const confirmations: Array<{ message: string; primaryAction: { style: string } }> = [];
  const queued: string[] = [];
  const module = { exports: {} as { PlaylistBatchUpdatePreviewView: ComponentType } };
  new Function("require", "module", "exports", "harness", bundled)(
    createRequire(resolve("package.json")),
    module,
    module.exports,
    {
      saved: async () => [
        playlist("one"),
        playlist("current"),
        playlist("blocked"),
        playlist("failed"),
        playlist("two"),
      ],
      preview: async (id: string) => {
        if (id === "failed") throw new Error("Spotify unavailable");
        return preview(
          id,
          id === "current"
            ? { up_to_date: true, state: "up_to_date", removals: [] }
            : id === "blocked"
              ? { blocked: true, state: "incomplete_data", incomplete_data: true, warning: "Incomplete response" }
              : {},
        );
      },
      confirm: async (options: (typeof confirmations)[number]) => {
        confirmations.push(options);
        return accept;
      },
      queue: async (saved: SavedPlaylistSummary) => {
        queued.push(saved.id);
        return { jobId: saved.id };
      },
    },
  );
  let view!: ReturnType<typeof create>;
  await act(async () => {
    view = create(createElement(module.exports.PlaylistBatchUpdatePreviewView));
  });
  try {
    assert.equal(view.root.findByType("List" as ElementType).props.isLoading, false);
    const sections = view.root.findAllByType("Section" as ElementType);
    assert.deepEqual(
      sections.map((section) => section.props.title),
      ["All Playlist Updates", "one", "current", "blocked", "failed", "two"],
    );
    assert.ok(
      view.root
        .findAllByType("Item" as ElementType)
        .some((row) => row.props.accessories?.[0]?.tag?.value === "delete permanently"),
    );
    assert.ok(view.root.findAllByType("Item" as ElementType).some((row) => row.props.title === "Update blocked"));
    assert.ok(
      view.root.findAllByType("Item" as ElementType).some((row) => row.props.subtitle === "Spotify unavailable"),
    );
    const action = () =>
      view.root.findAllByType("Action" as ElementType).find((row) => row.props.title === "Accept All Updates")!;
    await act(async () => {
      await action().props.onAction();
    });
    assert.deepEqual(queued, []);
    accept = true;
    await act(async () => {
      await Promise.all([action().props.onAction(), action().props.onAction()]);
    });
    assert.deepEqual(queued, ["one", "two"]);
    assert.equal(confirmations.length, 2);
    assert.equal(confirmations[1].primaryAction.style, "destructive");
    assert.match(confirmations[1].message, /“one”/);
    assert.match(confirmations[1].message, /“two”/);
    assert.match(confirmations[1].message, /Removed song: delete permanently/);
    assert.match(confirmations[1].message, /3 .* playlists will be skipped/);
    assert.equal(action(), undefined);
  } finally {
    await act(async () => view.unmount());
  }
});

test("Update All sits after import in the same section and before the saved playlists", () => {
  const command = readFileSync("src/import-playlist-link.tsx", "utf8");
  const importIndex = command.lastIndexOf('title="Import a New Playlist"');
  const updateIndex = command.indexOf('title="Update All"');
  const savedIndex = command.indexOf('title="Saved Imported Playlists"');
  assert.ok(importIndex < updateIndex && updateIndex < savedIndex);
  assert.doesNotMatch(command.slice(importIndex, updateIndex), /<\/List.Section>/);
});
