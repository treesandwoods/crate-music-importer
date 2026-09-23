import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { buildSync } from "esbuild";
import type { ImportJob } from "./types";

const source = readFileSync("src/notifications.ts", "utf8").replace(
  /import .* from "@raycast\/api";/,
  "const { showHUD, showToast, Toast, PopToRootType } = host;",
);
const bundled = buildSync({
  stdin: { contents: source, loader: "ts", resolveDir: resolve("src") },
  bundle: true,
  write: false,
  platform: "node",
  format: "cjs",
}).outputFiles[0].text;

for (const [status, type, mode, expected] of [
  ["complete", "album", undefined, "Album added to Music · Test Album · 12 tracks"],
  ["complete", "playlist", "update", "Playlist update complete · Test Album · 12 changes"],
  ["needs_attention", "album", undefined, "Album needs review · Test Album · 2 tracks"],
  ["needs_attention", "playlist", "update", "Playlist needs review · Test Album · 2 tracks"],
  ["failed", "album", undefined, "Album import failed · Test Album · Open Activity"],
] as const) {
  test(`${type} ${status} sends one job-level HUD`, async () => {
    const calls: unknown[][] = [];
    const module = { exports: {} as { showTerminalJobHUD: (job: ImportJob) => Promise<void> } };
    new Function("module", "exports", "host", bundled)(module, module.exports, {
      showHUD: async (...args: unknown[]) => calls.push(args),
      showToast: async () => assert.fail("Terminal events must use HUD"),
      Toast: { Style: { Success: "success", Failure: "failure" } },
      PopToRootType: { Suspended: "suspended" },
    });
    await module.exports.showTerminalJobHUD({
      status,
      mode,
      source: { type, name: "Test Album", total: 12 },
      counts: { total: 12, review: 1, failed: 1 },
    } as ImportJob);
    assert.deepEqual(calls, [[expected, { clearRootSearch: false, popToRootType: "suspended" }]]);
  });
}
