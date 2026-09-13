import { execFile } from "node:child_process";
import { promisify } from "node:util";

const exec = promisify(execFile);

// NSWorkspace reads application focus without Accessibility or UI automation.
// Treat any focused Raycast window conservatively: never close it with a HUD.
export async function isRaycastFocused(): Promise<boolean> {
  const { stdout } = await exec(
    "/usr/bin/osascript",
    [
      "-l",
      "JavaScript",
      "-e",
      'ObjC.import("AppKit"); ObjC.unwrap($.NSWorkspace.sharedWorkspace.frontmostApplication.bundleIdentifier);',
    ],
    { timeout: 5_000 },
  );
  return stdout.trim() === "com.raycast.macos";
}
