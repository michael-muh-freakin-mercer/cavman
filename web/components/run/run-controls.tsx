"use client";

import { Play, Square, XCircle } from "lucide-react";
import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { canContinue, isExecuting } from "@/lib/run-state";
import type { RunDetail } from "@/lib/types";

export function RunControls({
  run,
  onStop,
  onContinue,
  onAbandon,
}: {
  run: RunDetail;
  onStop: () => Promise<void>;
  onContinue: (message: string) => Promise<void>;
  onAbandon: (reason: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const continueDialog = useRef<HTMLDialogElement>(null);
  const closeDialog = useRef<HTMLDialogElement>(null);
  const [reason, setReason] = useState("");
  const executing = isExecuting(run.state);

  async function act(name: string, action: () => Promise<void>) {
    setBusy(name);
    setError(null);
    try {
      await action();
      return true;
    } catch (reasonError) {
      setError(reasonError instanceof Error ? reasonError.message : "That did not work.");
      return false;
    } finally {
      setBusy(null);
    }
  }

  if (run.status !== "active") return null;

  return (
    <div className="flex flex-col items-start gap-2 sm:items-end">
      <div className="flex flex-wrap gap-2">
        {executing && run.state !== "stopping" ? (
          <Button variant="secondary" onClick={() => act("stop", onStop)} disabled={busy !== null}>
            <Square className="h-4 w-4" aria-hidden="true" /> {busy === "stop" ? "Stopping…" : "Stop"}
          </Button>
        ) : null}
        {canContinue(run.state, run.status) ? (
          <Button onClick={() => continueDialog.current?.showModal()} disabled={busy !== null}>
            <Play className="h-4 w-4" aria-hidden="true" /> Continue
          </Button>
        ) : null}
        {!executing ? (
          <Button variant="ghost" onClick={() => closeDialog.current?.showModal()} disabled={busy !== null}>
            <XCircle className="h-4 w-4" aria-hidden="true" /> Close run
          </Button>
        ) : null}
      </div>
      {error ? <p role="alert" className="max-w-sm text-sm text-bad">{error}</p> : null}

      <dialog ref={continueDialog} aria-labelledby="continue-title" className="m-auto w-[min(520px,92vw)] rounded-xl border-2 border-ink bg-surface p-6 text-fg shadow-[6px_6px_0_0_var(--color-ink)] backdrop:bg-ink/50">
        <form
          method="dialog"
          onSubmit={async (event) => {
            event.preventDefault();
            if (await act("continue", () => onContinue(""))) continueDialog.current?.close();
          }}
        >
          <h2 id="continue-title" className="text-lg font-semibold">Continue this build</h2>
          <p className="mt-1 text-sm text-muted">Cavman picks up exactly where it stopped, from the saved state.</p>
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="ghost" onClick={() => continueDialog.current?.close()}>Cancel</Button>
            <Button type="submit" disabled={busy !== null}>{busy === "continue" ? "Starting…" : "Continue"}</Button>
          </div>
        </form>
      </dialog>

      <dialog ref={closeDialog} aria-labelledby="close-title" className="m-auto w-[min(520px,92vw)] rounded-xl border-2 border-ink bg-surface p-6 text-fg shadow-[6px_6px_0_0_var(--color-ink)] backdrop:bg-ink/50">
        <form
          method="dialog"
          onSubmit={async (event) => {
            event.preventDefault();
            if (await act("abandon", () => onAbandon(reason))) closeDialog.current?.close();
          }}
        >
          <h2 id="close-title" className="text-lg font-semibold">Close this run?</h2>
          <p className="mt-1 text-sm text-muted">
            Closing stops the build for good. Everything recorded so far stays visible. Cavman refuses while a decision is still pending.
          </p>
          <label className="mt-4 block text-xs text-muted" htmlFor="close-reason">Reason</label>
          <input id="close-reason" required minLength={3} value={reason} onChange={(e) => setReason(e.target.value)} className="mt-1 h-10 w-full rounded-md border-2 border-line-strong bg-surface px-3 text-sm focus:border-ink focus:outline-none" />
          {error ? <p role="alert" className="mt-2 text-sm text-bad">{error}</p> : null}
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="ghost" onClick={() => closeDialog.current?.close()}>Keep it</Button>
            <Button variant="danger" type="submit" disabled={busy !== null}>{busy === "abandon" ? "Closing…" : "Close run"}</Button>
          </div>
        </form>
      </dialog>
    </div>
  );
}
