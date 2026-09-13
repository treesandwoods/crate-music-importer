import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  canQueuePlaylistUpdate,
  playlistUpdateConfirmation,
  playlistUpdateQueueArgs,
  removalLabel,
} from "./playlist-update-model";
import type { PlaylistUpdatePreview, SavedPlaylistSummary } from "./types";

const playlist: SavedPlaylistSummary = {
  id: "saved-id",
  name: "Saved",
  track_count: 3,
  spotify_url: "https://open.spotify.com/playlist/37i9dQZF1DXTESTFIXTURE1",
};
const preview: PlaylistUpdatePreview = {
  source: {
    type: "playlist",
    id: "saved-id",
    name: "Saved",
    url: playlist.spotify_url,
    saved_total: 3,
    current_total: 3,
  },
  playlist_id: "saved-id",
  additions: [
    {
      position: 3,
      recording_id: "new",
      spotify_id: "sp-new",
      title: "New",
      artists: "Artist",
      status: "reused_music",
      detail: "existing Music match",
    },
  ],
  removals: [
    {
      saved_position: 1,
      recording_id: "unlink",
      spotify_id: "sp-one",
      title: "Shared",
      artists: "Artist",
      action: "unlink",
    },
    {
      saved_position: 2,
      recording_id: "delete",
      spotify_id: "sp-two",
      title: "Sole",
      artists: "Artist",
      action: "delete",
    },
  ],
  up_to_date: false,
  incomplete_data: false,
  blocked: false,
  state: "ready",
  warning: null,
  baseline_backfilled: false,
  removals_deferred: false,
  confirmation_token: "confirmed-preview",
};

test("preview labels and confirmation itemize unlink and permanent deletion", () => {
  assert.equal(removalLabel(preview.removals[0]), "unlink");
  assert.equal(removalLabel(preview.removals[1]), "delete permanently");
  assert.match(playlistUpdateConfirmation(preview), /Artist — Shared: unlink/);
  assert.match(playlistUpdateConfirmation(preview), /Artist — Sole: delete permanently/);
});

test("up-to-date and incomplete previews cannot be queued", () => {
  assert.equal(canQueuePlaylistUpdate(preview), true);
  assert.equal(canQueuePlaylistUpdate({ ...preview, up_to_date: true, state: "up_to_date" }), false);
  assert.equal(
    canQueuePlaylistUpdate({ ...preview, blocked: true, incomplete_data: true, state: "incomplete_data" }),
    false,
  );
});

test("queue payload uses the distinct update action and saved playlist identity", () => {
  const args = playlistUpdateQueueArgs(playlist, preview);
  assert.deepEqual(args.slice(0, 3), ["queue-json", "playlist_update_combined", playlist.spotify_url]);
  const seed = JSON.parse(args[3]);
  assert.equal(seed.savedPlaylistId, "saved-id");
  assert.equal(seed.total, 3);
  assert.equal(seed.confirmationToken, "confirmed-preview");
});

test("command keeps new import first and exposes saved playlist, update, and incomplete states", () => {
  const source = readFileSync("src/import-playlist-link.tsx", "utf8");
  assert.ok(source.indexOf('title="Import a New Playlist"') < source.indexOf('title="Saved Imported Playlists"'));
  assert.match(source, /Playlist is up to date/);
  assert.match(source, /Spotify Data Incomplete/);
  assert.match(source, /Change Stored Spotify Link/);
  assert.match(source, /title=\{primaryActionTitle\}/);
  assert.match(source, /Spotify #\$\{row.position\}/);
  assert.match(source, /source: artwork\[playlist\.cover_url\]/);
  assert.doesNotMatch(source, /mask:/);
  assert.doesNotMatch(source, /<List\s[^>]+actions=\{actions\}/);
});

test("bulk playlist update is isolated above itemized track rows", () => {
  const source = readFileSync("src/import-playlist-link.tsx", "utf8");
  const bulkSection = source.indexOf('<List.Section title="Playlist Update">');
  const additionsSection = source.indexOf('<List.Section title="Additions"');
  const additionRowsStart = source.indexOf("{preview.additions.map", additionsSection);
  const additionRowsEnd = source.indexOf("</List.Section>", additionRowsStart);
  const removalRowsStart = source.indexOf("{preview.removals.map", additionRowsEnd);
  const removalRowsEnd = source.indexOf("</List.Section>", removalRowsStart);

  assert.ok(bulkSection >= 0 && bulkSection < additionsSection);
  assert.equal(source.match(/actions=\{queueActions\}/g)?.length, 1);
  assert.doesNotMatch(source.slice(additionRowsStart, additionRowsEnd), /actions=\{/);
  assert.doesNotMatch(source.slice(removalRowsStart, removalRowsEnd), /actions=\{/);
});
