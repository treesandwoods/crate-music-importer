import { showToast, Toast } from "@raycast/api";

import { compactToast, jobToast } from "./notification-model";
import type { ImportJob } from "./types";

export async function showCompactToast(style: Toast.Style, title: string, message = ""): Promise<Toast> {
  const compact = compactToast(title, message);
  return showToast({ style, ...compact });
}

export function updateCompactToast(toast: Toast, style: Toast.Style, title: string, message = ""): void {
  const value = compactToast(title, message);
  toast.style = style;
  toast.title = value.title;
  toast.message = value.message;
}

export function showTerminalJobToast(job: ImportJob): Promise<Toast> {
  const value = jobToast(job);
  return showToast({
    style: value.style === "success" ? Toast.Style.Success : Toast.Style.Failure,
    title: value.title,
    message: value.message,
  });
}
