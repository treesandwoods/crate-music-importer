import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { buildSync } from "esbuild";
import { createElement, StrictMode, type ComponentType, type ElementType } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { DependencyResult, HealthResult, HealthAuditState } from "./backend";
import {
  canUpdate,
  confirmationMessage,
  dependencySummary,
  eligibleUpdates,
  groupIssues,
  updateTitle,
} from "./health-view-model";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const source = readFileSync(resolve("src/update-dependencies.tsx"), "utf8")
  .replace(
    /import\s*{[^}]*}\s*from "@raycast\/api";/s,
    `
    const host = (name) => (props) => require("react").createElement(name, props, props.children, props.actions, props.detail);
    const Action = host("Action"), ActionPanel = host("ActionPanel"), List = host("List");
    List.Section = host("Section"); List.Item = host("Item"); List.Item.Detail = host("Detail");
    Action.CopyToClipboard = host("Copy"); Action.ShowInFinder = host("Finder");
    const Alert = { ActionStyle: { Cancel: "cancel" } };
    const confirmAlert = harness.confirm;
  `,
  )
  .replace(
    /import\s*{[^}]*}\s*from "\.\/backend";/s,
    "const dependencyStatus = harness.status, dependencyUpdate = harness.update, loadHealthAudit = harness.health, startHealthAudit = harness.start;",
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

function dependencies(status = "outdated"): DependencyResult {
  return {
    operation: "status",
    operationTime: "now",
    planId: "reviewed-plan",
    dependencies: [
      {
        name: "yt-dlp",
        status,
        previousVersion: "1",
        availableVersion: "2",
        resultingVersion: "1",
        resolvedPath: "/brew/yt-dlp",
        installationMethod: "homebrew",
        error: null,
        command: status === "outdated" ? ["brew", "upgrade", "--formula", "yt-dlp"] : null,
      },
    ],
  };
}
const healthResult: HealthResult = {
  schemaVersion: 1,
  checkedAt: "now",
  status: "attention",
  summary: { musicTracks: 1 },
  checks: {},
  issues: [
    {
      id: "missing",
      title: "Missing file",
      severity: "critical",
      category: "missing_file",
      ownership: "user_owned",
      detail: "File unavailable",
      persistentIds: ["PID"],
      recordingIds: [],
      paths: ["/music/song.mp3"],
      tracks: [{ title: "Song", artist: "Artist", album: "Album", persistent_id: "PID" }],
      evidence: {},
      suggestedAction: "review",
      suggestedNextStep: "Review in Music.",
    },
  ],
};
function auditState(running = false): HealthAuditState {
  return {
    status: running ? "running" : "complete",
    running,
    mode: "normal",
    pid: 123,
    phase: "Checking files",
    checked: 1,
    total: 3,
    startedAt: null,
    updatedAt: null,
    finishedAt: null,
    lastReport: healthResult,
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
async function render(overrides: Record<string, unknown> = {}) {
  const calls = {
    starts: 0,
    status: 0,
    health: 0,
    updates: [] as string[],
    confirmations: [] as Array<{ title: string; message: string }>,
  };
  const harness = {
    status: async () => {
      calls.status++;
      return dependencies();
    },
    health: async () => {
      calls.health++;
      return auditState();
    },
    start: async () => {
      calls.starts++;
      return auditState(true);
    },
    update: async (plan: string) => {
      calls.updates.push(plan);
      return { ...dependencies(), operation: "update" };
    },
    confirm: async (options: { title: string; message: string }) => {
      calls.confirmations.push(options);
      return false;
    },
    ...overrides,
  };
  const module = { exports: {} as { default: ComponentType } };
  new Function("require", "module", "exports", "harness", bundled)(
    createRequire(resolve("package.json")),
    module,
    module.exports,
    harness,
  );
  let view!: ReactTestRenderer;
  await act(async () => {
    view = create(createElement(StrictMode, null, createElement(module.exports.default)));
  });
  const actions = () => view.root.findAllByType("Action" as ElementType);
  return {
    view,
    calls,
    actions,
    updateAction: () => actions().find((action) => /^Update \d+ Stable/.test(action.props.title)),
    text: () =>
      view.root
        .findAllByType("Detail" as ElementType)
        .map((detail) => detail.props.markdown)
        .join("\n"),
  };
}

test("loads saved results once under StrictMode without starting, with independent dependencies", async () => {
  const pending = deferred<HealthAuditState>();
  let count = 0;
  const ui = await render({
    health: () => {
      count++;
      return pending.promise;
    },
  });
  assert.equal(count, 1);
  assert.equal(ui.calls.starts, 0);
  assert.equal(ui.calls.status, 1);
  assert.match(ui.text(), /yt-dlp/);
  assert.equal(ui.updateAction(), undefined);
  await act(async () => pending.resolve(auditState()));
  assert.ok(ui.updateAction());
  assert.match(ui.text(), /Missing file/);
  await act(async () => ui.view.unmount());
});

test("health stays visible when dependency availability fails", async () => {
  const ui = await render({
    status: async () => {
      throw new Error("Network unavailable");
    },
  });
  assert.match(ui.text(), /Missing file/);
  assert.match(ui.text(), /Update availability could not be checked/);
  assert.match(ui.text(), /Network unavailable/);
  assert.equal(ui.updateAction(), undefined);
  await act(async () => ui.view.unmount());
});

test("dependencies stay visible when Music permission fails", async () => {
  const ui = await render({
    health: async () => {
      throw new Error("Music permission denied");
    },
  });
  assert.match(ui.text(), /Music permission denied/);
  assert.match(ui.text(), /yt-dlp/);
  assert.ok(ui.updateAction());
  await act(async () => ui.view.unmount());
});

for (const status of ["current", "failed", "skipped"]) {
  test(`${status} dependencies never offer an update`, async () => {
    const ui = await render({ status: async () => dependencies(status) });
    assert.equal(ui.updateAction(), undefined);
    if (status === "current") assert.match(ui.text(), /All eligible dependencies are current/);
    if (status === "failed") assert.match(ui.text(), /Update availability could not be checked/);
    await act(async () => ui.view.unmount());
  });
}

test("confirmation contains only eligible transitions; cancellation makes no update", async () => {
  const result = dependencies();
  result.dependencies.push(
    { ...result.dependencies[0], name: "deno" },
    { ...result.dependencies[0], name: "unsafe", status: "skipped" },
  );
  const ui = await render({ status: async () => result });
  assert.equal(ui.updateAction()!.props.title, "Update 2 Stable Dependencies…");
  await act(async () => {
    await ui.updateAction()!.props.onAction();
  });
  assert.equal(ui.calls.confirmations.length, 1);
  assert.match(ui.calls.confirmations[0].message, /yt-dlp: 1 → 2/);
  assert.match(ui.calls.confirmations[0].message, /deno: 1 → 2/);
  assert.doesNotMatch(ui.calls.confirmations[0].message, /unsafe/);
  assert.deepEqual(ui.calls.updates, []);
  await act(async () => ui.view.unmount());
});

test("update errors remain visible without erasing health findings", async () => {
  const ui = await render({
    confirm: async () => true,
    update: async () => {
      throw new Error("Update blocked by active import");
    },
  });
  await act(async () => {
    await ui.updateAction()!.props.onAction();
  });
  assert.match(ui.text(), /active import/);
  assert.match(ui.text(), /Missing file/);
  assert.equal(ui.calls.health, 1);
  assert.equal(ui.updateAction(), undefined);
  await act(async () => ui.view.unmount());
});

test("successful update refreshes only dependencies and preserves validation failures", async () => {
  let statuses = 0;
  const ui = await render({
    confirm: async () => true,
    status: async () => {
      statuses++;
      return dependencies(statuses === 1 ? "outdated" : "current");
    },
    update: async () => ({ ...dependencies("failed"), validation: { metadata: { error: "YouTube check failed" } } }),
  });
  await act(async () => {
    await ui.updateAction()!.props.onAction();
  });
  assert.equal(statuses, 2);
  assert.equal(ui.calls.health, 1);
  assert.match(ui.text(), /Missing file/);
  assert.match(ui.text(), /YouTube check failed/);
  assert.equal(ui.updateAction(), undefined);
  await act(async () => ui.view.unmount());
});

test("view model gates eligibility on plan, command, status and both busy states", () => {
  const result = dependencies();
  assert.equal(eligibleUpdates(result).length, 1);
  assert.equal(canUpdate(result, true, false), false);
  assert.equal(canUpdate(result, false, true), false);
  assert.equal(canUpdate(result, false, false), true);
  assert.equal(canUpdate({ ...result, planId: undefined }, false, false), false);
  assert.equal(canUpdate({ ...result, error: "failed" }, false, false), false);
  assert.equal(eligibleUpdates({ ...result, dependencies: [{ ...result.dependencies[0], command: [] }] }).length, 0);
  assert.equal(updateTitle(result), "Update 1 Stable Dependency…");
  assert.match(confirmationMessage(result), /brew upgrade --formula yt-dlp/);
  assert.equal(dependencySummary(dependencies("failed")), "Update availability could not be checked");
  assert.equal(groupIssues(healthResult.issues)[0].severity, "critical");
});

test("running audit reattaches, polls to completion and keeps findings; unmount stops polling", async () => {
  let loads = 0;
  const ui = await render({
    health: async () => {
      loads++;
      if (loads === 1) return auditState(true);
      if (loads === 2) {
        const state = { ...auditState(true), checked: 2 };
        delete state.lastReport;
        return state;
      }
      return { ...auditState(), lastReport: { ...healthResult, summary: { musicTracks: 77 } } };
    },
  });
  assert.equal(ui.calls.starts, 0);
  assert.match(ui.text(), /Missing file/);
  assert.match(ui.text(), /1\/3 files/);
  assert.equal(ui.updateAction(), undefined);
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 2100));
  });
  assert.equal(loads, 2);
  assert.match(ui.text(), /2\/3 files/);
  assert.match(ui.text(), /Missing file/);
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 2100));
  });
  assert.equal(loads, 4);
  assert.match(ui.text(), /77/);
  assert.ok(ui.updateAction());
  await act(async () => ui.view.unmount());
  let reopenedLoads = 0;
  const reopened = await render({
    health: async () => {
      reopenedLoads++;
      return auditState(true);
    },
  });
  assert.equal(reopened.calls.starts, 0);
  assert.match(reopened.text(), /1\/3 files/);
  await act(async () => reopened.view.unmount());
  await new Promise((resolve) => setTimeout(resolve, 2100));
  assert.equal(reopenedLoads, 1);
});

test("explicit start is guarded against duplicate clicks and keeps previous findings", async () => {
  const pending = deferred<HealthAuditState>();
  let starts = 0;
  const ui = await render({
    start: () => {
      starts++;
      return pending.promise;
    },
  });
  const action = ui.actions().find((item) => item.props.title === "Refresh Library Health")!;
  await act(async () => {
    void action.props.onAction();
    void action.props.onAction();
  });
  assert.equal(starts, 1);
  assert.match(ui.text(), /Missing file/);
  await act(async () => pending.resolve(auditState(true)));
  assert.equal(ui.updateAction(), undefined);
  await act(async () => ui.view.unmount());
});

test("interrupted audit keeps prior findings and offers retry", async () => {
  const ui = await render({ health: async () => ({ ...auditState(), status: "failed", error: "Audit interrupted" }) });
  assert.match(ui.text(), /Audit interrupted/);
  assert.match(ui.text(), /Missing file/);
  assert.ok(ui.actions().find((item) => item.props.title === "Refresh Library Health"));
  await act(async () => ui.view.unmount());
});

test("empty saved state offers normal and deep explicit audits without waiting for dependencies", async () => {
  const pending = deferred<DependencyResult>();
  let deep: boolean | undefined;
  const ui = await render({
    status: () => pending.promise,
    health: async () => ({ ...auditState(), status: "idle", lastReport: null }),
    start: async (deepAll: boolean) => {
      deep = deepAll;
      return auditState(true);
    },
  });
  assert.equal(ui.calls.starts, 0);
  assert.match(ui.text(), /No saved health report/);
  assert.ok(ui.actions().find((item) => item.props.title === "Run Library Health Audit"));
  await act(async () => {
    await ui
      .actions()
      .find((item) => item.props.title === "Deep Check All Local Music")!
      .props.onAction();
  });
  assert.equal(deep, true);
  await act(async () => pending.resolve(dependencies()));
  assert.equal(ui.updateAction(), undefined);
  await act(async () => ui.view.unmount());
});
