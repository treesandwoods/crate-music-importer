import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("the extension has four named view commands and a no-view notification entry", () => {
  const manifest = JSON.parse(readFileSync("package.json", "utf8"));
  assert.deepEqual(
    manifest.commands.map((command: { name: string }) => command.name),
    [
      "browse-import-albums",
      "import-playlist-link",
      "import-activity-problems",
      "completion-notifications",
      "update-dependencies",
    ],
  );
  assert.deepEqual(
    manifest.commands.map((command: { mode: string }) => command.mode),
    ["view", "view", "view", "no-view", "view"],
  );
  assert.deepEqual(
    manifest.commands
      .filter((command: { mode: string }) => command.mode === "view")
      .map((command: { title: string }) => command.title),
    ["Albums", "Playlists", "Review Activity", "Library Health"],
  );
});
