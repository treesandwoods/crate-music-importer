import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";

export interface JsonLineSession {
  read<T>(): Promise<T>;
  request<T>(value: unknown): Promise<T>;
  close(): void;
}

// A single sequential checker keeps the snapshot and album index in memory.
export function jsonLineSession(child: ChildProcessWithoutNullStreams, signal: AbortSignal): JsonLineSession {
  const lines = createInterface({ input: child.stdout });
  const messages: unknown[] = [];
  let failure: Error | undefined;
  let stderr = "";
  let pending: { resolve(value: unknown): void; reject(error: Error): void } | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;

  function stop(error: Error) {
    failure ??= error;
    clearTimeout(timer);
    pending?.reject(failure);
    pending = undefined;
    lines.close();
    child.stdin.destroy();
    child.kill();
    signal.removeEventListener("abort", abort);
  }
  function abort() {
    stop(new Error("Album library check cancelled."));
  }
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk: string) => {
    stderr = (stderr + chunk).slice(-4096);
  });
  child.on("error", stop);
  child.stdin.on("error", stop);
  child.on("close", () => stop(new Error(stderr.trim().replace(/^ERROR:\s*/, "") || "Album checker stopped.")));
  lines.on("line", (line) => {
    if (failure) return;
    try {
      const value: unknown = JSON.parse(line);
      if (pending) {
        clearTimeout(timer);
        pending.resolve(value);
        pending = undefined;
      } else if (!messages.length) {
        messages.push(value);
      } else {
        stop(new Error("Album checker returned an unexpected response."));
      }
    } catch {
      stop(new Error("Album checker returned invalid JSON."));
    }
  });
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) abort();

  async function read<T>(): Promise<T> {
    if (failure) throw failure;
    if (pending) throw new Error("Album checks must run in order.");
    const value = messages.length
      ? messages.shift()
      : await new Promise<unknown>((resolve, reject) => {
          pending = { resolve, reject };
          timer = setTimeout(() => stop(new Error("Album cache check timed out. Retry the library checks.")), 15_000);
        });
    if (value && typeof value === "object" && "error" in value) throw new Error(String(value.error));
    return value as T;
  }
  return {
    read,
    request: <T>(value: unknown) => {
      const response = read<T>();
      if (!failure) child.stdin.write(`${JSON.stringify(value)}\n`);
      return response;
    },
    close: abort,
  };
}
