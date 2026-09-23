import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { cropSquareArtwork, squarePlaylistArtwork } from "./playlist-artwork";

test(
  "forced refresh downloads same URL again and selects a new persistent image path",
  { skip: process.platform !== "darwin" },
  async () => {
    const directory = await mkdtemp(join(tmpdir(), "crate-artwork-refresh-test-"));
    const originalFetch = globalThis.fetch;
    const bmp = Buffer.alloc(54 + 120 * 120 * 3, 128);
    bmp.fill(0, 0, 54);
    bmp.write("BM");
    bmp.writeUInt32LE(bmp.length, 2);
    bmp.writeUInt32LE(54, 10);
    bmp.writeUInt32LE(40, 14);
    bmp.writeUInt32LE(120, 18);
    bmp.writeUInt32LE(120, 22);
    bmp.writeUInt16LE(1, 26);
    bmp.writeUInt16LE(24, 28);
    const bmpPath = join(directory, "source.bmp");
    const pngPath = join(directory, "source.png");
    await writeFile(bmpPath, bmp);
    execFileSync("/usr/bin/sips", ["-s", "format", "png", bmpPath, "--out", pngPath]);
    const png = await readFile(pngPath);
    let calls = 0;
    globalThis.fetch = async () => {
      calls += 1;
      return new Response(png, { status: 200 });
    };
    try {
      const url = "https://example.test/playlist.bmp";
      const first = await squarePlaylistArtwork(url, directory);
      assert.equal(await squarePlaylistArtwork(url, directory), first);
      const refreshed = await squarePlaylistArtwork(url, directory, true);
      assert.notEqual(refreshed, first);
      assert.equal(await squarePlaylistArtwork(url, directory), refreshed);
      assert.equal(calls, 2);
    } finally {
      globalThis.fetch = originalFetch;
      await rm(directory, { recursive: true, force: true });
    }
  },
);

for (const [width, height] of [
  [240, 120],
  [120, 240],
  [120, 120],
]) {
  test(
    `actual pixel crop produces a square from ${width}x${height}`,
    { skip: process.platform !== "darwin" },
    async () => {
      const directory = await mkdtemp(join(tmpdir(), "crate-artwork-test-"));
      try {
        const source = join(directory, "input.bmp");
        const output = join(directory, "square.png");
        const bmp = Buffer.alloc(54 + width * height * 3, 128);
        bmp.fill(0, 0, 54);
        bmp.write("BM");
        bmp.writeUInt32LE(bmp.length, 2);
        bmp.writeUInt32LE(54, 10);
        bmp.writeUInt32LE(40, 14);
        bmp.writeUInt32LE(width, 18);
        bmp.writeUInt32LE(height, 22);
        bmp.writeUInt16LE(1, 26);
        bmp.writeUInt16LE(24, 28);
        // Red bands outside the center square must be cropped, not squeezed in.
        const side = Math.min(width, height);
        for (let y = 0; y < height; y++) {
          for (let x = 0; x < width; x++) {
            if (
              x < (width - side) / 2 ||
              x >= (width + side) / 2 ||
              y < (height - side) / 2 ||
              y >= (height + side) / 2
            ) {
              const offset = 54 + (y * width + x) * 3;
              bmp[offset] = 0;
              bmp[offset + 1] = 0;
              bmp[offset + 2] = 255;
            }
          }
        }
        await writeFile(source, bmp);
        await cropSquareArtwork(source, output);
        const png = await readFile(output);
        assert.equal(png.readUInt32BE(16), 128);
        assert.equal(png.readUInt32BE(20), 128);
        const decoded = join(directory, "decoded.bmp");
        execFileSync("/usr/bin/sips", ["-s", "format", "bmp", output, "--out", decoded]);
        const pixels = await readFile(decoded);
        const offset = pixels.readUInt32LE(10);
        const channels = pixels.readUInt16LE(28) / 8;
        assert.ok(channels === 3 || channels === 4);
        for (let i = offset; i < pixels.length; i += channels) {
          assert.equal(pixels[i], pixels[i + 2], "red edge leaked into center crop");
          assert.ok(pixels[i] > 0, "corner should contain image pixels");
        }
      } finally {
        await rm(directory, { recursive: true, force: true });
      }
    },
  );
}
