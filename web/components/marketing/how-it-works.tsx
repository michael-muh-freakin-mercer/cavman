import { ArrowRight, BrainCircuit, CheckCircle2, MessageSquareText, Network, PackageCheck, Rocket, ScanSearch, Workflow } from "lucide-react";
import Link from "next/link";
import { Cavman } from "@/components/brand/cavman";
import { buttonClass } from "@/components/ui/button";

const STAGES = [
  { n: "01", icon: BrainCircuit, title: "Understand", body: "Interpret objective, constraints, requirements, and desired outcome.", detail: "Cavman turns your words into measurable success criteria before any work starts." },
  { n: "02", icon: Network, title: "Plan", body: "Create a dependency-aware execution graph and select specialists.", detail: "Each task gets one owner, one deliverable, acceptance criteria and predeclared checks." },
  { n: "03", icon: Workflow, title: "Build", body: "Execute ready work in parallel where appropriate.", detail: "Specialists work in isolated workspaces with only the tools their task needs." },
  { n: "04", icon: ScanSearch, title: "Review", body: "Validate, inspect, critique, retry, recover, or replan.", detail: "Sandboxed checks and a fresh independent reviewer gate every candidate. Failures are classified and routed." },
  { n: "05", icon: PackageCheck, title: "Deliver", body: "Verify and return the assembled result.", detail: "Completion requires every success criterion to cite accepted work. You download exactly what was verified." },
];

const SPECIALISTS = ["Python specialist", "TypeScript specialist", "API specialist", "Test specialist", "Docs specialist"];

function FlowNode({ icon: Icon, title, caption, accent = false }: { icon: React.ElementType; title: string; caption: string; accent?: boolean }) {
  return (
    <div className={`panel flex w-full max-w-sm items-center gap-4 px-5 py-4 ${accent ? "shadow-ember" : ""}`}>
      <span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border ${accent ? "border-2 border-ink bg-ember" : "border-line-strong bg-surface-2"}`}>
        <Icon className={`h-5 w-5 ${accent ? "text-ink" : "text-glacier"}`} aria-hidden="true" />
      </span>
      <div className="text-left">
        <p className="text-sm font-semibold text-fg">{title}</p>
        <p className="text-xs text-muted">{caption}</p>
      </div>
    </div>
  );
}

function Down() {
  return (
    <div className="flex h-10 items-center justify-center" aria-hidden="true">
      <svg width="2" height="40" className="overflow-visible">
        <line x1="1" y1="0" x2="1" y2="40" stroke="var(--color-ink)" strokeWidth="1.5" className="animate-flow" />
      </svg>
    </div>
  );
}

export function HowItWorks({ signedIn }: { signedIn: boolean }) {
  return (
    <>
      <section className="stone dig-grid relative overflow-hidden border-b-2 border-ink">
        <div className="mx-auto max-w-4xl px-4 pb-16 pt-20 text-center sm:px-6 sm:pt-28">
          <p className="eyebrow text-muted">Layer 1 · field notes · how it works</p>
          <h1 className="display text-balance mt-6 text-3xl leading-[1.1] text-fg sm:text-5xl">From prompt to <span className="marker">production.</span></h1>
          <p className="text-balance mx-auto mt-6 max-w-2xl text-lg text-fg-soft">
            One Manager owns your goal. Specialists do the work. Nothing counts as done until trusted checks and an
            independent reviewer say so.
          </p>
        </div>
      </section>

      <section aria-labelledby="stages-title" className="mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-8">
        <h2 id="stages-title" className="sr-only">Stages</h2>
        <ol className="grid gap-5 md:grid-cols-2 lg:grid-cols-5">
          {STAGES.map((stage) => (
            <li key={stage.n} className="panel flex flex-col p-6">
              <div className="flex items-center justify-between">
                <span className="font-mono text-xs font-medium text-muted">FIND {stage.n}</span>
                <stage.icon className="h-5 w-5 text-glacier" aria-hidden="true" />
              </div>
              <h3 className="display mt-6 text-sm text-fg">{stage.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-fg-soft">{stage.body}</p>
              <p className="mt-4 border-t border-line pt-4 text-xs leading-relaxed text-muted">{stage.detail}</p>
            </li>
          ))}
        </ol>
      </section>

      <section aria-labelledby="flow-title" className="border-y-2 border-ink bg-surface-2">
        <div className="mx-auto grid max-w-7xl gap-14 px-4 py-24 sm:px-6 lg:grid-cols-[1fr_1.1fr] lg:px-8">
          <div className="lg:pt-8">
            <p className="eyebrow text-muted">The flow</p>
            <h2 id="flow-title" className="display mt-4 text-2xl leading-tight text-fg sm:text-3xl">
              You talk to Cavman. Cavman runs the team.
            </h2>
            <p className="mt-5 max-w-lg text-fg-soft">
              Specialists are created for each project, scoped to one lane of work, and retired when their work is
              accepted. The set below is only an example — Cavman chooses the specialists your project actually needs.
            </p>
            <ul className="mt-8 space-y-3 text-sm text-fg-soft">
              <li className="flex gap-3"><CheckCircle2 className="h-5 w-5 shrink-0 text-ok" aria-hidden="true" />Only accepted work unlocks the tasks that depend on it.</li>
              <li className="flex gap-3"><CheckCircle2 className="h-5 w-5 shrink-0 text-ok" aria-hidden="true" />Failures are classified and routed: retry, revise, replace, escalate or replan.</li>
              <li className="flex gap-3"><CheckCircle2 className="h-5 w-5 shrink-0 text-ok" aria-hidden="true" />Material plan changes and risky actions wait for your exact approval.</li>
            </ul>
          </div>
          <figure className="flex flex-col items-center" aria-label="Flow from your prompt, through Cavman and its specialists, to a verified project">
            <FlowNode icon={MessageSquareText} title="User Prompt" caption="“Build a Python CLI that renames photos by the date they were taken”" />
            <Down />
            <div className="panel flex w-full max-w-sm items-center gap-4 px-5 py-4 shadow-ember">
              <Cavman mood="dig" className="h-10 w-10" />
              <div className="text-left">
                <p className="text-sm font-semibold text-fg">Cavman</p>
                <p className="text-xs text-muted">Plans, delegates, validates, reviews, accepts</p>
              </div>
            </div>
            <Down />
            <div className="w-full max-w-md rounded-lg border-2 border-dashed border-ink/60 p-4">
              <p className="eyebrow mb-3 text-center text-[0.62rem] text-muted">Specialists · chosen per project</p>
              <ul className="grid gap-2 sm:grid-cols-2">
                {SPECIALISTS.map((name, index) => (
                  <li key={name} className={`flex items-center gap-2 rounded-lg border border-line bg-surface-2 px-3 py-2 text-sm text-fg-soft ${index === SPECIALISTS.length - 1 ? "sm:col-span-2 sm:justify-center" : ""}`}>
                    <span className="h-1.5 w-1.5 rounded-full bg-glacier" aria-hidden="true" />
                    {name}
                  </li>
                ))}
              </ul>
            </div>
            <Down />
            <FlowNode icon={Rocket} title="Verified Project" caption="Accepted, tested, reviewed — ready to download" accent />
            <figcaption className="sr-only">
              User Prompt, then Cavman, then a dynamic set of specialists such as product, frontend, backend, QA and
              DevOps, then a verified project.
            </figcaption>
          </figure>
        </div>
      </section>

      <section className="mx-auto flex max-w-4xl flex-col items-center px-4 py-24 text-center sm:px-6">
        <h2 className="display text-balance text-2xl text-fg sm:text-3xl">Ready when you are.</h2>
        <p className="mt-4 text-fg-soft">Describe it once. Cavman does the rest.</p>
        <Link href={signedIn ? "/app/new" : "/sign-up"} className={buttonClass("primary", "lg", "mt-8")}>
          Build something <ArrowRight className="h-4 w-4" aria-hidden="true" />
        </Link>
      </section>
    </>
  );
}
