import { showToast, Toast } from "@raycast/api";

import { compactToast, jobToast } from "./notification-model";
import type { ImportJob } from "./types";

export async function showCompactToast(style: Toast.Style, title: string, message = ""): Promise<Toast> {
  const compact = compactToast(title, message);
  return showToast({ style, ...compact });
}

export function showTerminalJobToast(job: ImportJob): Promise<Toast> {
  const value = jobToast(job);
  return showToast({
    style: value.style === "success" ? Toast.Style.Success : Toast.Style.Failure,
    title: value.title,
    message: value.message,
  });
}
