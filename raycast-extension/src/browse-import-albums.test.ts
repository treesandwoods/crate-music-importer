import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { buildSync } from "esbuild";
import { createElement, type ComponentType, type ElementType } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { scanAlbumLibrary } from "./album-library";
import type { AlbumLibraryStatus } from "./types";

// Render the real command and its hooks, replacing only the native host and I/O.
// No Raycast worker, Spotify account, or Music library is needed.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const source = readFileSync(resolve("src/browse-import-albums.tsx"), "utf8")
  .replace(
    /import .* from "@raycast\/api";/,
    `
    const host = (name) => (props) => require("react").createElement(name, props, props.children, props.actions);
    const Action = Object.assign(host("Action"), { Push: "Push", OpenInBrowser: "OpenInBrowser" });
    const ActionPanel = "ActionPanel";
    const List = Object.assign(host("List"), { Item: host("Item"), Section: "Section", EmptyView: host("EmptyView") });
    const Icon = new Proxy({}, { get: (_, key) => key });
    const Color = new Proxy({}, { get: (_, key) => key });
    const Toast = { Style: { Failure: "failure" } };
  `,
  )
  .replace(/import .* from "\.\/notifications";/, "const showCompactToast = testHarness.toast;")
  .replace(/import .* from "\.\/source-preview";/, 'const SourcePreviewView = "Preview";')
  .replace(/import .* from "\.\/album-library";/, "const scanAlbumLibrary = testHarness.scan;")
  .replace(/import .* from "\.\/backend";/, "const openAlbumLibraryChecker = () => {};")
  .replace(
    /import .* from "\.\/spotify";/,
    "const spotifyAccessToken = testHarness.authorize; const searchSpotifyAlbums = testHarness.search; const fetchSpotifyAlbumTracks = () => {};",
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
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const album = (name: string) => ({ id: name, name, artists: "Artist", url: "https://example.test", totalTracks: 1 });

async function mount(
  authorize: () => Promise<string>,
  search: (query: string, token: string) => Promise<unknown[]>,
  fallbackText = "",
  scan: typeof scanAlbumLibrary = async () => {},
) {
  const toasts: unknown[][] = [];
  const module = { exports: {} as { default: ComponentType<{ fallbackText: string }> } };
  new Function("require", "module", "exports", "testHarness", bundled)(
    createRequire(resolve("package.json")),
    module,
    module.exports,
    {
      authorize,
      search,
      scan,
      toast: async (...args: unknown[]) => {
        toasts.push(args);
      },
    },
  );
  let view!: ReactTestRenderer;
  await act(async () => {
    view = create(createElement(module.exports.default, { fallbackText }));
  });
  return {
    view,
    toasts,
    input: async (text: string) => {
      await act(async () => {
        view.root.findByType("List" as ElementType).props.onSearchTextChange(text);
      });
    },
    settle: async () => {
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 450));
      });
    },
    close: async () => {
      await act(async () => view.unmount());
    },
  };
}

test("startup input mounts before OAuth and the latest text searches after delayed sign-in", async () => {
  const auth = deferred<string>();
  let authorizations = 0;
  const searches: string[] = [];
  const ui = await mount(
    () => {
      authorizations++;
      return auth.promise;
    },
    async (query) => {
      searches.push(query);
      return [album(query)];
    },
  );
  try {
    assert.equal(authorizations, 0);
    await ui.input("the last");
    await ui.settle();
    await ui.input("the last waltz");
    await ui.settle();
    await ui.input("the last waltz");
    assert.equal(ui.view.root.findByType("List" as ElementType).props.searchText, "the last waltz");
    assert.equal(authorizations, 1);
    assert.deepEqual(searches, []);
    await act(async () => auth.resolve("token"));
    assert.deepEqual(searches, ["the last waltz"]);
    assert.equal(ui.view.root.findByType("Item" as ElementType).props.title, "the last waltz");
  } finally {
    await ui.close();
  }
});

test("old results cannot appear during the next query's debounce or after clearing", async () => {
  const first = deferred<unknown[]>();
  const second = deferred<unknown[]>();
  const ui = await mount(
    async () => "token",
    (query) => (query === "first" ? first.promise : second.promise),
  );
  try {
    await ui.input("first");
    await ui.settle();
    await ui.input("second");
    await act(async () => first.resolve([album("stale")]));
    assert.equal(ui.view.root.findAllByType("Item" as ElementType).length, 0);
    await ui.settle();
    await ui.input("");
    await act(async () => second.resolve([album("also stale")]));
    assert.equal(ui.view.root.findAllByType("Item" as ElementType).length, 0);
    assert.equal(ui.view.root.findByType("EmptyView" as ElementType).props.title, "Search Spotify albums");
  } finally {
    await ui.close();
  }
});

test("fallback launch text searches automatically and an auth failure can be retried", async () => {
  let attempts = 0;
  const ui = await mount(
    async () => {
      if (++attempts === 1) throw new Error("Sign-in failed");
      return "token";
    },
    async (query) => [album(query)],
    "kind of blue",
  );
  try {
    await ui.settle();
    assert.equal(ui.view.root.findByType("EmptyView" as ElementType).props.title, "Spotify search unavailable");
    assert.equal(ui.toasts.length, 1);
    await act(async () => ui.view.root.findByType("Action" as ElementType).props.onAction());
    await ui.settle();
    assert.equal(attempts, 2);
    assert.equal(ui.view.root.findByType("Item" as ElementType).props.title, "kind of blue");
  } finally {
    await ui.close();
  }
});

test("closing during authorization prevents any subsequent search or toast", async () => {
  const auth = deferred<string>();
  const ui = await mount(
    () => auth.promise,
    async () => assert.fail("closed command must not search"),
  );
  await ui.input("album");
  await ui.settle();
  await ui.close();
  await act(async () => auth.resolve("token"));
  assert.deepEqual(ui.toasts, []);
});

test("every library state stays in the final icon-only accessory after the track count", async () => {
  let update!: (id: string, status: AlbumLibraryStatus) => void;
  let signal!: AbortSignal;
  const ui = await mount(
    async () => "token",
    async () => [album("first"), { ...album("second"), totalTracks: 123, releaseDate: "2026" }],
    "albums",
    async (_, __, currentSignal, currentUpdate) => {
      signal = currentSignal;
      update = currentUpdate;
    },
  );
  try {
    await ui.settle();
    const items = () => ui.view.root.findAllByType("Item" as ElementType);
    assert.equal(items()[0].props.accessories[2].icon.source, "Minus");
    for (const [status, symbol] of [
      ["scanning", "CircleProgress25"],
      ["complete", "CheckCircle"],
      ["partial", "CircleProgress50"],
      ["none", "Xmark"],
      ["error", "QuestionMarkCircle"],
    ] as const) {
      await act(async () => update("first", { status, matched: 1, total: 2 }));
      assert.equal(items()[0].props.accessories.length, 3);
      assert.equal(items()[0].props.accessories[1].text, "1 tracks");
      assert.equal(items()[0].props.accessories[2].icon.source, symbol);
      assert.equal("text" in items()[0].props.accessories[2], false);
      assert.equal(items()[1].props.accessories[1].text, "123 tracks");
      assert.equal(items()[1].props.accessories[2].icon.source, "Minus");
    }
    await ui.input("another");
    assert.ok(signal.aborted);
    await act(async () => update("first", { status: "complete" }));
    assert.equal(items().length, 0);
  } finally {
    await ui.close();
  }
});

test("retrying checks and closing the command abort the old checker", async () => {
  const signals: AbortSignal[] = [];
  const ui = await mount(
    async () => "token",
    async () => [album("first")],
    "albums",
    async (_, __, signal) => {
      signals.push(signal);
    },
  );
  await ui.settle();
  try {
    const retry = ui.view.root
      .findAllByType("Action" as ElementType)
      .find((action) => action.props.title === "Retry Library Checks");
    assert.ok(retry);
    await act(async () => retry.props.onAction());
    assert.equal(signals.length, 2);
    assert.ok(signals[0].aborted);
  } finally {
    await ui.close();
  }
  assert.ok(signals[1].aborted);
});
