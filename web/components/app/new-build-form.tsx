"use client";

import { ArrowRight, ChevronDown, LoaderCircle } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useId, useState } from "react";
import { CostEstimate } from "@/components/app/cost-estimate";
import { GithubIcon } from "@/components/brand/github-icon";
import { announceTyping } from "@/components/brand/live-cavman";
import { linkSocial } from "@/lib/auth-client";
import { MAX_PROMPT_LENGTH, newBuildPath, takePendingPrompt } from "@/lib/prompt-storage";
import { unverifiedStacks } from "@/lib/stack-support";
import type { EstimateView } from "@/lib/types";
import { useHydrated } from "@/lib/use-hydrated";

const GITHUB_REPOSITORY = /^https:\/\/github\.com\/[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}?(\.git)?\/?$/;

const MODE_LABELS: Record<string, string> = {
  automatic: "Automatic",
  budget: "Budget",
  balanced: "Balanced",
  quality: "Maximum Quality",
};

export function NewBuildForm({
  initialPrompt,
  projectId,
  projectName,
  defaultBudget,
  maxBudget,
  disabledReason,
  modes = ["automatic"],
  estimate = null,
  githubEnabled = false,
  initialRepository = "",
}: {
  initialPrompt: string;
  projectId: string | null;
  projectName: string | null;
  defaultBudget: number;
  maxBudget: number;
  disabledReason: string | null;
  modes?: string[];
  estimate?: EstimateView | null;
  githubEnabled?: boolean;
  initialRepository?: string;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const [prompt, setPrompt] = useState(initialPrompt);
  const [stack, setStack] = useState("");
  const [constraints, setConstraints] = useState("");
  const [target, setTarget] = useState("");
  const [budget, setBudget] = useState(String(defaultBudget));
  const [mode, setMode] = useState("automatic");
  const [repository, setRepository] = useState(initialRepository);
  const [needsScope, setNeedsScope] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ids = { prompt: useId(), stack: useId(), constraints: useId(), target: useId(), budget: useId(), mode: useId(), repository: useId(), error: useId() };

  useEffect(() => {
    // Recover a prompt typed before signing in, if it did not arrive in the URL.
    const pending = takePendingPrompt();
    // Synchronizing from browser storage (an external system) once on mount.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (pending && !initialPrompt) setPrompt(pending);
  }, [initialPrompt]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (prompt.trim().length < 3) {
      setError("Describe what you want to build.");
      return;
    }
    if (repository.trim() && !GITHUB_REPOSITORY.test(repository.trim())) {
      setError("Use a GitHub repository address like https://github.com/owner/repo.");
      return;
    }
    setBusy(true);
    setError(null);
    setNeedsScope(null);
    try {
      const response = await fetch("/api/cavman/builds", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: prompt.trim(),
          project_id: projectId ?? undefined,
          settings: {
            stack: stack.trim() || undefined,
            constraints: constraints.trim() || undefined,
            deployment_target: target.trim() || undefined,
            budget_usd: Number(budget) || undefined,
            model_mode: mode,
            repository_url: projectId ? undefined : repository.trim() || undefined,
          },
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok && body.needs_scope && githubEnabled) setNeedsScope(body.needs_scope);
      if (!response.ok) {
        const detail = Array.isArray(body.detail) ? "Check the advanced settings." : body.detail;
        throw new Error(typeof detail === "string" ? detail : "Cavman could not start this build.");
      }
      router.push(`/app/runs/${body.run_id}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Cavman could not start this build.");
      setBusy(false);
    }
  }

  const unverified = unverifiedStacks(prompt, stack);
  const input = "mt-1.5 block w-full rounded-md border-2 border-line-strong bg-surface px-3 text-sm text-fg placeholder:text-faint focus:border-ink focus:outline-none";

  return (
    <form method="post" onSubmit={submit} className="mx-auto max-w-3xl" aria-describedby={error ? ids.error : undefined}>
      {projectName ? (
        <p className="mb-3 text-sm text-muted">
          New run in <span className="text-fg">{projectName}</span>. Cavman starts from this project&rsquo;s current code and builds on it.
        </p>
      ) : null}
      <label htmlFor={ids.prompt} className="sr-only">
        What do you want to build?
      </label>
      <div className="rounded-lg border-2 border-ink bg-surface shadow-[5px_5px_0_0_var(--color-ink)] focus-within:shadow-[5px_5px_0_0_var(--color-ink),0_0_0_5px_rgb(255_212_0/0.6)]">
        <textarea
          id={ids.prompt}
          name="prompt"
          value={prompt}
          onChange={(e) => {
            setPrompt(e.target.value);
            announceTyping();
          }}
          maxLength={MAX_PROMPT_LENGTH}
          rows={7}
          autoFocus
          placeholder="Build me a booking app for a tattoo studio. Clients pick an artist and a time slot; the studio gets an email for each booking."
          className="block w-full resize-y rounded-md bg-surface p-5 text-base leading-relaxed text-fg placeholder:text-faint focus:outline-none focus-visible:shadow-none focus-visible:outline-none sm:text-lg"
        />
      </div>
      <div className="mt-2 flex items-start justify-between gap-4 text-xs">
        <p className="text-muted">Cavman runs real tests for Python and Node/TypeScript code. Other stacks are delivered as reviewed source.</p>
        <p className="shrink-0 text-faint">{prompt.length}/{MAX_PROMPT_LENGTH}</p>
      </div>
      {unverified.length ? (
        <p role="status" className="mt-3 rounded-lg border border-warn/30 bg-warn/5 px-4 py-3 text-sm text-warn">
          Cavman can&rsquo;t run or test {unverified.join(", ")} code yet. It will still plan, write and review it, but
          nothing will prove it builds or works. For tested results, ask for Python or Node/TypeScript.
        </p>
      ) : null}

      <details open={Boolean(initialRepository) || undefined} className="group mt-4 rounded-xl border border-line bg-surface/60">
        <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-sm text-fg-soft hover:text-fg">
          Advanced settings (optional)
          <ChevronDown className="h-4 w-4 transition-transform group-open:rotate-180" aria-hidden="true" />
        </summary>
        <div className="grid gap-4 border-t border-line p-4 sm:grid-cols-2">
          <div>
            <label htmlFor={ids.stack} className="text-xs text-muted">Preferred stack</label>
            <input id={ids.stack} value={stack} onChange={(e) => setStack(e.target.value)} maxLength={300} placeholder="e.g. Python, FastAPI, SQLite" className={`${input} h-10`} />
          </div>
          <div>
            <label htmlFor={ids.target} className="text-xs text-muted">Deployment target</label>
            <input id={ids.target} value={target} onChange={(e) => setTarget(e.target.value)} maxLength={200} placeholder="e.g. a Linux container" className={`${input} h-10`} />
          </div>
          <div className="sm:col-span-2">
            <label htmlFor={ids.constraints} className="text-xs text-muted">Constraints</label>
            <textarea id={ids.constraints} value={constraints} onChange={(e) => setConstraints(e.target.value)} maxLength={2000} rows={2} placeholder="Anything Cavman must or must not do" className={`${input} py-2`} />
          </div>
          <div>
            <label htmlFor={ids.budget} className="text-xs text-muted">Budget ceiling (USD, max {maxBudget})</label>
            <input id={ids.budget} type="number" min="0.5" max={maxBudget} step="0.5" value={budget} onChange={(e) => setBudget(e.target.value)} className={`${input} h-10`} />
          </div>
          <div>
            <label htmlFor={ids.mode} className="text-xs text-muted">Model mode</label>
            <select id={ids.mode} value={mode} onChange={(e) => setMode(e.target.value)} className={`${input} h-10`}>
              {modes.map((item) => (
                <option key={item} value={item}>{MODE_LABELS[item] ?? item}</option>
              ))}
            </select>
          </div>
          {projectId ? null : (
            <div className="sm:col-span-2">
              <label htmlFor={ids.repository} className="text-xs text-muted">Start from a GitHub repository</label>
              <input id={ids.repository} type="url" inputMode="url" value={repository} onChange={(e) => setRepository(e.target.value)} maxLength={300} placeholder="https://github.com/owner/repo" className={`${input} h-10`} />
              <p className="mt-1.5 text-xs text-faint">
                Cavman copies the default branch&rsquo;s files (not its history) into a new project. Symlinks and submodules are refused; secret-looking files are left out.
              </p>
            </div>
          )}
          <p className="text-xs leading-relaxed text-muted sm:col-span-2">
            Automatic uses the models the server is configured with. The run pauses safely if it reaches its budget.
          </p>
        </div>
      </details>

      {estimate ? (
        <CostEstimate estimate={estimate} mode={mode} modeLabel={MODE_LABELS[mode] ?? mode} budget={Number(budget)} />
      ) : null}
      {disabledReason ? (
        <p role="alert" className="mt-4 rounded-lg border border-warn/30 bg-warn/5 px-4 py-3 text-sm text-warn">{disabledReason}</p>
      ) : null}
      {error ? (
        <p id={ids.error} role="alert" className="mt-4 text-sm text-bad">{error}</p>
      ) : null}
      {needsScope ? (
        <button
          type="button"
          onClick={() => linkSocial({
            provider: "github",
            scopes: [needsScope],
            // Back to this form with the request and repository filled in.
            callbackURL: `${newBuildPath(prompt)}&repository=${encodeURIComponent(repository.trim())}`,
          })}
          className="mt-3 inline-flex h-10 items-center gap-2 rounded-lg border border-line-strong bg-surface-2 px-4 text-sm font-medium text-fg hover:bg-surface-3"
        >
          <GithubIcon /> Give Cavman access to your GitHub repositories
        </button>
      ) : null}
      <div className="mt-6 flex items-center justify-end gap-3">
        <p className="hidden text-xs text-muted sm:block">You can close this tab — the build keeps going.</p>
        <button
          type="submit"
          disabled={busy || !hydrated || Boolean(disabledReason)}
          className="inline-flex h-12 items-center gap-2 rounded-md border-2 border-ink bg-ember px-7 text-base font-bold text-ink shadow-[3px_3px_0_0_var(--color-ink)] active:translate-x-[2px] active:translate-y-[2px] active:shadow-[1px_1px_0_0_var(--color-ink)] hover:bg-ember-hot disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? <LoaderCircle className="h-5 w-5 animate-spin" aria-hidden="true" /> : null}
          {busy ? (repository.trim() && !projectId ? "Importing…" : "Starting…") : "Build it"}
          {busy ? null : <ArrowRight className="h-5 w-5" aria-hidden="true" />}
        </button>
      </div>
    </form>
  );
}
