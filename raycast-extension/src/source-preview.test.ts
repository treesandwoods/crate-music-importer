import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { buildSync } from "esbuild";
import { createElement, type ComponentType, type ElementType } from "react";
import { act, create } from "react-test-renderer";

import type { SourcePreview } from "./types";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function findAction(node: unknown, title: string): { props: { onAction: () => unknown } } | undefined {
  if (!node || typeof node !== "object") return undefined;
  const element = node as { type?: unknown; props?: { title?: string; children?: unknown; onAction?: () => unknown } };
  if (element.props?.title === title && element.props.onAction)
    return element as { props: { onAction: () => unknown } };
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

function samplePreview(name: string): SourcePreview {
  return {
    source: { type: "playlist", id: name, name, url: `https://example.test/${name}`, total: 1 },
    counts: {},
    rows: [],
  };
}

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const source = readFileSync(resolve("src/source-preview.tsx"), "utf8")
  .replace(
    /import\s*{[^}]*}\s*from "@raycast\/api";/s,
    `
    const host = (name) => (props) => require("react").createElement(name, props, props.children);
    const Action = Object.assign(host("Action"), { OpenInBrowser: host("OpenInBrowser") });
    const ActionPanel = host("ActionPanel");
    const Label = host("Label"), Metadata = Object.assign(host("Metadata"), { Label });
    const Detail = Object.assign(host("Detail"), { Metadata });
    const Item = Object.assign(host("Item"), { Detail });
    const List = Object.assign(host("List"), { Item, Section: host("Section"), EmptyView: host("EmptyView") });
    const Icon = { Music: "music", Circle: "circle" };
    const Color = { Green: "green", SecondaryText: "secondary" };
    const Toast = { Style: { Animated: "animated", Success: "success", Failure: "failure" } };
    const Alert = { ActionStyle: { Cancel: "cancel" } };
    const confirmAlert = (...args) => harness.confirm(...args), launchCommand = async () => {}, LaunchType = {};
  `,
  )
  .replace(/import .* from "\.\/backend";/, "const previewSource = harness.preview, queueSource = harness.queue;")
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

test("album preview rows and summary show only Music library status", async () => {
  const preview: SourcePreview = {
    source: { type: "album", id: "album", name: "Album", url: "https://example.test", total: 2 },
    album_id: "album",
    counts: { in_library: 1, not_in_library: 1, downloaded: 3, review_music: 1 },
    rows: [
      {
        position: 1,
        recording_id: "one",
        title: "Song One",
        artists: "Artist",
        status: "in_library",
        detail: "managed provenance",
      },
      {
        position: 2,
        recording_id: "two",
        title: "Song Two",
        artists: "Artist",
        status: "not_in_library",
        detail: "download review",
      },
    ],
  };
  const module = { exports: {} as { SourcePreviewView: ComponentType<{ type: "album"; url: string }> } };
  new Function("require", "module", "exports", "harness", bundled)(
    createRequire(resolve("package.json")),
    module,
    module.exports,
    { preview: async () => preview },
  );
  let view!: ReturnType<typeof create>;
  await act(async () => {
    view = create(createElement(module.exports.SourcePreviewView, { type: "album", url: preview.source.url }));
  });
  try {
    const section = view.root.findByType("Section" as ElementType);
    assert.equal(section.props.subtitle, "In Library 1 · Not in Library 1");
    const items = view.root.findAllByType("Item" as ElementType);
    assert.deepEqual(
      items.map((item) => item.props.accessories[0].tag.value),
      ["In Library", "Not in Library"],
    );
    for (const item of items) {
      assert.doesNotMatch(item.props.detail.props.markdown, /managed|download|review|provenance/i);
      assert.match(item.props.detail.props.markdown, /\*\*(In Library|Not in Library)\*\*/);
    }
  } finally {
    await act(async () => view.unmount());
  }
});

test("album preview identifies an existing track that will be upgraded", async () => {
  const preview: SourcePreview = {
    source: { type: "album", id: "album", name: "Album", url: "https://example.test", total: 1 },
    counts: { in_library: 0, not_in_library: 0, upgrade_managed: 1 },
    rows: [
      {
        position: 1,
        recording_id: "original",
        title: "Song",
        artists: "Artist",
        status: "upgrade_managed",
        detail: "Existing Music match; upgrade album metadata",
      },
    ],
  };
  const module = { exports: {} as { SourcePreviewView: ComponentType<{ type: "album"; url: string }> } };
  new Function("require", "module", "exports", "harness", bundled)(
    createRequire(resolve("package.json")),
    module,
    module.exports,
    { preview: async () => preview },
  );
  let view!: ReturnType<typeof create>;
  await act(async () => {
    view = create(createElement(module.exports.SourcePreviewView, { type: "album", url: preview.source.url }));
  });
  try {
    assert.equal(
      view.root.findByType("Section" as ElementType).props.subtitle,
      "In Library 0 · Not in Library 0 · Upgrade existing 1",
    );
    assert.equal(
      view.root.findByType("Item" as ElementType).props.accessories[0].tag.value,
      "Existing Music match — upgrade album",
    );
  } finally {
    await act(async () => view.unmount());
  }
});

test("overlapping preview refreshes keep the newest response and repeated import submits queue once", async () => {
  const initial = samplePreview("Initial");
  const older = deferred<SourcePreview>();
  const newer = deferred<SourcePreview>();
  const confirmation = deferred<boolean>();
  const requests: Array<{ signal?: AbortSignal; promise: Promise<SourcePreview> }> = [];
  let confirms = 0;
  let queued = 0;
  const module = { exports: {} as { SourcePreviewView: ComponentType<{ type: "playlist"; url: string }> } };
  new Function("require", "module", "exports", "harness", bundled)(
    createRequire(resolve("package.json")),
    module,
    module.exports,
    {
      preview: (_type: string, _url: string, signal?: AbortSignal) => {
        const next =
          requests.length === 0 ? Promise.resolve(initial) : requests.length === 1 ? older.promise : newer.promise;
        requests.push({ signal, promise: next });
        return next;
      },
      confirm: () => {
        confirms++;
        return confirmation.promise;
      },
      queue: async () => {
        queued++;
        return { source: { total: 1 } };
      },
    },
  );
  let view!: ReturnType<typeof create>;
  await act(async () => {
    view = create(createElement(module.exports.SourcePreviewView, { type: "playlist", url: initial.source.url }));
  });
  try {
    await act(async () => {
      await requests[0].promise;
    });
    const refresh = findAction(view.root.findByType("List" as ElementType).props.actions, "Refresh Preview");
    assert.ok(refresh);
    await act(async () => {
      void refresh.props.onAction();
      void refresh.props.onAction();
    });
    assert.equal(requests.length, 3);
    assert.ok(requests[1].signal?.aborted);

    await act(async () => newer.resolve(samplePreview("Newest")));
    await act(async () => older.resolve(samplePreview("Older")));
    assert.equal(view.root.findByType("List" as ElementType).props.navigationTitle, "Newest");

    const start = findAction(view.root.findByType("List" as ElementType).props.actions, "Download + Add to Music");
    assert.ok(start);
    await act(async () => {
      void start.props.onAction();
      void start.props.onAction();
    });
    assert.equal(confirms, 1);
    await act(async () => confirmation.resolve(true));
    await act(async () => new Promise((resolve) => setImmediate(resolve)));
    assert.equal(queued, 1);
  } finally {
    await act(async () => view.unmount());
  }
});
