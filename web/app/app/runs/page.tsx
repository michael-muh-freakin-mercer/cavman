import { ArrowRight, ListChecks } from "lucide-react";
import type { Metadata } from "next";
import { ApiError } from "@/components/app/api-error";
import { PageHeader } from "@/components/app/page-header";
import { Pager, cursorParam } from "@/components/app/pager";
import { RunList } from "@/components/app/run-list";
import { ButtonLink } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/panel";
import { load } from "@/lib/load";
import { requireUser } from "@/lib/session";
import type { RunSummary } from "@/lib/types";

export const metadata: Metadata = { title: "Runs" };

export default async function RunsPage({ searchParams }: { searchParams: Promise<{ before?: string }> }) {
  const user = await requireUser("/app/runs");
  const cursor = cursorParam((await searchParams).before);
  const result = await load<{ runs: RunSummary[]; next: string | null }>(user.id, `runs?limit=25${cursor ? `&${cursor}` : ""}`);
  return (
    <>
      <PageHeader eyebrow="Runs" title="All runs" description="Every build you have started, newest first." />
      <div className="px-4 py-6 sm:px-8 sm:py-8">
        {!result.ok ? (
          <ApiError status={result.status} message={result.message} />
        ) : result.data.runs.length ? (
          <>
            <RunList runs={result.data.runs} />
            <Pager path="/app/runs" next={result.data.next} paged={Boolean(cursor)} />
          </>
        ) : cursor ? (
          <EmptyState title="No older runs" action={<ButtonLink href="/app/runs" variant="secondary">Newest runs</ButtonLink>} />
        ) : (
          <EmptyState icon={<ListChecks className="h-6 w-6" />} title="No runs yet" action={<ButtonLink href="/app/new">Start a build <ArrowRight className="h-4 w-4" aria-hidden="true" /></ButtonLink>}>
            Runs appear here as soon as you ask Cavman to build something.
          </EmptyState>
        )}
      </div>
    </>
  );
}
