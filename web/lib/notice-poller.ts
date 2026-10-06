import "server-only";
import { auth, ensureAuthSchema } from "./auth";
import { deliverNotices, type Notice, type Recipient } from "./build-emails";
import { apiBase } from "./cavman";
import { emailConfigured, sendEmail } from "./email";
import { setting } from "./env";

/**
 * Polls the API for builds that need their owner and emails them. Started once
 * per web server process from instrumentation.ts; one web server per
 * deployment, so no two pollers race for the same notice.
 * CAVMAN_BUILD_EMAILS=0 turns it off; CAVMAN_NOTICE_POLL_SECONDS sets the pace.
 */
function service(): Record<string, string> {
  return { Authorization: `Bearer ${setting("API_TOKEN")}` };
}

async function fetchNotices(): Promise<Notice[]> {
  const response = await fetch(`${apiBase()}/api/notices`, { headers: service(), cache: "no-store" });
  if (!response.ok) throw new Error(`The API refused the notices request (${response.status}).`);
  return ((await response.json()) as { notices: Notice[] }).notices;
}

async function markSent(jobId: string): Promise<void> {
  const response = await fetch(`${apiBase()}/api/notices/${jobId}/sent`, { method: "POST", headers: service() });
  if (!response.ok && response.status !== 404) throw new Error(`The API refused to mark job ${jobId} (${response.status}).`);
}

async function findUser(id: string): Promise<Recipient | null> {
  const context = await auth.$context;
  return (await context.internalAdapter.findUserById(id)) as Recipient | null;
}

let started = false;

export function startNoticePoller(): void {
  if (started || setting("BUILD_EMAILS") === "0" || !setting("API_TOKEN") || !emailConfigured()) return;
  started = true;
  const seconds = Math.max(5, Number(setting("NOTICE_POLL_SECONDS") ?? 60) || 60);
  const baseUrl = process.env.BETTER_AUTH_URL ?? "http://localhost:3000";
  let running = false;
  const tick = async () => {
    if (running) return;
    running = true;
    try {
      await ensureAuthSchema();
      await deliverNotices({ fetchNotices, markSent, findUser, send: sendEmail, baseUrl });
    } catch (error) {
      // The API may be restarting; the next pass tries again.
      console.warn("[cavman] build emails skipped this pass:", error instanceof Error ? error.message : error);
    } finally {
      running = false;
    }
  };
  setInterval(tick, seconds * 1000).unref();
}
