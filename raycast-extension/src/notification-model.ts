import type { ImportJob } from "./types";

export const TOAST_TITLE_LIMIT = 32;
export const TOAST_MESSAGE_LIMIT = 48;

function graphemes(value: string): string[] {
  const Segmenter = (
    Intl as unknown as {
      Segmenter?: new (
        locale?: string,
        options?: { granularity: "grapheme" },
      ) => {
        segment(text: string): Iterable<{ segment: string }>;
      };
    }
  ).Segmenter;
  if (!Segmenter) return Array.from(value);
  return Array.from(new Segmenter(undefined, { granularity: "grapheme" }).segment(value), (part) => part.segment);
}

export function compactText(value: string, limit: number): string {
  const parts = graphemes(String(value || "").trim());
  if (parts.length <= limit) return parts.join("");
  return `${parts.slice(0, Math.max(0, limit - 1)).join("")}…`;
}

export function compactToast(title: string, message = ""): { title: string; message?: string } {
  const result: { title: string; message?: string } = { title: compactText(title, TOAST_TITLE_LIMIT) };
  const compactMessage = compactText(message, TOAST_MESSAGE_LIMIT);
  if (compactMessage) result.message = compactMessage;
  return result;
}

export function jobToast(job: ImportJob): {
  style: "success" | "failure";
  title: string;
  message?: string;
} {
  const kind = job.source.type === "album" ? "Album" : "Playlist";
  const tracks = `${job.counts.total || job.source.total || 0} ${job.mode === "update" ? "changes" : "tracks"}`;
  if (job.status === "complete") {
    return {
      style: "success",
      ...compactToast(job.mode === "update" ? "Playlist update complete" : `${kind} added to Music`, tracks),
    };
  }
  if (job.status === "needs_attention") {
    const count = job.counts.review + job.counts.failed;
    return {
      style: "failure",
      ...compactToast(`${kind} needs attention`, `${count} ${count === 1 ? "track needs" : "tracks need"} review`),
    };
  }
  return {
    style: "failure",
    ...compactToast(job.mode === "update" ? "Playlist update failed" : `${kind} import failed`, "Open Import Activity"),
  };
}
