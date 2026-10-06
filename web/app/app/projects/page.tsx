import { ArrowRight, FolderKanban } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { ApiError } from "@/components/app/api-error";
import { PageHeader } from "@/components/app/page-header";
import { Pager, cursorParam } from "@/components/app/pager";
import { ButtonLink } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/panel";
import { StatusPill } from "@/components/ui/status";
import { relativeTime } from "@/lib/format";
import { load } from "@/lib/load";
import { RUN_TONE } from "@/lib/run-state";
import { requireUser } from "@/lib/session";
import type { ProjectView } from "@/lib/types";

export const metadata: Metadata = { title: "Projects" };

export default async function ProjectsPage({ searchParams }: { searchParams: Promise<{ before?: string }> }) {
  const user = await requireUser("/app/projects");
  const cursor = cursorParam((await searchParams).before);
  const result = await load<{ projects: ProjectView[]; next: string | null }>(user.id, `projects?limit=24${cursor ? `&${cursor}` : ""}`);
  return (
    <>
      <PageHeader eyebrow="Projects" title="Projects" description="Each project has its own repository and run history." />
      <div className="px-4 py-6 sm:px-8 sm:py-8">
        {!result.ok ? (
          <ApiError status={result.status} message={result.message} />
        ) : result.data.projects.length ? (
          <>
            <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {result.data.projects.map((project) => (
                <li key={project.id}>
                  <Link href={`/app/projects/${project.id}`} className="panel block h-full p-5 transition-colors hover:border-line-strong">
                    <div className="flex items-start justify-between gap-3">
                      <h2 className="text-base font-semibold text-fg">{project.name}</h2>
                      {project.latest_run ? <StatusPill tone={RUN_TONE[project.latest_run.state]}>{project.latest_run.label}</StatusPill> : null}
                    </div>
                    <p className="mt-2 line-clamp-2 text-sm text-muted">{project.description || "No description"}</p>
                    <p className="mt-4 text-xs text-faint">
                      {project.run_count} run{project.run_count === 1 ? "" : "s"} · updated {relativeTime(project.updated_at)}
                    </p>
                  </Link>
                </li>
              ))}
            </ul>
            <Pager path="/app/projects" next={result.data.next} paged={Boolean(cursor)} />
          </>
        ) : cursor ? (
          <EmptyState title="No older projects" action={<ButtonLink href="/app/projects" variant="secondary">Newest projects</ButtonLink>} />
        ) : (
          <EmptyState icon={<FolderKanban className="h-6 w-6" />} title="No projects yet" action={<ButtonLink href="/app/new">Start a build <ArrowRight className="h-4 w-4" aria-hidden="true" /></ButtonLink>}>
            A project is created for every new build.
          </EmptyState>
        )}
      </div>
    </>
  );
}
