"use client";

import { LoaderCircle } from "lucide-react";
import { useId, useState } from "react";
import { twoFactor } from "@/lib/auth-client";

/** The second sign-in step: a code from the authenticator app, or a backup code. */
export function TwoFactorChallenge({ onDone }: { onDone: () => void }) {
  const [backup, setBackup] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ids = { code: useId(), trust: useId(), error: useId() };
  const field = "mt-1.5 block h-11 w-full rounded-md border-2 border-line-strong bg-surface px-3 font-mono tracking-widest text-fg focus:border-ink focus:outline-none";

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const code = String(data.get("code") ?? "").replace(/\s+/g, "");
    const trustDevice = data.get("trust") === "on";
    setBusy(true);
    setError(null);
    const result = backup
      ? await twoFactor.verifyBackupCode({ code, trustDevice })
      : await twoFactor.verifyTotp({ code, trustDevice });
    if (result.error) {
      setBusy(false);
      setError(result.error.message ?? "That code did not work. Try again.");
      return;
    }
    onDone();
  }

  return (
    <form method="post" onSubmit={submit} className="space-y-4" aria-describedby={error ? ids.error : undefined}>
      <p className="text-sm text-fg-soft">
        {backup
          ? "Enter one of the backup codes you saved when you turned on two-factor sign-in. Each code works once."
          : "Enter the 6-digit code from your authenticator app."}
      </p>
      <div>
        <label htmlFor={ids.code} className="text-sm text-fg-soft">{backup ? "Backup code" : "Authentication code"}</label>
        <input id={ids.code} key={backup ? "backup" : "totp"} name="code" required autoFocus autoComplete="one-time-code"
          inputMode={backup ? "text" : "numeric"} pattern={backup ? undefined : "[0-9 ]{6,7}"} className={field} />
      </div>
      <div className="flex items-center gap-2">
        <input id={ids.trust} name="trust" type="checkbox" className="h-4 w-4 accent-ink" />
        <label htmlFor={ids.trust} className="text-sm text-fg-soft">Trust this device for 30 days</label>
      </div>
      {error ? <p id={ids.error} role="alert" className="text-sm text-bad">{error}</p> : null}
      <button type="submit" disabled={busy}
        className="inline-flex h-11 w-full items-center justify-center gap-2 rounded-md border-2 border-ink bg-ember text-sm font-bold text-ink shadow-[3px_3px_0_0_var(--color-ink)] hover:bg-ember-hot disabled:opacity-60">
        {busy ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" /> : null}
        Verify
      </button>
      <button type="button" onClick={() => { setBackup(!backup); setError(null); }} className="w-full text-center text-xs text-muted hover:text-fg">
        {backup ? "Use your authenticator app instead" : "Lost your device? Use a backup code"}
      </button>
    </form>
  );
}
