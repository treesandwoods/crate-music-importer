import assert from "node:assert/strict";
import test from "node:test";

import {
  acquireSpotifyAccessToken,
  authorizationCodeParameters,
  parseSpotifyAlbums,
  refreshTokenParameters,
  spotifyApiError,
  spotifySearchParameters,
} from "./spotify-model";

test("OAuth lifecycle reuses a valid stored access token", async () => {
  let connected = false;
  const token = await acquireSpotifyAccessToken({
    getTokens: async () => ({ accessToken: "stored", refreshToken: "refresh", isExpired: () => false }),
    removeTokens: async () => assert.fail("valid tokens must not be removed"),
    refresh: async () => assert.fail("valid tokens must not be refreshed"),
    connect: async () => {
      connected = true;
      return "new";
    },
  });

  assert.equal(token, "stored");
  assert.equal(connected, false);
});

test("OAuth lifecycle refreshes an expired PKCE token", async () => {
  const refreshed: string[] = [];
  const token = await acquireSpotifyAccessToken({
    getTokens: async () => ({ accessToken: "expired", refreshToken: "refresh", isExpired: () => true }),
    removeTokens: async () => assert.fail("successful refresh must keep stored tokens"),
    refresh: async (refreshToken) => {
      refreshed.push(refreshToken);
      return "renewed";
    },
    connect: async () => assert.fail("successful refresh must not reconnect"),
  });

  assert.equal(token, "renewed");
  assert.deepEqual(refreshed, ["refresh"]);
});

test("OAuth lifecycle reconnects after a failed refresh", async () => {
  let removed = 0;
  const token = await acquireSpotifyAccessToken({
    getTokens: async () => ({ accessToken: "expired", refreshToken: "bad-refresh", isExpired: () => true }),
    removeTokens: async () => {
      removed += 1;
    },
    refresh: async () => {
      throw new Error("invalid refresh token");
    },
    connect: async () => "connected",
  });

  assert.equal(token, "connected");
  assert.equal(removed, 1);
});

test("OAuth lifecycle removes an unrefreshable token before reconnecting", async () => {
  const events: string[] = [];
  const token = await acquireSpotifyAccessToken({
    getTokens: async () => ({ accessToken: "expired", isExpired: () => true }),
    removeTokens: async () => {
      events.push("remove");
    },
    refresh: async () => assert.fail("missing refresh tokens cannot be refreshed"),
    connect: async () => {
      events.push("connect");
      return "connected";
    },
  });

  assert.equal(token, "connected");
  assert.deepEqual(events, ["remove", "connect"]);
});

test("PKCE token parameters never contain a client secret", () => {
  const exchange = authorizationCodeParameters({
    clientId: "public-client",
    code: "code",
    redirectUri: "https://raycast.com/redirect",
    codeVerifier: "verifier",
  });
  const refresh = refreshTokenParameters("public-client", "refresh");

  assert.equal(exchange.get("grant_type"), "authorization_code");
  assert.equal(exchange.get("code_verifier"), "verifier");
  assert.equal(exchange.has("client_secret"), false);
  assert.equal(refresh.get("grant_type"), "refresh_token");
  assert.equal(refresh.get("refresh_token"), "refresh");
  assert.equal(refresh.has("client_secret"), false);
});

test("album search stays within Spotify's current ten-result limit", () => {
  const parameters = spotifySearchParameters("  kind of blue  ");

  assert.equal(parameters.get("q"), "kind of blue");
  assert.equal(parameters.get("type"), "album");
  assert.equal(parameters.get("limit"), "10");
});

test("Spotify album responses preserve edition details", () => {
  const albums = parseSpotifyAlbums({
    albums: {
      items: [
        {
          id: "album-1",
          name: "Album (Deluxe)",
          artists: [{ name: "Artist" }],
          external_urls: { spotify: "https://open.spotify.com/album/album-1" },
          images: [{ url: "https://images.test/album.jpg" }],
          release_date: "2001-02-03",
          total_tracks: 14,
          album_type: "album",
        },
        { name: "Missing ID" },
      ],
    },
  });

  assert.deepEqual(albums, [
    {
      id: "album-1",
      name: "Album (Deluxe)",
      artists: "Artist",
      url: "https://open.spotify.com/album/album-1",
      imageUrl: "https://images.test/album.jpg",
      releaseDate: "2001-02-03",
      totalTracks: 14,
      albumType: "album",
    },
  ]);
});

test("Spotify API errors stay concise and actionable", () => {
  assert.equal(spotifyApiError(401), "Spotify sign-in expired. Reopen to reconnect.");
  assert.equal(spotifyApiError(403), "Spotify denied album search for this developer app.");
  assert.equal(spotifyApiError(429), "Spotify search limit reached. Try again shortly.");
  assert.equal(spotifyApiError(429, "17"), "Spotify limit reached. Retry in 17s.");
  assert.equal(spotifyApiError(500), "Spotify search returned HTTP 500.");
});
