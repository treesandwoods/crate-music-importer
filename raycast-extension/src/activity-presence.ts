const ACTIVITY_PRESENCE_KEY = "review-activity-presence";
export const ACTIVITY_PRESENCE_MAX_AGE_MS = 5_000;

interface Storage {
  getItem(key: string): Promise<string | number | boolean | undefined>;
  setItem(key: string, value: string): Promise<void>;
  removeItem(key: string): Promise<void>;
}

interface Presence {
  sessionId: string;
  updatedAt: number;
}

function parsePresence(value: string | undefined): Presence | undefined {
  if (!value) return undefined;
  try {
    const parsed = JSON.parse(value) as Partial<Presence>;
    if (typeof parsed.sessionId === "string" && typeof parsed.updatedAt === "number") {
      return { sessionId: parsed.sessionId, updatedAt: parsed.updatedAt };
    }
  } catch {
    // Invalid or old extension state is treated as absent.
  }
  return undefined;
}

export async function markActivityOpen(storage: Storage, sessionId: string, now = Date.now()): Promise<void> {
  await storage.setItem(ACTIVITY_PRESENCE_KEY, JSON.stringify({ sessionId, updatedAt: now }));
}

export async function clearActivityOpen(storage: Storage, sessionId: string): Promise<void> {
  const value = await storage.getItem(ACTIVITY_PRESENCE_KEY);
  const current = parsePresence(typeof value === "string" ? value : undefined);
  if (current?.sessionId === sessionId) await storage.removeItem(ACTIVITY_PRESENCE_KEY);
}

export async function isActivityOpen(storage: Storage, now = Date.now()): Promise<boolean> {
  const value = await storage.getItem(ACTIVITY_PRESENCE_KEY);
  const current = parsePresence(typeof value === "string" ? value : undefined);
  return Boolean(current && now - current.updatedAt >= 0 && now - current.updatedAt <= ACTIVITY_PRESENCE_MAX_AGE_MS);
}
