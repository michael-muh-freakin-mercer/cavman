import type { Metadata } from "next";
import { NewBuildForm } from "@/components/app/new-build-form";
import { LiveCavman } from "@/components/brand/live-cavman";
import { githubEnabled } from "@/lib/auth";
import { load } from "@/lib/load";
import { requireUser } from "@/lib/session";
import type { EstimateView, ProjectView, SystemView } from "@/lib/types";

export const metadata: Metadata = { title: "New build" };

export default async function NewBuildPage({ searchParams }: { searchParams: Promise<{ prompt?: string; project?: string; repository?: string }> }) {
  const { prompt, project, repository } = await searchParams;
  const user = await requireUser("/app/new");
  const [system, estimate, projectResult] = await Promise.all([
    load<SystemView>(user.id, "system"),
    load<EstimateView>(user.id, "estimate"),
    project && /^[0-9a-f]{32}$/.test(project) ? load<ProjectView>(user.id, `projects/${project}`) : Promise.resolve(null),
  ]);
  const disabledReason = !system.ok
    ? "Cavman is temporarily unavailable, so new builds cannot start right now. Try again in a moment."
    : !system.data.provider.configured
      ? "Cavman's model provider is not configured on the server yet. An operator needs to add a provider key before builds can run."
      : null;
  return (
    <div className="stone dig-grid min-h-[calc(100dvh-3.5rem)] px-4 py-10 sm:px-8 sm:py-16 lg:min-h-dvh">
      <div className="mx-auto max-w-3xl text-center">
        <LiveCavman className="mx-auto mb-4 h-14 w-14" />
        <p className="eyebrow text-muted">New build</p>
        <h1 className="display text-balance mt-4 text-2xl leading-tight text-fg sm:text-4xl">What do you want to build?</h1>
        <p className="mt-4 text-fg-soft">Plain English is fine. Cavman infers sensible defaults and asks only when it genuinely needs you.</p>
      </div>
      <div className="mt-10">
        <NewBuildForm
          initialPrompt={(prompt ?? "").slice(0, 8000)}
          projectId={projectResult && projectResult.ok ? projectResult.data.id : null}
          projectName={projectResult && projectResult.ok ? projectResult.data.name : null}
          defaultBudget={system.ok ? system.data.budget.default_usd : 5}
          maxBudget={system.ok ? system.data.budget.max_usd : 100}
          disabledReason={disabledReason}
          modes={system.ok ? system.data.model_modes.filter((m) => m.available).map((m) => m.mode) : ["automatic"]}
          estimate={estimate.ok ? estimate.data : null}
          githubEnabled={githubEnabled}
          initialRepository={(repository ?? "").slice(0, 300)}
        />
      </div>
    </div>
  );
}
