import { ArrowRight, ShieldAlert, Sparkles } from "lucide-react";
import Link from "next/link";
import { ApiError } from "@/components/app/api-error";
import { PageHeader } from "@/components/app/page-header";
import { RunList } from "@/components/app/run-list";
import { ButtonLink } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/panel";
import { load } from "@/lib/load";
import { isExecuting } from "@/lib/run-state";
import { requireUser } from "@/lib/session";
import type { RunSummary } from "@/lib/types";

export default async function Overview() {
  const user = await requireUser("/app");
  // The newest 50 runs: anything waiting for a decision or still building is among them in practice.
  const result = await load<{ runs: RunSummary[] }>(user.id, "runs?limit=50");
  const first = user.name.split(" ")[0] || "there";
  return (
    <>
      <PageHeader eyebrow="Overview" title={`Hi ${first}.`} description="Your builds, and anything that needs you." actions={<ButtonLink href="/app/new">New build <ArrowRight className="h-4 w-4" aria-hidden="true" /></ButtonLink>} />
      <div className="space-y-8 px-4 py-6 sm:px-8 sm:py-8">
        {!result.ok ? (
          <ApiError status={result.status} message={result.message} />
        ) : result.data.runs.length === 0 ? (
          <>
            <EmptyState
              icon={<Sparkles className="h-6 w-6" />}
              title="No builds yet"
              action={<ButtonLink href="/app/new" size="lg">What do you want to build? <ArrowRight className="h-4 w-4" aria-hidden="true" /></ButtonLink>}
            >
              Describe any piece of software in plain English. Cavman plans it, builds it, tests it and hands it back.
            </EmptyState>
            <FirstRun />
          </>
        ) : (
          <OverviewSections runs={result.data.runs} />
        )}
      </div>
    </>
  );
}

const FIRST_STEPS = [
  { title: "Describe it", body: "Say what it should do and what done looks like. Python and TypeScript get real tests; name the stack if you care." },
  { title: "Watch it work", body: "Cavman plans tasks, specialists build them, the sandbox runs the tests and a fresh reviewer checks each one. You can close the tab." },
  { title: "Decide when asked", body: "Cavman stops for consequential steps and when it reaches its budget. Nothing is spent past the ceiling you set." },
  { title: "Take it away", body: "Download the verified project, or ask for changes and a follow-up run builds on it." },
];

/** First-run guidance: what happens after the first prompt, in four steps. */
function FirstRun() {
  return (
    <section aria-labelledby="first-run">
      <h2 id="first-run" className="text-sm font-semibold text-fg">How a build goes</h2>
      <ol className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {FIRST_STEPS.map((step, index) => (
          <li key={step.title} className="rounded-lg border border-line p-4">
            <p className="text-xs font-semibold text-muted">Step {index + 1}</p>
            <p className="mt-1 text-sm font-medium text-fg">{step.title}</p>
            <p className="mt-1 text-sm text-fg-soft">{step.body}</p>
          </li>
        ))}
      </ol>
      <p className="mt-3 text-sm text-muted">
        More in the <Link href="/docs" className="text-glacier underline-offset-2 hover:underline">docs</Link>, including{" "}
        <Link href="/docs#faq" className="text-glacier underline-offset-2 hover:underline">common questions</Link>.
      </p>
    </section>
  );
}

function OverviewSections({ runs }: { runs: RunSummary[] }) {
  const needsYou = runs.filter((r) => r.state === "approval_needed");
  const active = runs.filter((r) => isExecuting(r.state));
  return (
    <>
      {needsYou.length ? (
        <section aria-labelledby="needs-you">
          <h2 id="needs-you" className="flex items-center gap-2 text-sm font-semibold text-fg">
            <ShieldAlert className="h-4 w-4 text-ember-deep" aria-hidden="true" /> Waiting for your decision
          </h2>
          <div className="mt-3"><RunList runs={needsYou} /></div>
        </section>
      ) : null}
      {active.length ? (
        <section aria-labelledby="active">
          <h2 id="active" className="text-sm font-semibold text-fg">Building now</h2>
          <div className="mt-3"><RunList runs={active} /></div>
        </section>
      ) : null}
      <section aria-labelledby="recent">
        <div className="flex items-center justify-between">
          <h2 id="recent" className="text-sm font-semibold text-fg">Recent runs</h2>
          <Link href="/app/runs" className="text-xs text-glacier hover:underline">All runs</Link>
        </div>
        <div className="mt-3"><RunList runs={runs.slice(0, 8)} /></div>
      </section>
    </>
  );
}
