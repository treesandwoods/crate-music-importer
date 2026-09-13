import { acknowledgeJobNotification, loadJobs } from "./backend";
import { deliverPendingNotifications } from "./notification-delivery";
import { showTerminalJobToast } from "./notifications";

/** A no-view command stays alive until display and durable acknowledgement finish. */
export default async function Command() {
  await deliverPendingNotifications((await loadJobs()).pendingNotifications, {
    handled: new Set<string>(),
    showJob: showTerminalJobToast,
    acknowledge: acknowledgeJobNotification,
    pauseBetweenToasts: () => new Promise((resolve) => setTimeout(resolve, 2_000)),
  });
}
