import { getPreferenceValues, OAuth, Toast } from "@raycast/api";

import {
  acquireSpotifyAccessToken,
  authorizationCodeParameters,
  parseSpotifyAlbums,
  refreshTokenParameters,
  spotifyApiError,
  spotifySearchParameters,
} from "./spotify-model";
import { showCompactToast } from "./notifications";
import type { SpotifyAlbumSummary } from "./types";

import { readLocalSettings, spotifySettings, type Settings } from "./configuration";

function authSettings() {
  return spotifySettings(readLocalSettings(), getPreferenceValues<Settings>());
}

function oauthClient() {
  return new OAuth.PKCEClient({
    redirectMethod: OAuth.RedirectMethod.Web,
    providerName: "Spotify",
    providerId: authSettings().providerId,
    description: "Connect Spotify to search its album catalog. Crate Music Importer never receives your password.",
  });
}
const AUTHORIZE_URL = "https://accounts.spotify.com/authorize";
const TOKEN_URL = "https://accounts.spotify.com/api/token";
const API_URL = "https://api.spotify.com/v1";

async function tokenRequest(parameters: URLSearchParams): Promise<OAuth.TokenResponse> {
  const response = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: parameters,
  });
  const value = (await response.json()) as OAuth.TokenResponse & { error_description?: string };
  if (!response.ok || !value.access_token) {
    throw new Error(value.error_description || `Spotify sign-in returned HTTP ${response.status}.`);
  }
  return value;
}

async function refresh(refreshToken: string): Promise<string> {
  const response = await tokenRequest(refreshTokenParameters(authSettings().clientId, refreshToken));
  response.refresh_token = response.refresh_token || refreshToken;
  await oauthClient().setTokens(response);
  console.info("[spotify-auth] Refreshed Spotify token stored");
  return response.access_token;
}

async function connect(): Promise<string> {
  const request = await oauthClient().authorizationRequest({
    endpoint: AUTHORIZE_URL,
    clientId: authSettings().clientId,
    scope: "user-read-private",
    extraParameters: {
      show_dialog: "true",
      redirect_uri: "https://raycast.com/redirect?packageName=crate-music-importer",
    },
  });
  const response = await oauthClient().authorize(request);
  console.info("[spotify-auth] OAuth callback reached worker");
  const tokenResponse = await tokenRequest(
    authorizationCodeParameters({
      clientId: authSettings().clientId,
      code: response.authorizationCode,
      redirectUri: request.redirectURI,
      codeVerifier: request.codeVerifier,
    }),
  );
  await oauthClient().setTokens(tokenResponse);
  console.info("[spotify-auth] Spotify token stored");
  await showCompactToast(Toast.Style.Success, "Spotify connected");
  return tokenResponse.access_token;
}

export async function spotifyAccessToken(): Promise<string> {
  return acquireSpotifyAccessToken({
    getTokens: () => oauthClient().getTokens(),
    removeTokens: () => oauthClient().removeTokens(),
    refresh,
    connect,
  });
}

async function spotifyJson(path: string, token: string): Promise<Record<string, unknown>> {
  const response = await fetch(`${API_URL}${path}`, { headers: { Authorization: `Bearer ${token}` } });
  if (response.status === 401) await oauthClient().removeTokens();
  if (!response.ok) throw new Error(spotifyApiError(response.status, response.headers.get("retry-after")));
  return (await response.json()) as Record<string, unknown>;
}

export async function searchSpotifyAlbums(query: string, token: string): Promise<SpotifyAlbumSummary[]> {
  const parameters = spotifySearchParameters(query);
  const value = await spotifyJson(`/search?${parameters}`, token);
  return parseSpotifyAlbums(value);
}
