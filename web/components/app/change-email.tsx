"use client";

import { LoaderCircle } from "lucide-react";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { changeEmail } from "@/lib/auth-client";
import { useHydrated } from "@/lib/use-hydrated";

/** Change the account's email: the current address approves it (when verified), then the new one is verified. */
export function ChangeEmail({ email, verified }: { email: string; verified: boolean }) {
  const hydrated = useHydrated();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const id = useId();

  if (!open) {
    return (
      <div className="space-y-2">
        <Button size="sm" variant="secondary" disabled={!hydrated} onClick={() => { setOpen(true); setNotice(null); }}>Change email</Button>
        {notice ? <p role="status" className="text-sm text-ok">{notice}</p> : null}
      </div>
    );
  }
  return (
    <form
      method="post"
      className="space-y-3"
      onSubmit={async (event) => {
        event.preventDefault();
        const newEmail = String(new FormData(event.currentTarget).get("email") ?? "").trim();
        setBusy(true);
        setError(null);
        const result = await changeEmail({ newEmail, callbackURL: "/app/settings?email=changed" });
        setBusy(false);
        if (result.error) {
          setError(result.error.message ?? "The email could not be changed. Try again.");
          return;
        }
        setOpen(false);
        setNotice(verified
          ? `We sent a link to ${email}. Approve the change there, then confirm it from ${newEmail}.`
          : `We sent a link to ${newEmail}. Open it to confirm the change.`);
      }}
    >
      <div>
        <label htmlFor={id} className="text-xs text-muted">New email</label>
        <input id={id} name="email" type="email" required autoComplete="email" autoFocus
          className="mt-1 h-10 w-full rounded-md border-2 border-line-strong bg-surface px-3 text-sm focus:border-ink focus:outline-none" />
      </div>
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy || !hydrated}>
          {busy ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : null}
          Send confirmation
        </Button>
        <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setOpen(false)}>Cancel</Button>
      </div>
      {error ? <p role="alert" className="text-sm text-bad">{error}</p> : null}
    </form>
  );
}
