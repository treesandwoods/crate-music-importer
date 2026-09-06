import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { isAbsolute, join } from "node:path";

export interface Settings {
  managedMusicDirectory?: string;
  repositoryPath?: string;
  cliPath?: string;
  pythonPath?: string;
  spotifyClientId?: string;
  spotifyProviderId?: string;
}

export function expandPath(value: string, home = homedir()): string {
  const path = value === "~" ? home : value.startsWith("~/") ? join(home, value.slice(2)) : value;
  if (!isAbsolute(path)) throw new Error(`Use an absolute path or ~/ in preferences: ${value}`);
  return path;
}

export function readLocalSettings(): Settings {
  const path = expandPath(
    process.env.CRATE_CONFIG_FILE || join(homedir(), "Library/Application Support/Crate Music Importer/config.json"),
  );
  if (!existsSync(path)) return {};
  const value: unknown = JSON.parse(readFileSync(path, "utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`Invalid configuration: ${path}`);
  return value as Settings;
}

export function mergeSettings(local: Settings, preferences: Settings): Settings {
  const result = { ...local };
  for (const [key, value] of Object.entries(preferences)) {
    if (typeof value === "string" && value.trim()) result[key as keyof Settings] = value.trim();
  }
  return result;
}

export function spotifySettings(local: Settings, preferences: Settings): { clientId: string; providerId: string } {
  const settings = mergeSettings(local, preferences);
  const clientId = settings.spotifyClientId?.trim();
  if (!clientId || !/^[a-fA-F0-9]{32}$/.test(clientId)) {
    throw new Error(
      "Set your Spotify Client ID in Crate Music Importer preferences. See the README for developer-app and redirect URI setup.",
    );
  }
  return {
    clientId,
    providerId:
      local.spotifyClientId === clientId && local.spotifyProviderId
        ? local.spotifyProviderId
        : `spotify-album-search-${clientId}`,
  };
}
