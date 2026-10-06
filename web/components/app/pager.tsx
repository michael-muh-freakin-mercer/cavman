import { ArrowLeft, ArrowRight } from "lucide-react";
import Link from "next/link";

/** Newest-first paging: "Older" follows the API's cursor, "Newest" goes back to the first page. */
export function Pager({ path, next, paged }: { path: string; next: string | null; paged: boolean }) {
  if (!next && !paged) return null;
  const link = "inline-flex items-center gap-1.5 text-sm font-medium text-glacier underline-offset-4 hover:underline";
  return (
    <nav aria-label="Pages" className="mt-6 flex items-center justify-between">
      {paged ? <Link href={path} className={link}><ArrowLeft className="h-4 w-4" aria-hidden="true" /> Newest</Link> : <span />}
      {next ? <Link href={`${path}?before=${encodeURIComponent(next)}`} className={link}>Older <ArrowRight className="h-4 w-4" aria-hidden="true" /></Link> : null}
    </nav>
  );
}

/** A cursor from the address bar, passed on only if it looks like one. */
export function cursorParam(before: string | undefined): string {
  return before && /^[A-Za-z0-9_-]+={0,2}$/.test(before) ? `before=${encodeURIComponent(before)}` : "";
}
