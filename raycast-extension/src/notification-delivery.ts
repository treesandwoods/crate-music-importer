import type { ImportJob } from "./types";

export interface NotificationDelivery {
  handled: Set<string>;
  showJob(job: ImportJob): Promise<unknown>;
  acknowledge(jobId: string): Promise<unknown>;
  pauseBetweenToasts?(): Promise<void>;
}

export function notificationEventKey(job: ImportJob): string {
  return `${job.jobId}:${job.finishedAt || job.updatedAt}:${job.status}`;
}

export async function deliverPendingNotifications(jobs: ImportJob[], delivery: NotificationDelivery): Promise<void> {
  const pending = jobs.filter((job) => !delivery.handled.has(notificationEventKey(job)));
  for (const [index, job] of pending.entries()) {
    await delivery.showJob(job);
    await delivery.acknowledge(job.jobId);
    delivery.handled.add(notificationEventKey(job));
    if (index < pending.length - 1) await delivery.pauseBetweenToasts?.();
  }
}
