import type { Instrumentation } from "next";

import { reportingEnabled, scrubEvent } from "@/lib/error-reporting";

// Server-side error reporting to Sentry, off unless SENTRY_DSN is set. Only the
// Node.js server reports: no browser SDK, so the CSP stays as it is and no
// visitor's browser talks to Sentry. See lib/error-reporting.ts for what is sent.
export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  // Emails owners when a build finishes or needs them (lib/notice-poller.ts).
  const { startNoticePoller } = await import("@/lib/notice-poller");
  startNoticePoller();
  if (!reportingEnabled()) return;
  const Sentry = await import("@sentry/nextjs");
  Sentry.init({
    dsn: process.env.SENTRY_DSN,
    environment: process.env.SENTRY_ENVIRONMENT || "production",
    release: process.env.SENTRY_RELEASE || undefined,
    // Sentry 11 collects every category unless told otherwise; scrubEvent
    // still runs on whatever is left.
    dataCollection: {
      userInfo: false,
      cookies: false,
      httpHeaders: { request: { allow: ["user-agent", "content-type", "accept", "host", "referer"] }, response: false },
      httpBodies: [],
      urlQueryParams: false,
      graphQL: { document: false, variables: false },
      genAI: { inputs: false, outputs: false },
      databaseQueryData: false,
      queues: false,
      stackFrameVariables: false,
    },
    tracesSampleRate: 0,
    integrations: [Sentry.httpIntegration({ maxRequestBodySize: "none" })],
    beforeSend: scrubEvent,
    initialScope: { tags: { component: "web" } },
  });
}

export const onRequestError: Instrumentation.onRequestError = async (...args) => {
  if (!reportingEnabled() || process.env.NEXT_RUNTIME !== "nodejs") return;
  const Sentry = await import("@sentry/nextjs");
  Sentry.captureRequestError(...args);
};
