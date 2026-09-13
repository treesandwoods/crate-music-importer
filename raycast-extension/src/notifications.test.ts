import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { buildSync } from "esbuild";
import type { ImportJob } from "./types";

const source = readFileSync("src/notifications.ts", "utf8").replace(
  /import .* from "@raycast\/api";/,
  "const { showHUD, showToast, Toast, PopToRootType, environment, LaunchType } = host;",
);
const bundled = buildSync({
  stdin: { contents: source, loader: "ts", resolveDir: resolve("src") },
  bundle: true,
  write: false,
  platform: "node",
  format: "cjs",
}).outputFiles[0].text;

for (const windowOpen of [false, true]) {
  for (const status of ["complete", "needs_attention"] as const) {
    test(`${status} uses the appropriate notification API with window ${windowOpen ? "open" : "closed"}`, async () => {
      const calls: unknown[][] = [];
      const module = { exports: {} as { showTerminalJobNotification: (job: ImportJob) => Promise<void> } };
      new Function("module", "exports", "host", bundled)(module, module.exports, {
        environment: { launchType: windowOpen ? "userInitiated" : "background" },
        LaunchType: { Background: "background" },
        showHUD: async (...args: unknown[]) => {
          assert.equal(windowOpen, false, "An open Raycast window must not be closed by HUD");
          calls.push(args);
        },
        showToast: async (options: { title: string; message: string }) => {
          assert.equal(windowOpen, true, "Background calls must use HUD explicitly");
          calls.push([`${options.title} · ${options.message}`]);
        },
        Toast: { Style: { Success: "success", Failure: "failure" } },
        PopToRootType: { Suspended: "suspended" },
      });
      await module.exports.showTerminalJobNotification({
        status,
        source: { type: "album", name: "Test Album", total: 12 },
        counts: { total: 12, review: 2, failed: 0 },
      } as ImportJob);
      assert.equal(calls.length, 1);
      assert.match(String(calls[0][0]), /Test Album/);
      assert.match(String(calls[0][0]), status === "complete" ? /12 tracks/ : /2 tracks/);
      if (!windowOpen) assert.deepEqual(calls[0][1], { clearRootSearch: false, popToRootType: "suspended" });
    });
  }
}
