import type { Metadata } from "next";
import { LegalDocument, type LegalSection } from "@/components/legal/legal-document";
import { CONTACT_EMAIL, MINIMUM_AGE, OPERATOR } from "@/lib/legal";

export const metadata: Metadata = { title: "Privacy Policy" };

const SECTIONS: LegalSection[] = [
  {
    id: "collect",
    title: "What we collect",
    body: ["Only what the service needs to work:"],
    list: [
      "Account details: your email address, name if you give one, a hashed password, your two-factor key and backup codes (encrypted) if you turn on two-factor sign-in, and your GitHub username and access token if you connect GitHub.",
      "What you give Cavman: prompts, settings, uploaded or imported files and repositories, and your messages and approvals during a build.",
      "What Cavman makes: plans, generated code and documents, check results, reviews, delivery archives, and a record of each model call (model, tokens and cost).",
      "Sign-in records: your active sessions with the IP address and browser they came from, so you can see and revoke them in Settings.",
      "Server logs: IP address, time, page requested and browser, kept for security and troubleshooting.",
    ],
  },
  {
    id: "use",
    title: "How we use it",
    body: [
      "To run your builds, keep your account secure, stop abuse, enforce spending limits, send you account email (verification, password resets, build notices), and fix problems. We do not sell your data, show you ads, or use your prompts or code to train models.",
    ],
  },
  {
    id: "third-parties",
    title: "Who else processes it",
    body: ["Cavman relies on these providers. Each receives only what its job needs:"],
    list: [
      "OpenRouter and the model providers it routes to: your prompts, files and generated code, to produce the build. Their own policies govern what they retain.",
      "E2B: generated code and its dependencies, run in an isolated sandbox for checks.",
      "DigitalOcean: hosting, database and storage for everything above (United States).",
      "Cloudflare: DNS for cavman.dev, and Turnstile, which checks sign-up and password-reset requests for bots.",
      "Resend: your email address and the content of account emails.",
      "GitHub: only if you connect it, for imports and publishing you ask for.",
      "Sentry: error reports from our servers (the error, where in our code it happened and which page or API route failed), so we can fix problems. We configure it not to receive your prompts, code, request contents, cookies or IP address.",
      "Grafana Labs (Grafana Cloud): our server logs, so we can investigate problems. Logs record technical events and errors. They can include account and build identifiers and, in error messages, short fragments of a build's content.",
    ],
  },
  {
    id: "cookies",
    title: "Cookies",
    body: [
      "Cavman uses one essential cookie to keep you signed in. Cloudflare Turnstile may set its own cookies on the sign-up and password-reset pages to tell people from bots. There are no analytics or advertising cookies, so there is no cookie banner.",
    ],
  },
  {
    id: "retention",
    title: "How long we keep it",
    body: [
      "Your account, projects, builds and deliveries are kept until you delete them or your account. Temporary build workspaces and dependency caches are cleaned up automatically after a build finishes. Server logs are rotated automatically and capped in size, so older entries are deleted as new ones arrive; the copy held by Grafana Cloud is deleted after at most 30 days. Backups are kept for up to 30 days.",
      "Deleting your account in Settings erases your account, projects, runs and deliveries from the live system straight away. If a build is still running, Settings asks you to stop it first. Copies in backups expire on the backup schedule.",
    ],
  },
  {
    id: "rights",
    title: "Your choices and rights",
    body: [
      "In Settings you can download all your data, see and revoke signed-in devices, and delete your account. For anything else (a correction, a question about what we hold, or an objection) email us and we will answer within 30 days. Depending on where you live, you may also have the right to complain to your data protection authority.",
    ],
  },
  {
    id: "security",
    title: "Security",
    body: [
      "Connections are encrypted, passwords are hashed, generated code runs in isolated sandboxes without your credentials, and access to production is limited to the operator. No system is perfectly secure; if we learn of a breach affecting your data we will tell you promptly.",
    ],
  },
  {
    id: "children",
    title: "Age",
    body: [`Cavman is not for anyone under ${MINIMUM_AGE}, and we do not knowingly collect their data.`],
  },
  {
    id: "changes",
    title: "Changes and contact",
    body: [
      `We will update the date above when this policy changes and tell you by email about significant changes. The data controller is ${OPERATOR}. Privacy questions: ${CONTACT_EMAIL}.`,
    ],
  },
];

export default function PrivacyPage() {
  return (
    <LegalDocument
      title="Privacy Policy"
      intro="What Cavman collects, why, who else sees it, and how to take it back."
      sections={SECTIONS}
    />
  );
}
