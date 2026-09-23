import { acknowledgeJobNotification, loadJobs } from "./backend";
import { deliverPendingNotifications } from "./notification-delivery";
import { showTerminalJobHUD } from "./notifications";

/** Raycast keeps a no-view command alive until this promise resolves. */
export default async function Command() {
  const pending = (await loadJobs()).pendingNotifications;
  await deliverPendingNotifications(pending, {
    handled: new Set<string>(),
    showJob: showTerminalJobHUD,
    acknowledge: acknowledgeJobNotification,
    pauseBetweenToasts: () => new Promise((resolve) => setTimeout(resolve, 2_000)),
  });
}
