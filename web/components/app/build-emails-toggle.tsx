"use client";

import { useId, useState } from "react";

export function BuildEmailsToggle({ initial }: { initial: boolean }) {
  const [enabled, setEnabled] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = useId();

  async function change(next: boolean) {
    setBusy(true);
    setError(null);
    setEnabled(next);
    const response = await fetch("/api/account/notifications", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ buildEmails: next }),
    }).catch(() => null);
    setBusy(false);
    if (!response?.ok) {
      setEnabled(!next);
      setError("That did not save. Try again.");
    }
  }

  return (
    <div>
      <label htmlFor={id} className="flex items-start gap-3 text-sm">
        <input id={id} type="checkbox" className="mt-0.5 h-4 w-4 accent-current" checked={enabled} disabled={busy}
          onChange={(event) => change(event.target.checked)} />
        <span>
          <span className="block text-fg">Email me about my builds</span>
          <span className="block text-xs text-muted">When a build is ready, needs your decision, or stops before it is done.</span>
        </span>
      </label>
      {error ? <p role="alert" className="mt-2 text-xs text-bad">{error}</p> : null}
    </div>
  );
}
