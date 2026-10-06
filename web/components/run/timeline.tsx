"use client";

import { useState } from "react";
import { Dot } from "@/components/ui/status";
import { relativeTime } from "@/lib/format";
import type { TimelineEvent } from "@/lib/types";
import type { Tone } from "@/lib/run-state";

const LEVEL_TONE: Record<TimelineEvent["level"], Tone> = {
  info: "neutral",
  success: "ok",
  warning: "warn",
  error: "bad",
  attention: "ember",
};

export function Timeline({ runId, events }: { runId: string; events: TimelineEvent[] }) {
  const [debug, setDebug] = useState<TimelineEvent[] | null>(null);
  const [loading, setLoading] = useState(false);
  const shown = debug ?? events;

  async function toggleDebug() {
    if (debug) {
      setDebug(null);
      return;
    }
    setLoading(true);
    try {
      const response = await fetch(`/api/cavman/runs/${runId}/events?debug=true`, { cache: "no-store" });
      if (response.ok) setDebug((await response.json()).events);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <div className="mb-3 flex justify-end">
        <button type="button" onClick={toggleDebug} className="text-xs text-muted hover:text-fg" aria-pressed={Boolean(debug)}>
          {loading ? "Loading…" : debug ? "Hide technical events" : "Show technical events"}
        </button>
      </div>
      {shown.length === 0 ? (
        <p className="text-sm text-muted">No activity yet.</p>
      ) : (
        <ol tabIndex={0} className="max-h-[32rem] space-y-0 overflow-auto pr-1 focus-visible:outline-2 focus-visible:outline-ink" aria-live="polite" aria-relevant="additions" aria-label="Activity">
          {[...shown].reverse().map((event) => (
            <li key={event.sequence} className="relative flex gap-3 pb-4 pl-1 last:pb-0">
              <span className="mt-1.5"><Dot tone={LEVEL_TONE[event.level]} /></span>
              <div className="min-w-0 flex-1">
                <p className={`text-sm ${event.debug ? "font-mono text-xs text-muted" : "text-fg"}`}>{event.title}</p>
                {event.detail ? <p className="truncate text-xs text-muted">{event.detail}</p> : null}
              </div>
              <time dateTime={event.created_at} className="shrink-0 text-[0.7rem] text-faint">
                {relativeTime(event.created_at)}
              </time>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
