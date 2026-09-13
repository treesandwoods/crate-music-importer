import { environment, launchCommand, LaunchType } from "@raycast/api";
import { isRaycastFocused } from "./notification-window";
import { acknowledgeJobNotification, loadJobs } from "./backend";
import { deliverPendingNotifications } from "./notification-delivery";
import { showTerminalJobNotification } from "./notifications";

/** A no-view command stays alive until display and durable acknowledgement finish. */
export default async function Command() {
  const pending = (await loadJobs()).pendingNotifications;
  if (!pending.length) return;
  const handled = new Set<string>();
  for (const [index, job] of pending.entries()) {
    if (environment.launchType === LaunchType.Background && (await isRaycastFocused())) {
      // Toasts require a foreground launch. Leave this event pending for it.
      await launchCommand({ name: "import-notifications", type: LaunchType.UserInitiated });
      return;
    }
    await deliverPendingNotifications([job], {
      handled,
      showJob: showTerminalJobNotification,
      acknowledge: acknowledgeJobNotification,
    });
    if (index < pending.length - 1) await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
}
