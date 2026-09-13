import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import test from "node:test";
import { buildSync } from "esbuild";

const source = readFileSync("src/import-notifications.ts", "utf8")
  .replace(
    /import .* from "@raycast\/api";/,
    'const environment = { launchType: "background" }; const LaunchType = { Background: "background", UserInitiated: "userInitiated" }; const launchCommand = testHarness.launchCommand;',
  )
  .replace(
    /import .* from "\.\/notification-window";/,
    "const isRaycastFocused = async () => Boolean(testHarness.focused);",
  )
  .replace(/import .* from "\.\/backend";/, "const { acknowledgeJobNotification, loadJobs } = testHarness;")
  .replace(/import .* from "\.\/notifications";/, "const { showTerminalJobNotification } = testHarness;");
const bundled = buildSync({
  stdin: { contents: source, loader: "ts", resolveDir: resolve("src") },
  bundle: true,
  write: false,
  platform: "node",
  format: "cjs",
}).outputFiles[0].text;

test("worker notification target supports background launch and awaits display before acknowledging", async () => {
  const manifest = JSON.parse(readFileSync("package.json", "utf8"));
  assert.equal(
    manifest.commands.find((command: { name: string }) => command.name === "import-notifications").mode,
    "no-view",
  );
  const module = { exports: {} as { default: () => Promise<void> } };
  const events: string[] = [];
  let finishDisplay!: () => void;
  const display = new Promise<void>((resolve) => {
    finishDisplay = resolve;
  });
  new Function("require", "module", "exports", "testHarness", bundled)(
    createRequire(resolve("package.json")),
    module,
    module.exports,
    {
      loadJobs: async () => ({ pendingNotifications: [{ jobId: "job", updatedAt: "now", status: "complete" }] }),
      showTerminalJobNotification: async () => {
        events.push("display");
        await display;
      },
      acknowledgeJobNotification: async () => {
        events.push("acknowledge");
      },
    },
  );
  let completed = false;
  const running = module.exports.default().then(() => {
    completed = true;
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(events, ["display"]);
  assert.equal(completed, false);
  finishDisplay();
  await running;
  assert.deepEqual(events, ["display", "acknowledge"]);
});

test("focused Raycast hands off to foreground toast without acknowledging or displaying a HUD", async () => {
  const module = { exports: {} as { default: () => Promise<void> } };
  const launches: unknown[] = [];
  new Function("require", "module", "exports", "testHarness", bundled)(
    createRequire(resolve("package.json")),
    module,
    module.exports,
    {
      focused: true,
      loadJobs: async () => ({ pendingNotifications: [{ jobId: "job", updatedAt: "now", status: "complete" }] }),
      launchCommand: async (options: unknown) => {
        launches.push(options);
      },
      showTerminalJobNotification: async () => {
        assert.fail("must not display HUD while focused");
      },
      acknowledgeJobNotification: async () => {
        assert.fail("handoff must not acknowledge delivery");
      },
    },
  );
  await module.exports.default();
  assert.deepEqual(launches, [{ name: "import-notifications", type: "userInitiated" }]);
});
