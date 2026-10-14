import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { buildSync } from "esbuild";
import { createElement, StrictMode, type ComponentType, type ElementType } from "react";
import { act, create } from "react-test-renderer";
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const source = readFileSync(resolve("src/review-problems.tsx"), "utf8")
  .replace(
    /import\s*{[^}]*}\s*from "@raycast\/api";/s,
    `
const host = (name) => (props) => require("react").createElement(name, props, props.children, props.actions, props.detail);
const Action = host("Action"), ActionPanel = host("ActionPanel"), List = host("List"), Form = host("Form");
List.Section = host("Section"); List.Item = host("Item"); List.Item.Detail = host("Detail"); List.EmptyView = host("EmptyView");
for (const name of ["CopyToClipboard", "ShowInFinder", "OpenInBrowser"]) Action[name] = host(name);
const Alert = {ActionStyle:{}}, Color = {}, Icon = {}, Toast = {Style:{}}, confirmAlert = async () => false, useNavigation = () => ({});
`,
  )
  .replace(/import\s*{([^}]*)}\s*from "\.\/backend";/s, "const {$1} = harness;")
  .replace(
    /import\s*{[^}]*}\s*from "\.\/notifications";/s,
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
test("mount replay replaces aborted refresh and renders every completed job", async () => {
  const signals: AbortSignal[] = [];
  const history = Array.from({ length: 87 }, (_, i) => ({
    jobId: `history-${i}`,
    action: "album_combined",
    status: "complete",
    phase: "complete",
    source: { type: "album", id: `album-${i}`, url: "url", name: `Album ${i}`, total: 1 },
    tracks: [],
    counts: { total: 1, complete: 1, ready: 1 },
    notification: { pending: false },
    retryable: false,
  }));
  const module = { exports: {} as { default: ComponentType } };
  new Function("require", "module", "exports", "harness", bundled)(
    createRequire(resolve("package.json")),
    module,
    module.exports,
    {
      loadActivity: (signal: AbortSignal) => {
        signals.push(signal);
        if (signals.length === 1)
          return new Promise((_, reject) =>
            signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true }),
          );
        return Promise.resolve({
          resolver: { problems: [], sources: [], readySources: [] },
          jobs: { jobs: history, pendingNotifications: [] },
        });
      },
    },
  );
  let view!: ReturnType<typeof create>;
  try {
    await act(async () => {
      view = create(createElement(StrictMode, null, createElement(module.exports.default)));
    });
    assert.equal(signals.length, 2, "replacement must start during mount replay");
    assert.ok(signals[0].aborted);
    const section = view.root
      .findAllByType("Section" as ElementType)
      .find((row) => row.props.title === "Recently Completed");
    assert.ok(section);
    assert.equal(section.findAllByType("Item" as ElementType).length, 87);
    assert.equal(view.root.findByType("List" as ElementType).props.isLoading, false);
  } finally {
    if (view) await act(async () => view.unmount());
  }
});
