import assert from "node:assert/strict";
import test from "node:test";
import { expandPath, mergeSettings, spotifySettings } from "./configuration";

test("preferences override local values and paths expand for any user", () => {
  assert.equal(expandPath("~/Music/library", "/Users/example"), "/Users/example/Music/library");
  assert.throws(() => expandPath("relative"));
  assert.deepEqual(
    mergeSettings({ repositoryPath: "/old", pythonPath: "/python" }, { repositoryPath: "/new", pythonPath: "" }),
    { repositoryPath: "/new", pythonPath: "/python" },
  );
});

test("Spotify requires user configuration and isolates changed app tokens", () => {
  assert.throws(() => spotifySettings({}, {}), /Spotify Client ID/);
  const first = "a".repeat(32);
  const second = "b".repeat(32);
  const local = { spotifyClientId: first, spotifyProviderId: "legacy-provider" };
  assert.equal(spotifySettings(local, {}).providerId, "legacy-provider");
  assert.equal(spotifySettings(local, { spotifyClientId: second }).providerId, `spotify-album-search-${second}`);
});
