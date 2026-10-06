import type { Metadata } from "next";
import Link from "next/link";
import { CONTACT_EMAIL } from "@/lib/legal";

export const metadata: Metadata = { title: "Docs" };

const SECTIONS = [
  {
    id: "start",
    title: "Start a build",
    body: [
      "Sign in, describe what you want on the New build screen, and press Build it. Cavman creates a project, a durable run, and queues it for a worker. You can close the browser at any time; the run continues.",
      "Optional settings let you name a preferred stack, add constraints, record a deployment target, and set the run's budget ceiling. Everything else is inferred.",
    ],
  },
  {
    id: "repository",
    title: "Start from a repository",
    body: [
      "To work on existing code, paste a public GitHub repository address under the build's optional settings. Cavman imports a snapshot of it into a new project (files that look like secrets are left out), and the build starts from that code.",
    ],
  },
  {
    id: "run",
    title: "Follow a run",
    body: [
      "The run dashboard shows real state from the orchestration core: the current stage, each task and its specialist, attempts, dependencies, trusted check results, reviews, failures and recoveries, approvals, model usage and cost.",
      "Task states: Waiting, Ready, Running, Validating, Reviewing, Revision Needed, Needs Approval, Blocked, Failed, Accepted.",
    ],
  },
  {
    id: "trust",
    title: "What “done” means",
    body: [
      "A specialist returning work only creates a candidate. It is accepted only after every predeclared check passes in the sandbox and a fresh independent reviewer approves it — against the exact bytes submitted. The run completes only when every success criterion cites accepted work.",
      "Checks run inside Bubblewrap with no network, a cleared environment, and resource limits. The sandbox runs Python (compile, pytest) and Node/TypeScript (the Node test runner, tsc and the project's npm run build). npm dependencies are installed by a separate isolated step with install scripts disabled, then mounted read-only. Other stacks are delivered as reviewed source and documents.",
    ],
  },
  {
    id: "approvals",
    title: "Approvals",
    body: [
      "Cavman asks only for consequential decisions: material plan changes, capability escalations, and actions such as publishing code. Each request states what, why, the risk and the exact scope. Approving binds to the scope digest you were shown; if the request changes, you are asked again.",
    ],
  },
  {
    id: "budget",
    title: "Budgets and cost",
    body: [
      "Every run has a spending ceiling and a model-call ceiling. Cavman records provider, model, tokens, cache usage and provider-reported cost for each call. When a limit is reached the run pauses safely; raise the budget and continue if you choose.",
      "Some providers do not report cost for every call. Cavman shows that honestly rather than estimating.",
    ],
  },
  {
    id: "delivery",
    title: "Delivery",
    body: [
      "When a run completes, Cavman assembles an archive from exactly the accepted, fingerprint-verified files plus a build report. Nothing is pushed or deployed on your behalf.",
      "To change a delivered project, use Ask for changes on the completed run. The new run starts from the delivered code, in the same project.",
      "If the server has GitHub sign-in, you can also publish a completed project to a new GitHub repository. Cavman asks for repository access at that moment, and pushes only after you confirm the exact name and visibility.",
    ],
  },
  {
    id: "limits",
    title: "Current limitations",
    body: [
      "Live previews of generated apps are not available yet; Cavman will not render untrusted code on its own origin. Only public GitHub repositories can be imported, and publishing creates a new repository rather than updating one. Sandboxed execution supports Python and Node/TypeScript; a project's own build script runs too, but dev servers do not. OpenRouter is the only configured model provider.",
    ],
  },
];

const FAQ = [
  {
    q: "What does Cavman build well?",
    a: "Python and TypeScript code with tests: libraries, command-line tools, API and business logic, data processing. Those builds are proven by real tests in the sandbox and an independent review. Other stacks (Go, Rust, mobile and others) come back as reviewed source without executed tests, and Cavman warns you before such a build starts.",
  },
  {
    q: "What does a build cost?",
    a: "Hosted Cavman is free during the beta, with a monthly model allowance shown in Settings. Measured builds have cost between a few cents and about $0.70 of model usage. Each run has a ceiling, the New build page estimates the cost from recent real builds, and a run pauses safely rather than going over.",
  },
  {
    q: "What happens when my monthly allowance runs out?",
    a: "New work waits until next month. Runs in progress pause at their next step; nothing is lost, and you can continue them when there is allowance again.",
  },
  {
    q: "Why is my build waiting for me?",
    a: "Either it needs a decision (an approval card on the run explains what, why and the exact scope), or it stopped: it reached its budget, hit a problem it could not recover from, or you stopped it. The run page says which and what you can do next.",
  },
  {
    q: "Can I run what Cavman made?",
    a: "Yes: download the verified project from the completed run and run it yourself. Cavman does not host or preview generated apps.",
  },
  {
    q: "Who can see my prompts and code?",
    a: "Only you. Every project and run is private to your account. Prompts and code go to the model provider only to do the work, and Cavman never uses them to train models. The privacy policy has the details, and Settings lets you export or delete everything.",
  },
  {
    q: "Can I run Cavman myself?",
    a: "Yes. Cavman is open source under the MIT license; the repository has the setup guide. Bring your own model provider key.",
  },
];

export default function DocsPage() {
  return (
    <div className="mx-auto grid max-w-6xl gap-12 px-4 py-20 sm:px-6 lg:grid-cols-[220px_1fr] lg:px-8">
      <nav aria-label="On this page" className="lg:sticky lg:top-24 lg:self-start">
        <p className="eyebrow text-muted">Docs</p>
        <ul className="mt-4 space-y-2 text-sm">
          {SECTIONS.map((section) => (
            <li key={section.id}>
              <a href={`#${section.id}`} className="text-fg-soft hover:text-fg">
                {section.title}
              </a>
            </li>
          ))}
          <li>
            <a href="#faq" className="text-fg-soft hover:text-fg">Questions</a>
          </li>
        </ul>
      </nav>
      <article className="max-w-3xl">
        <h1 className="display text-balance text-2xl leading-tight text-fg sm:text-3xl">Cavman documentation</h1>
        <p className="mt-4 text-lg text-fg-soft">
          Everything you need to use Cavman. Operators should read the{" "}
          <a className="text-glacier underline underline-offset-4" href="https://github.com/michael-muh-freakin-mercer/cavman#readme" rel="noreferrer">
            setup guide in the repository
          </a>
          .
        </p>
        {SECTIONS.map((section) => (
          <section key={section.id} id={section.id} className="scroll-mt-24 border-t border-line pt-10 mt-10">
            <h2 className="text-2xl font-semibold text-fg">{section.title}</h2>
            {section.body.map((paragraph) => (
              <p key={paragraph.slice(0, 24)} className="mt-4 leading-relaxed text-fg-soft">
                {paragraph}
              </p>
            ))}
          </section>
        ))}
        <section id="faq" className="scroll-mt-24 border-t border-line pt-10 mt-10">
          <h2 className="text-2xl font-semibold text-fg">Questions</h2>
          <dl className="mt-6 space-y-6">
            {FAQ.map((item) => (
              <div key={item.q}>
                <dt className="font-medium text-fg">{item.q}</dt>
                <dd className="mt-2 leading-relaxed text-fg-soft">{item.a}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-8 leading-relaxed text-fg-soft">
            Something else? Email{" "}
            <a className="text-glacier underline underline-offset-4" href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
          </p>
        </section>
        <p className="mt-14 text-sm text-muted">
          Ready? <Link href="/app/new" className="font-medium text-glacier underline-offset-2 hover:underline">Start a build</Link>.
        </p>
      </article>
    </div>
  );
}
