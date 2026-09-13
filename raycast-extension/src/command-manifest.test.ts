import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("the extension exposes only its four user-facing commands", () => {
  const manifest = JSON.parse(readFileSync("package.json", "utf8"));
  assert.deepEqual(
    manifest.commands.map((command: { name: string }) => command.name),
    ["browse-import-albums", "import-playlist-link", "import-activity-problems", "update-dependencies"],
  );
  assert.ok(manifest.commands.every((command: { mode: string }) => command.mode === "view"));
});
