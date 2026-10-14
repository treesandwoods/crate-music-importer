import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { buildSync } from "esbuild";

const source = readFileSync(resolve("src/spotify.ts"), "utf8")
  .replace(
    /import\s*{[^}]*}\s*from "@raycast\/api";/s,
    `
    const { getPreferenceValues, OAuth, Toast } = harness.raycast;
  `,
  )
  .replace(
    /import\s*{[^}]*}\s*from "\.\/spotify-model";/s,
    `
    const {
      acquireSpotifyAccessToken, authorizationCodeParameters, parseSpotifyAlbums,
      loadSpotifyAlbumTrackPages, refreshTokenParameters, spotifyApiError,
      spotifySearchParameters, SpotifyRequestError,
    } = harness.model;
  `,
  )
  .replace(/import .* from "\.\/notifications";/, "const showCompactToast = harness.showToast;")
  .replace(
    /import\s*{[^}]*}\s*from "\.\/configuration";/s,
    "const { readLocalSettings, spotifySettings } = harness.configuration;",
  );
const bundled = buildSync({
  stdin: { contents: source, loader: "ts", resolveDir: resolve("src") },
  bundle: true,
  write: false,
  platform: "node",
  format: "cjs",
}).outputFiles[0].text;

function harness(tokens: unknown, fetchResponse: Response) {
  let removedTokens = 0;
  let storedTokens = 0;
  const client = {
    getTokens: async () => tokens,
    removeTokens: async () => {
      removedTokens++;
    },
    setTokens: async () => {
      storedTokens++;
    },
    authorizationRequest: async () => ({ redirectURI: "https://redirect.test", codeVerifier: "verifier" }),
    authorize: async () => ({ authorizationCode: "authorization-code" }),
  };
  const module = { exports: {} as Record<string, (...args: unknown[]) => Promise<unknown>> };
  new Function("require", "module", "exports", "harness", bundled)(
    createRequire(resolve("package.json")),
    module,
    module.exports,
    {
      raycast: {
        getPreferenceValues: () => ({}),
        Toast: { Style: { Success: "success" } },
        OAuth: {
          RedirectMethod: { Web: "web" },
          PKCEClient: class {
            constructor() {
              return client;
            }
          },
        },
      },
      model: {
        acquireSpotifyAccessToken: async (lifecycle: {
          getTokens: () => Promise<unknown>;
          removeTokens: () => Promise<void>;
          refresh: (token: string) => Promise<string>;
          connect: () => Promise<string>;
        }) => {
          const current = await lifecycle.getTokens();
          if (!current) return lifecycle.connect();
          return (current as { accessToken: string }).accessToken;
        },
        authorizationCodeParameters: () => new URLSearchParams("grant_type=authorization_code"),
        refreshTokenParameters: () => new URLSearchParams("grant_type=refresh_token"),
        parseSpotifyAlbums: () => [],
        loadSpotifyAlbumTrackPages: async () => [],
        spotifyApiError: (status: number) => `Spotify HTTP ${status}`,
        spotifySearchParameters: () => new URLSearchParams("q=test"),
        SpotifyRequestError: class extends Error {
          constructor(
            public readonly status: number,
            message: string,
          ) {
            super(message);
          }
        },
      },
      configuration: {
        readLocalSettings: () => ({}),
        spotifySettings: () => ({ providerId: "spotify", clientId: "client" }),
      },
      showToast: async () => ({}),
      fetchResponse,
    },
  );
  return {
    exports: module.exports,
    get removedTokens() {
      return removedTokens;
    },
    get storedTokens() {
      return storedTokens;
    },
  };
}

function response(status: number, value: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: async () => value,
  } as Response;
}

async function withTimeoutStub<T>(
  run: (timeouts: number[], signals: AbortSignal[], controllers: AbortController[]) => Promise<T>,
): Promise<T> {
  const originalFetch = globalThis.fetch;
  const originalTimeout = AbortSignal.timeout;
  const timeouts: number[] = [];
  const signals: AbortSignal[] = [];
  const controllers: AbortController[] = [];
  globalThis.fetch = async (_input, init) => {
    if (init?.signal) signals.push(init.signal as AbortSignal);
    return response(200, { access_token: "fresh-token" });
  };
  AbortSignal.timeout = ((milliseconds: number) => {
    timeouts.push(milliseconds);
    const controller = new AbortController();
    controllers.push(controller);
    return controller.signal;
  }) as typeof AbortSignal.timeout;
  try {
    return await run(timeouts, signals, controllers);
  } finally {
    globalThis.fetch = originalFetch;
    AbortSignal.timeout = originalTimeout;
  }
}

test("Spotify API calls install a 15-second timeout without a caller signal", async () => {
  await withTimeoutStub(async (timeouts, signals, controllers) => {
    globalThis.fetch = async (_input, init) => {
      if (init?.signal) signals.push(init.signal as AbortSignal);
      return response(200, {});
    };
    const app = harness(undefined, response(200, {}));
    await app.exports.searchSpotifyAlbums("test", "access-token");
    assert.deepEqual(timeouts, [15_000]);
    assert.equal(signals.length, 1);
    assert.equal(signals[0].aborted, false);
    controllers[0].abort();
    assert.equal(signals[0].aborted, true);
  });
});

test("Spotify token requests install the same bounded timeout", async () => {
  await withTimeoutStub(async (timeouts, signals, controllers) => {
    const app = harness(undefined, response(200, { access_token: "fresh-token" }));
    await app.exports.spotifyAccessToken();
    assert.deepEqual(timeouts, [15_000]);
    assert.equal(signals.length, 1);
    assert.equal(signals[0].aborted, false);
    assert.equal(app.storedTokens, 1);
    controllers[0].abort();
    assert.equal(signals[0].aborted, true);
  });
});

test("Spotify 401 responses clear saved OAuth tokens", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => response(401, { error: "invalid_token" });
  try {
    const app = harness(undefined, response(401, {}));
    await assert.rejects(
      app.exports.searchSpotifyAlbums("test", "expired-token") as Promise<unknown>,
      /Spotify HTTP 401/,
    );
    assert.equal(app.removedTokens, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
