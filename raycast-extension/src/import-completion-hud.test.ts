import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { transformSync } from "esbuild";
import type { ImportJob } from "./types";

const source = readFileSync("src/import-completion-hud.ts", "utf8")
  .replace(/import .* from "\.\/backend";/, "const { loadJobs, acknowledgeJobNotification } = host;")
  .replace(/import .* from "\.\/notification-delivery";/, "const { deliverPendingNotifications } = host;")
  .replace(/import .* from "\.\/notifications";/, "const { showTerminalJobHUD } = host;");
const compiled = transformSync(source, { loader: "ts", format: "cjs" }).code;

test("the no-view command delivers pending jobs through HUD and acknowledges them", async () => {
  const calls: string[] = [];
  const jobs = [
    { jobId: "album", status: "complete" },
    { jobId: "playlist", status: "needs_attention" },
  ] as ImportJob[];
  const module = { exports: {} as { default: () => Promise<void> } };
  new Function("module", "exports", "host", compiled)(module, module.exports, {
    loadJobs: async () => ({ pendingNotifications: jobs }),
    showTerminalJobHUD: async (job: ImportJob) => calls.push(`hud:${job.jobId}`),
    acknowledgeJobNotification: async (jobId: string) => calls.push(`ack:${jobId}`),
    deliverPendingNotifications: async (
      values: ImportJob[],
      delivery: { showJob: (job: ImportJob) => Promise<unknown>; acknowledge: (jobId: string) => Promise<unknown> },
    ) => {
      assert.equal(values, jobs);
      for (const job of values) {
        await delivery.showJob(job);
        await delivery.acknowledge(job.jobId);
      }
    },
  });
  await module.exports.default();
  assert.deepEqual(calls, ["hud:album", "ack:album", "hud:playlist", "ack:playlist"]);
});
