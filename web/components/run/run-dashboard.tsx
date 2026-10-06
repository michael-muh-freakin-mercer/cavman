"use client";

import { Activity, FlaskConical, Info, Layers, ShieldAlert, TriangleAlert, Users, Wallet, WifiOff } from "lucide-react";
import { Cavman } from "@/components/brand/cavman";
import { Notice, Panel, EmptyState } from "@/components/ui/panel";
import { StatusPill } from "@/components/ui/status";
import { duration, formatUsd, relativeTime } from "@/lib/format";
import { RUN_MOOD, RUN_TONE, isExecuting } from "@/lib/run-state";
import type { ApprovalView, RunDetail } from "@/lib/types";
import { postJson, useRun } from "@/lib/use-run";
import { ApprovalCard } from "./approval-card";
import { ArtifactsPanel } from "./artifacts-panel";
import { ChecksPanel } from "./checks-panel";
import { CompletionPanel } from "./completion-panel";
import { FailuresPanel } from "./failures-panel";
import { RunControls } from "./run-controls";
import { StageTracker } from "./stage-tracker";
import { TaskCard } from "./task-card";
import { Timeline } from "./timeline";
import { UsagePanel } from "./usage-panel";

const ATTENTION_STATES = new Set(["waiting", "paused", "budget_reached", "failed", "blocked", "recovering"]);

export function RunDashboard({ initial, githubEnabled = false }: { initial: RunDetail; githubEnabled?: boolean }) {
  const { run, setRun, connection, error, refresh } = useRun(initial);
  const executing = isExecuting(run.state);
  const pending = run.approvals.filter((a) => a.status === "pending");
  const decided = run.approvals.filter((a) => a.status !== "pending");
  const titles = Object.fromEntries(run.tasks.map((t) => [t.id, t.title]));
  const lastJob = run.jobs.at(-1);
  const managerMessage = lastJob?.message && lastJob.status !== "running" ? lastJob.message : null;
  const elapsedEnd = run.status === "active" && executing ? new Date().toISOString() : lastJob?.finished_at ?? run.updated_at;

  async function decide(approval: ApprovalView, decision: "approve" | "reject", reason: string) {
    const result = await postJson<{ run: RunDetail }>(`runs/${run.id}/approvals/${approval.id}`, {
      decision,
      reason,
      scope_digest: approval.scope_digest,
    });
    setRun((previous) => ({ ...result.run, timeline: previous.timeline.length > result.run.timeline.length ? previous.timeline : result.run.timeline }));
  }

  return (
    <div className="space-y-6 px-4 py-6 sm:px-8 sm:py-8">
      <header className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex min-w-0 items-start gap-4">
          <span className="grid h-16 w-16 shrink-0 place-items-center rounded-lg border-2 border-ink bg-surface shadow-[3px_3px_0_0_var(--color-ink)]">
            <Cavman mood={RUN_MOOD[run.state]} className="h-12 w-12" />
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <StatusPill tone={RUN_TONE[run.state]} pulse={executing}>
                {run.label}
              </StatusPill>
              {run.executor === "scripted" ? (
                <span className="rounded-full border border-review/40 bg-review/10 px-2.5 py-0.5 text-xs text-review" title="This run used scripted models for testing. All state changes and checks were still real.">
                  Scripted test executor
                </span>
              ) : null}
              <ConnectionBadge connection={connection} />
            </div>
            <h1 className="mt-3 text-2xl font-extrabold tracking-tight text-fg sm:text-3xl">{run.project_name}</h1>
            <p className="mt-2 line-clamp-2 max-w-3xl text-sm text-muted">“{run.prompt}”</p>
            {run.instructions?.length ? (
              <div className="mt-3 max-w-3xl">
                <p className="text-xs font-semibold text-muted">Your instructions during the build</p>
                <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-sm text-fg-soft" aria-label="Your instructions during the build">
                  {run.instructions.map((item) => <li key={item.id}>{item.text}</li>)}
                </ol>
              </div>
            ) : null}
          </div>
        </div>
        <RunControls
          run={run}
          onStop={async () => {
            await postJson(`runs/${run.id}/stop`, {});
            await refresh();
          }}
          onContinue={async (message) => {
            await postJson(`runs/${run.id}/continue`, { message });
            await refresh();
          }}
          onInstruct={async (message) => {
            await postJson(`runs/${run.id}/instructions`, { message });
            await refresh();
          }}
          onAbandon={async (reason) => {
            const next = await postJson<RunDetail>(`runs/${run.id}/abandon`, { reason });
            setRun(next);
          }}
        />
      </header>

      <div className="panel grid gap-6 p-5 md:grid-cols-[1fr_auto] md:items-center">
        <StageTracker stage={run.stage} state={run.state} />
        <dl className="grid grid-cols-3 gap-6 text-sm md:border-l-2 md:border-dashed md:border-line-strong md:pl-6">
          <div>
            <dt className="text-xs text-muted">Tasks accepted</dt>
            <dd className="mt-0.5 tabular-nums text-fg">
              {run.tasks_accepted} of {run.tasks_total}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-muted">Cost</dt>
            <dd className="mt-0.5 tabular-nums text-fg">{formatUsd(run.usage.cost_usd, { complete: run.usage.cost_complete })}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted">Elapsed</dt>
            <dd className="mt-0.5 tabular-nums text-fg">{duration(run.created_at, elapsedEnd)}</dd>
          </div>
        </dl>
      </div>

      {error ? (
        <Notice tone="warn" title="Showing the last known state" role="status">
          {error}
        </Notice>
      ) : null}

      {run.state === "complete" ? <CompletionPanel run={run} githubEnabled={githubEnabled} onPublished={refresh} /> : null}

      {run.state === "cancelled" ? (
        <Notice tone="info" title="This run was closed">
          {run.explanation}
        </Notice>
      ) : null}

      {ATTENTION_STATES.has(run.state) ? (
        <Notice tone={run.state === "failed" ? "bad" : "warn"} title={run.label} role="status">
          <p>{run.explanation}</p>
          {managerMessage ? (
            <p className="mt-2 border-l-2 border-line-strong pl-3 text-fg-soft">
              <span className="text-xs text-muted">Cavman said: </span>
              {managerMessage}
            </p>
          ) : null}
          {run.state === "budget_reached" ? <p className="mt-2 text-xs text-muted">Raise the budget below, then continue the run.</p> : null}
        </Notice>
      ) : null}

      {pending.length ? (
        <Panel id="approvals" title={<span className="flex items-center gap-2"><ShieldAlert className="h-4 w-4 text-ember-deep" aria-hidden="true" /> Approval needed</span>} description="Cavman is paused until you decide.">
          <div className="space-y-4">
            {pending.map((approval) => (
              <ApprovalCard key={approval.id} approval={approval} onDecide={(decision, reason) => decide(approval, decision, reason)} />
            ))}
          </div>
        </Panel>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <div className="min-w-0 space-y-6">
          <Panel id="tasks" title={<span className="flex items-center gap-2"><Users className="h-4 w-4 text-glacier" aria-hidden="true" /> Agent activity</span>} description="Each task has one specialist, one deliverable and its own checks.">
            {run.tasks.length ? (
              <div className="grid gap-3 md:grid-cols-2">
                {run.tasks.map((task) => (
                  <TaskCard key={task.id} task={task} titles={titles} executing={executing} />
                ))}
              </div>
            ) : (
              <EmptyState title={executing ? "Cavman is planning" : "No tasks yet"}>
                {executing
                  ? "Tasks appear here as soon as the plan is recorded."
                  : "Cavman has not created a plan for this run."}
              </EmptyState>
            )}
            {run.criteria.length ? (
              <div className="mt-5 border-t border-line pt-4">
                <p className="eyebrow text-[0.62rem] text-muted">Success criteria</p>
                <ul className="mt-2 list-inside list-disc space-y-1 text-sm text-fg-soft">
                  {run.criteria.map((criterion) => (
                    <li key={criterion}>{criterion}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </Panel>

          <Panel id="checks" title={<span className="flex items-center gap-2"><FlaskConical className="h-4 w-4 text-glacier" aria-hidden="true" /> Validation &amp; tests</span>} description="Trusted results only — from sandboxed execution and independent review.">
            <ChecksPanel tasks={run.tasks} artifacts={run.artifacts} executing={executing} />
          </Panel>

          <Panel id="artifacts" title={<span className="flex items-center gap-2"><Layers className="h-4 w-4 text-glacier" aria-hidden="true" /> Artifacts</span>} description="Every submitted candidate, with its provenance.">
            <ArtifactsPanel runId={run.id} artifacts={run.artifacts} />
          </Panel>

          {decided.length ? (
            <Panel title="Decisions">
              <div className="space-y-3">
                {decided.map((approval) => (
                  <ApprovalCard key={approval.id} approval={approval} onDecide={async () => undefined} />
                ))}
              </div>
            </Panel>
          ) : null}
        </div>

        <div className="min-w-0 space-y-6">
          {run.failure_details.length || run.recovery.proposals.length ? (
            <Panel id="failures" title={<span className="flex items-center gap-2"><TriangleAlert className="h-4 w-4 text-bad" aria-hidden="true" /> Failures &amp; recovery</span>}>
              <FailuresPanel run={run} />
            </Panel>
          ) : null}
          <Panel id="activity" title={<span className="flex items-center gap-2"><Activity className="h-4 w-4 text-glacier" aria-hidden="true" /> Activity</span>}>
            <Timeline runId={run.id} events={run.timeline} />
          </Panel>
          <Panel id="usage" title={<span className="flex items-center gap-2"><Wallet className="h-4 w-4 text-glacier" aria-hidden="true" /> Usage &amp; cost</span>}>
            <UsagePanel
              usage={run.usage}
              canEdit={run.status === "active"}
              onBudget={async (value) => {
                await postJson(`runs/${run.id}/budget`, { budget_usd: value }, "PATCH");
                await refresh();
              }}
            />
          </Panel>
          {run.constraints.length ? (
            <Panel title={<span className="flex items-center gap-2"><Info className="h-4 w-4 text-muted" aria-hidden="true" /> Your settings</span>}>
              <ul className="space-y-1 text-sm text-fg-soft">
                {run.constraints.map((constraint) => (
                  <li key={constraint}>{constraint}</li>
                ))}
              </ul>
            </Panel>
          ) : null}
          <p className="text-xs text-faint">
            Run <span className="font-mono">{run.id}</span> · started {relativeTime(run.created_at)}
          </p>
        </div>
      </div>
    </div>
  );
}

function ConnectionBadge({ connection }: { connection: ReturnType<typeof useRun>["connection"] }) {
  if (connection === "live") {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-muted">
        <span className="h-1.5 w-1.5 rounded-full bg-ok animate-pulse-dot" aria-hidden="true" /> Live
      </span>
    );
  }
  if (connection === "reconnecting" || connection === "connecting") {
    return (
      <span role="status" className="inline-flex items-center gap-1.5 text-xs text-warn">
        <WifiOff className="h-3.5 w-3.5" aria-hidden="true" /> {connection === "connecting" ? "Connecting…" : "Reconnecting… your build keeps running"}
      </span>
    );
  }
  return null;
}
