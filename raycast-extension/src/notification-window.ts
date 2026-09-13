import { execFile } from "node:child_process";
import { promisify } from "node:util";

const exec = promisify(execFile);

// Query on-screen window metadata, not application focus: Raycast's launcher
// is a nonactivating panel. No screenshots or Accessibility access are used.
export async function isRaycastWindowVisible(): Promise<boolean> {
  const { stdout } = await exec(
    "/usr/bin/osascript",
    [
      "-l",
      "JavaScript",
      "-e",
      `
    ObjC.import("AppKit"); ObjC.import("CoreGraphics");
    const apps = $.NSRunningApplication.runningApplicationsWithBundleIdentifier("com.raycast.macos");
    const pids = [];
    for (let i = 0; i < apps.count; i++) pids.push(Number(apps.objectAtIndex(i).processIdentifier));
    const list = $.CGWindowListCopyWindowInfo(17, 0);
    const windows = ObjC.deepUnwrap(ObjC.castRefToObject(list));
    if (!Array.isArray(windows)) throw new Error("Cannot inspect Raycast window visibility");
    JSON.stringify(windows.some(w => pids.includes(w.kCGWindowOwnerPID) && w.kCGWindowAlpha > 0 && w.kCGWindowBounds.Width >= 300 && w.kCGWindowBounds.Height >= 100));
  `,
    ],
    { timeout: 5_000 },
  );
  if (!/^(true|false)$/.test(stdout.trim())) throw new Error("Invalid Raycast visibility result");
  return stdout.trim() === "true";
}
