import type { Email } from "./email";

/**
 * Emails about a build that has stopped and needs its owner: done, waiting for
 * a decision, or stopped short. The API decides which finished jobs qualify
 * (GET /api/notices); this module words the email and walks the list. It has
 * no server-only imports so the unit tests can drive it with fakes.
 */
export type Notice = {
  job_id: string;
  run_id: string;
  owner_id: string;
  project_name: string;
  prompt: string;
  state: string;
  label: string;
  explanation: string;
  tasks_accepted: number;
  tasks_total: number;
  cost_usd: number | null;
  finished_at: string;
};

export type Recipient = { email: string; name?: string | null; emailVerified: boolean; buildEmails?: boolean | null };

export type NoticeDeps = {
  fetchNotices: () => Promise<Notice[]>;
  markSent: (jobId: string) => Promise<void>;
  findUser: (id: string) => Promise<Recipient | null>;
  send: (email: Email) => Promise<void>;
  baseUrl: string;
};

function escape(value: string): string {
  return value.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

function wording(notice: Notice): { subject: string; lead: string; action: string } {
  const project = notice.project_name;
  switch (notice.state) {
    case "complete":
      return {
        subject: `Your build is ready: ${project}`,
        lead: `Cavman finished your build. ${notice.tasks_accepted} of ${notice.tasks_total} tasks passed their checks and review.`,
        action: "See the build and download it",
      };
    case "approval_needed":
      return {
        subject: `Cavman needs your decision: ${project}`,
        lead: notice.explanation,
        action: "Review and decide",
      };
    case "input_needed":
      return {
        subject: `Cavman has questions about your build: ${project}`,
        lead: notice.explanation,
        action: "Answer them",
      };
    case "budget_reached":
      return {
        subject: `Your build reached its budget: ${project}`,
        lead: `${notice.explanation} Nothing more is spent until you continue it.`,
        action: "See where it stopped",
      };
    default:
      return {
        subject: `Your build stopped: ${project}`,
        lead: notice.explanation,
        action: "See where it stopped",
      };
  }
}

export function noticeEmail(to: string, notice: Notice, baseUrl: string): Email {
  const base = baseUrl.replace(/\/$/, "");
  const url = `${base}/app/runs/${notice.run_id}`;
  const settings = `${base}/app/settings`;
  const { subject, lead, action } = wording(notice);
  const cost = notice.cost_usd == null ? "" : `Model cost so far: $${notice.cost_usd.toFixed(2)}.`;
  const why = "You get this email because you started this build on Cavman. Turn these emails off in Settings";
  return {
    to,
    subject,
    text: [lead, `You asked for: "${notice.prompt}"`, cost, `${action}: ${url}`, `${why}: ${settings}`]
      .filter(Boolean).join("\n\n"),
    html: [
      `<p>${escape(lead)}</p>`,
      `<p>You asked for: &ldquo;${escape(notice.prompt)}&rdquo;</p>`,
      cost ? `<p>${escape(cost)}</p>` : "",
      `<p><a href="${escape(url)}">${escape(action)}</a></p>`,
      `<p style="color:#666;font-size:12px">${escape(why)}: <a href="${escape(settings)}">Settings</a>.</p>`,
    ].join(""),
  };
}

/**
 * Email each pending notice's owner once, then mark it sent. An owner who is
 * gone, unverified or opted out is marked without an email. A failed send is
 * left unmarked, so the next pass tries again (the API stops offering a job
 * a few hours after it ended).
 */
export async function deliverNotices(deps: NoticeDeps): Promise<{ sent: number; skipped: number; failed: number }> {
  const result = { sent: 0, skipped: 0, failed: 0 };
  for (const notice of await deps.fetchNotices()) {
    try {
      const user = await deps.findUser(notice.owner_id);
      if (!user || !user.emailVerified || user.buildEmails === false) {
        result.skipped += 1;
      } else {
        await deps.send(noticeEmail(user.email, notice, deps.baseUrl));
        result.sent += 1;
      }
      await deps.markSent(notice.job_id);
    } catch (error) {
      result.failed += 1;
      console.error(`[cavman] build email for job ${notice.job_id} failed:`, error instanceof Error ? error.message : error);
    }
  }
  return result;
}
