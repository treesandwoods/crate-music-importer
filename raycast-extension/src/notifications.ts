import { showHUD, showToast, Toast, PopToRootType } from "@raycast/api";

import { compactText, compactToast, jobToast } from "./notification-model";
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

export function showTerminalJobHUD(job: ImportJob): Promise<void> {
  const value = jobToast(job);
  const message = [compactText(value.title, 28), value.message].filter(Boolean).join(" · ");
  return showHUD(message, { clearRootSearch: false, popToRootType: PopToRootType.Suspended });
}
