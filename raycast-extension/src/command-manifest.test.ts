import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("the extension has four view commands and a no-view completion HUD", () => {
  const manifest = JSON.parse(readFileSync("package.json", "utf8"));
  assert.deepEqual(
    manifest.commands.map((command: { name: string }) => command.name),
    [
      "browse-import-albums",
      "import-playlist-link",
      "import-activity-problems",
      "import-completion-hud",
      "update-dependencies",
    ],
  );
  assert.deepEqual(
    manifest.commands.map((command: { mode: string }) => command.mode),
    ["view", "view", "view", "no-view", "view"],
  );
});
