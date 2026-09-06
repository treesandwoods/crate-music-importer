import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { buildSync } from "esbuild";
import { createElement, type ComponentType, type ElementType } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const source = readFileSync(resolve("src/update-dependencies.tsx"), "utf8")
  .replace(
    /import .* from "@raycast\/api";/,
    `
    const host = (name) => (props) => require("react").createElement(name, props, props.children, props.actions);
    const Action = host("Action"), ActionPanel = host("ActionPanel"), Detail = host("Detail");
    const Alert = { ActionStyle: { Cancel: "cancel" } };
    const confirmAlert = harness.confirm;
  `,
  )
  .replace(
    /import .* from "\.\/backend";/,
    "const dependencyStatus = harness.status, dependencyUpdate = harness.update;",
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

test("dependency updates require a reviewed plan and explicit confirmation; failures stay visible", async () => {
  let confirmed = false;
  const calls: string[] = [];
  const module = { exports: {} as { default: ComponentType } };
  new Function("require", "module", "exports", "harness", bundled)(
    createRequire(resolve("package.json")),
    module,
    module.exports,
    {
      confirm: async () => confirmed,
      status: async () => ({
        operationTime: "now",
        planId: "reviewed-plan",
        dependencies: [
          {
            name: "yt-dlp",
            status: "outdated",
            previousVersion: "1",
            availableVersion: "2",
            command: ["brew", "upgrade", "--formula", "yt-dlp"],
          },
        ],
      }),
      update: async (plan: string) => {
        calls.push(plan);
        throw new Error("Update blocked by active import");
      },
    },
  );
  let view!: ReactTestRenderer;
  await act(async () => {
    view = create(createElement(module.exports.default));
  });
  const actions = () => view.root.findAllByType("Action" as ElementType);
  assert.equal(actions().length, 1);
  await act(async () => {
    actions()[0].props.onAction();
  });
  assert.equal(actions().length, 2);
  await act(async () => {
    await actions()[1].props.onAction();
  });
  assert.deepEqual(calls, []);
  confirmed = true;
  await act(async () => {
    await actions()[1].props.onAction();
  });
  assert.deepEqual(calls, ["reviewed-plan"]);
  assert.match(view.root.findByType("Detail" as ElementType).props.markdown, /active import/);
  await act(async () => {
    view.unmount();
  });
});
