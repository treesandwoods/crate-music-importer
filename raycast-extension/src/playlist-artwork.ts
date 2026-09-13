import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { access, mkdir, mkdtemp, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { promisify } from "node:util";

const exec = promisify(execFile);

/** Crop pixels before resizing: Raycast masks alone do not enforce aspect ratio. */
export async function cropSquareArtwork(input: string, output: string): Promise<void> {
  const { stdout } = await exec("/usr/bin/sips", ["-g", "pixelWidth", "-g", "pixelHeight", input]);
  const width = Number(stdout.match(/pixelWidth:\s*(\d+)/)?.[1]);
  const height = Number(stdout.match(/pixelHeight:\s*(\d+)/)?.[1]);
  const side = Math.min(width, height);
  if (!Number.isFinite(side) || side < 1) throw new Error("Invalid playlist image dimensions");
  await exec("/usr/bin/sips", [
    "-s",
    "format",
    "png",
    "--cropToHeightWidth",
    String(side),
    String(side),
    input,
    "--out",
    output,
  ]);
  await exec("/usr/bin/sips", ["--resampleHeightWidth", "128", "128", output]);
}

export async function squarePlaylistArtwork(url: string, directory: string): Promise<string> {
  if (!/^https?:\/\//.test(url)) throw new Error("Invalid playlist artwork URL");
  const target = join(directory, `${createHash("sha256").update(url).digest("hex")}.png`);
  try {
    await access(target);
    return target;
  } catch {
    /* Generate missing cache entry. */
  }
  await mkdir(directory, { recursive: true });
  const temporary = await mkdtemp(join(directory, "crop-"));
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(15_000) });
    if (!response.ok) throw new Error(`Playlist artwork download failed: ${response.status}`);
    const input = join(temporary, "source");
    const output = join(temporary, "square.png");
    await writeFile(input, Buffer.from(await response.arrayBuffer()));
    await cropSquareArtwork(input, output);
    await rename(output, target);
    return target;
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
}
