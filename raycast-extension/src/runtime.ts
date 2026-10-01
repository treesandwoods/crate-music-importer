import { execFile, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { environment, getPreferenceValues } from "@raycast/api";
import { expandPath, mergeSettings, readLocalSettings, type Settings } from "./configuration";
import identity from "../package.json";
import { jsonLineSession, type JsonLineSession } from "./json-line-session";

const exec = promisify(execFile);
const toolsPath = `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${process.env.PATH || ""}`;

export function runtime() {
  const settings = mergeSettings(readLocalSettings(), getPreferenceValues<Settings>());
  const support = environment.supportPath;
  const source = settings.repositoryPath
    ? expandPath(settings.repositoryPath)
    : join(environment.assetsPath, "backend");
  if (!existsSync(join(source, "crate_music_importer/ipod_import/cli.py")) && !settings.cliPath) {
    throw new Error("Backend files are missing. Rebuild the extension or set Repository Folder in preferences.");
  }
  const installedPython = join(homedir(), "Library/Application Support/Crate Music Importer/venv/bin/python3");
  const python = settings.pythonPath
    ? expandPath(settings.pythonPath)
    : existsSync(installedPython)
      ? installedPython
      : "python3";
  const managedRoot = expandPath(
    settings.managedMusicDirectory || process.env.CRATE_MANAGED_ROOT || join(homedir(), "Music/mp3 Music"),
  );
  return {
    settings,
    support,
    source,
    python,
    cli: settings.cliPath ? expandPath(settings.cliPath) : undefined,
    env: {
      ...process.env,
      PATH: toolsPath,
      PYTHONPATH: source,
      PYTHONPYCACHEPREFIX: join(support, "pycache"),
      CRATE_MANAGED_ROOT: managedRoot,
      CRATE_RAYCAST_AUTHOR: identity.author,
      CRATE_RAYCAST_EXTENSION: identity.name,
    },
  };
}

export async function backendCommand(args: string[], raycast: boolean, timeout: number): Promise<string> {
  const value = runtime();
  const command = value.cli || value.python;
  const commandArgs = value.cli
    ? [...(raycast ? ["--raycast"] : []), ...args]
    : ["-m", `crate_music_importer.ipod_import${raycast ? ".raycast" : ""}`, ...args];
  const { stdout } = await exec(command, commandArgs, {
    cwd: homedir(),
    env: value.env,
    timeout,
    maxBuffer: 10 * 1024 * 1024,
  });
  return stdout.trim();
}

export function backendJsonSession(args: string[], signal: AbortSignal): JsonLineSession {
  const value = runtime();
  const child = spawn(
    value.cli || value.python,
    value.cli ? args : ["-m", "crate_music_importer.ipod_import", ...args],
    {
      cwd: homedir(),
      env: value.env,
      stdio: "pipe",
    },
  );
  return jsonLineSession(child, signal);
}
