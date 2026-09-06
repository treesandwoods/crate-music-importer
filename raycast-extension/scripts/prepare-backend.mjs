import { cpSync, mkdirSync, rmSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
const root = join(dirname(fileURLToPath(import.meta.url)), "../..");
const target = join(root, "raycast-extension/assets/backend");
rmSync(target, { recursive: true, force: true });
mkdirSync(target, { recursive: true });
function copyPython(source, destination) {
  mkdirSync(destination, { recursive: true });
  for (const item of readdirSync(source, { withFileTypes: true })) {
    if (item.name === "__pycache__") continue;
    if (item.isDirectory()) copyPython(join(source, item.name), join(destination, item.name));
    else if (item.name.endsWith(".py")) cpSync(join(source, item.name), join(destination, item.name));
  }
}
copyPython(join(root, "crate_music_importer"), join(target, "crate_music_importer"));
cpSync(join(root, "LICENSE"), join(target, "LICENSE"));
