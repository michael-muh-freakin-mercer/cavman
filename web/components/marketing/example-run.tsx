import { Check, CircleDashed, LoaderCircle, ShieldCheck } from "lucide-react";

/**
 * Marketing illustration of a run, labelled as an example. Authenticated
 * product screens never use this component; they render real backend state.
 */
const ROWS = [
  { role: "Planner", task: "Spec: rename by EXIF date, dry run by default", state: "Accepted", tone: "ok" },
  { role: "Python specialist", task: "Date reader and rename plan", state: "Reviewing", tone: "review" },
  { role: "Python specialist", task: "Command-line interface", state: "Running", tone: "glacier" },
  { role: "Test specialist", task: "pytest suite with sample photos", state: "Waiting", tone: "neutral" },
] as const;

const TONE = {
  ok: "text-ok border-ok/30 bg-ok/10",
  review: "text-review border-review/30 bg-review/10",
  glacier: "text-glacier border-glacier/30 bg-glacier/10",
  neutral: "text-fg-soft border-line-strong bg-surface-3",
};

export function ExampleRun() {
  return (
    <figure className="panel relative overflow-hidden" aria-label="Illustrative example of a Cavman run">
      <div className="flex items-center justify-between border-b-2 border-ink px-5 py-3">
        <div className="flex items-center gap-3">
          <div className="flex gap-1.5" aria-hidden="true">
            <span className="h-2.5 w-2.5 rounded-full bg-line-strong" />
            <span className="h-2.5 w-2.5 rounded-full bg-line-strong" />
            <span className="h-2.5 w-2.5 rounded-full bg-line-strong" />
          </div>
          <span className="font-mono text-xs text-muted">photo-renamer</span>
        </div>
        <span className="sticker rotate-3 bg-sky py-0.5 text-[0.62rem]">EXAMPLE</span>
      </div>
      <div className="grid gap-0 md:grid-cols-[1.35fr_1fr]">
        <ul className="divide-y divide-line">
          {ROWS.map((row) => (
            <li key={row.role} className="flex items-center gap-4 px-5 py-3.5">
              <span className="w-5 text-muted" aria-hidden="true">
                {row.state === "Accepted" ? (
                  <Check className="h-4 w-4 text-ok" />
                ) : row.state === "Running" ? (
                  <LoaderCircle className="h-4 w-4 animate-spin text-glacier" />
                ) : (
                  <CircleDashed className="h-4 w-4" />
                )}
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm text-fg">{row.task}</p>
                <p className="text-xs text-muted">{row.role}</p>
              </div>
              <span className={`rounded-full border px-2.5 py-0.5 text-xs ${TONE[row.tone]}`}>{row.state}</span>
            </li>
          ))}
        </ul>
        <div className="border-t-2 border-ink bg-surface-2 p-5 md:border-l-2 md:border-t-0">
          <p className="eyebrow text-muted">Trusted checks</p>
          <ul className="mt-3 space-y-2.5 text-sm">
            <li className="flex items-center justify-between"><span className="text-fg-soft">Compile</span><span className="text-ok">Passed</span></li>
            <li className="flex items-center justify-between"><span className="text-fg-soft">Candidate tests</span><span className="text-ok">Passed</span></li>
            <li className="flex items-center justify-between"><span className="text-fg-soft">Regression tests</span><span className="text-glacier">Running</span></li>
            <li className="flex items-center justify-between"><span className="text-fg-soft">Independent review</span><span className="text-muted">Not run</span></li>
          </ul>
          <div className="mt-5 flex items-start gap-2 rounded-lg border border-line bg-surface p-3 text-xs text-muted">
            <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-glacier" aria-hidden="true" />
            Results come from sandboxed execution, never from an agent saying “tests passed”.
          </div>
        </div>
      </div>
      <figcaption className="sr-only">Illustration only; not a real run.</figcaption>
    </figure>
  );
}
