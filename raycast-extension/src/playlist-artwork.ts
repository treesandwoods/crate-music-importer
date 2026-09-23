import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { access, mkdir, mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
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

export async function squarePlaylistArtwork(url: string, directory: string, force = false): Promise<string> {
  if (!/^https?:\/\//.test(url)) throw new Error("Invalid playlist artwork URL");
  const hash = createHash("sha256").update(url).digest("hex");
  const pointer = join(directory, `${hash}.current`);
  const original = join(directory, `${hash}.png`);
  if (!force) {
    try {
      const current = (await readFile(pointer, "utf8")).trim();
      if (/^[a-f0-9]{64}-[a-f0-9-]+\.png$/.test(current) && current.startsWith(`${hash}-`)) {
        const cached = join(directory, current);
        await access(cached);
        return cached;
      }
    } catch {
      /* No refreshed version exists. */
    }
    try {
      await access(original);
      return original;
    } catch {
      /* Generate missing cache entry. */
    }
  }
  await mkdir(directory, { recursive: true });
  const temporary = await mkdtemp(join(directory, "crop-"));
  try {
    const response = await fetch(url, {
      headers: { "Cache-Control": "no-cache", Pragma: "no-cache" },
      signal: AbortSignal.timeout(15_000),
    });
    if (!response.ok) throw new Error(`Playlist artwork download failed: ${response.status}`);
    const input = join(temporary, "source");
    const output = join(temporary, "square.png");
    await writeFile(input, Buffer.from(await response.arrayBuffer()));
    await cropSquareArtwork(input, output);
    const version = `${hash}-${randomUUID()}.png`;
    const target = force ? join(directory, version) : original;
    await rename(output, target);
    if (force) await writeFile(pointer, `${version}\n`);
    return target;
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
}
