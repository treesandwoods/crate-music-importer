import type { ImportJob } from "./types";

export interface NotificationDelivery {
  handled: Set<string>;
  showJob(job: ImportJob): Promise<unknown>;
  acknowledge(jobId: string): Promise<unknown>;
  pauseBetweenToasts?(): Promise<void>;
}

export async function deliverPendingNotifications(jobs: ImportJob[], delivery: NotificationDelivery): Promise<void> {
  const pending = jobs.filter((job) => !delivery.handled.has(job.jobId));
  for (const [index, job] of pending.entries()) {
    await delivery.showJob(job);
    await delivery.acknowledge(job.jobId);
    delivery.handled.add(job.jobId);
    if (index < pending.length - 1) await delivery.pauseBetweenToasts?.();
  }
}
