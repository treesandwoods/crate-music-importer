import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { buildSync } from "esbuild";
import { createElement, type ComponentType, type ElementType } from "react";
import { act, create } from "react-test-renderer";

import type { SourcePreview } from "./types";

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
    const confirmAlert = async () => false, launchCommand = async () => {}, LaunchType = {};
  `,
  )
  .replace(/import .* from "\.\/backend";/, "const previewSource = harness.preview, queueSource = async () => ({});")
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
