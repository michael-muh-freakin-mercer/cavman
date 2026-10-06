import { GitBranch, Plus } from "lucide-react";
import type { Metadata } from "next";
import { ApiError } from "@/components/app/api-error";
import { PageHeader } from "@/components/app/page-header";
import { Pager, cursorParam } from "@/components/app/pager";
import { RunList } from "@/components/app/run-list";
import { ButtonLink } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/panel";
import { load } from "@/lib/load";
import { requireUser } from "@/lib/session";
import type { ProjectView } from "@/lib/types";

export const metadata: Metadata = { title: "Project" };

export default async function ProjectPage({ params, searchParams }: {
  params: Promise<{ projectId: string }>;
  searchParams: Promise<{ before?: string }>;
}) {
  const { projectId } = await params;
  const user = await requireUser(`/app/projects/${projectId}`);
  const cursor = cursorParam((await searchParams).before);
  const result = /^[0-9a-f]{32}$/.test(projectId)
    ? await load<ProjectView>(user.id, `projects/${projectId}?limit=25${cursor ? `&${cursor}` : ""}`)
    : ({ ok: false, status: 404, message: "Not found" } as const);
  if (!result.ok) {
    return (
      <div className="px-4 py-10 sm:px-8">
        <ApiError status={result.status} message={result.message} />
      </div>
    );
  }
  const project = result.data;
  const source = project.settings.source;
  return (
    <>
      <PageHeader
        eyebrow="Project"
        title={project.name}
        description={project.description}
        actions={<ButtonLink href={`/app/new?project=${project.id}`} variant="secondary"><Plus className="h-4 w-4" aria-hidden="true" /> New run in this project</ButtonLink>}
      />
      <div className="px-4 py-6 sm:px-8 sm:py-8">
        {source ? (
          <p className="mb-5 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-sm text-muted">
            <GitBranch className="h-4 w-4 text-glacier" aria-hidden="true" />
            Started from
            <a href={source.url} rel="noreferrer" target="_blank" className="text-fg-soft underline decoration-line-strong underline-offset-4 hover:text-fg">
              {source.url.replace("https://github.com/", "")}
            </a>
            at <span className="font-mono text-xs text-fg-soft">{source.commit.slice(0, 12)}</span>
            <span>· {source.files} files{source.dropped.length ? `, ${source.dropped.length} secret-looking files left out` : ""}</span>
          </p>
        ) : null}
        {project.runs?.length ? <RunList runs={project.runs} showProject={false} /> : <EmptyState title={cursor ? "No older runs" : "No runs in this project"} />}
        <Pager path={`/app/projects/${project.id}`} next={project.next ?? null} paged={Boolean(cursor)} />
      </div>
    </>
  );
}
