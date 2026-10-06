"use client";

import { LoaderCircle } from "lucide-react";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import { renderSVG } from "uqr";
import { Button } from "@/components/ui/button";
import { twoFactor } from "@/lib/auth-client";
import { useHydrated } from "@/lib/use-hydrated";

type Step =
  | { kind: "idle" }
  | { kind: "password"; action: "enable" | "disable" | "codes" }
  | { kind: "setup"; uri: string; codes: string[] }
  | { kind: "codes"; codes: string[] };

const input = "mt-1 h-10 w-full rounded-md border-2 border-line-strong bg-surface px-3 text-sm focus:border-ink focus:outline-none";

export function secretFromUri(uri: string): string {
  try {
    return new URL(uri).searchParams.get("secret") ?? "";
  } catch {
    return "";
  }
}

function BackupCodes({ codes }: { codes: string[] }) {
  return (
    <div>
      <p className="text-xs text-muted">Backup codes. Each signs you in once if you lose your device. Save them somewhere safe now: they are not shown again.</p>
      <ul className="mt-2 grid grid-cols-2 gap-1 rounded-lg border border-line p-3 font-mono text-sm text-fg" aria-label="Backup codes">
        {codes.map((code) => <li key={code}>{code}</li>)}
      </ul>
    </div>
  );
}

/** Optional two-factor sign-in with an authenticator app (TOTP) and backup codes. */
export function TwoFactorSettings({ enabled, hasPassword }: { enabled: boolean; hasPassword: boolean }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const [step, setStep] = useState<Step>({ kind: "idle" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const ids = { password: useId(), code: useId() };

  if (!hasPassword) {
    return (
      <p className="text-sm text-muted">
        Two-factor sign-in protects signing in with a password. You sign in with GitHub, so turn on two-factor authentication in your GitHub account.
      </p>
    );
  }

  function start(action: "enable" | "disable" | "codes") {
    setStep({ kind: "password", action });
    setError(null);
    setNotice(null);
  }

  async function withPassword(event: React.FormEvent<HTMLFormElement>, action: "enable" | "disable" | "codes") {
    event.preventDefault();
    const password = String(new FormData(event.currentTarget).get("password") ?? "");
    setBusy(true);
    setError(null);
    if (action === "enable") {
      const result = await twoFactor.enable({ password });
      setBusy(false);
      if (result.error || !result.data || !("totpURI" in result.data)) {
        return setError(result.error?.message ?? "Two-factor sign-in could not be turned on.");
      }
      setStep({ kind: "setup", uri: result.data.totpURI, codes: result.data.backupCodes });
    } else if (action === "disable") {
      const result = await twoFactor.disable({ password });
      setBusy(false);
      if (result.error) return setError(result.error.message ?? "Two-factor sign-in could not be turned off.");
      setStep({ kind: "idle" });
      setNotice("Two-factor sign-in is off.");
      router.refresh();
    } else {
      const result = await twoFactor.generateBackupCodes({ password });
      setBusy(false);
      if (result.error || !result.data) return setError(result.error?.message ?? "New backup codes could not be made.");
      setStep({ kind: "codes", codes: result.data.backupCodes });
    }
  }

  async function confirm(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const code = String(new FormData(event.currentTarget).get("code") ?? "").replace(/\s+/g, "");
    setBusy(true);
    setError(null);
    const result = await twoFactor.verifyTotp({ code });
    setBusy(false);
    if (result.error) return setError(result.error.message ?? "That code did not work. Check the time on your device and try again.");
    setStep({ kind: "idle" });
    setNotice("Two-factor sign-in is on. You will be asked for a code when you sign in with your password.");
    router.refresh();
  }

  const spinner = busy ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : null;
  const label = { enable: "Turn on", disable: "Turn off", codes: "Make new codes" };

  return (
    <div className="space-y-3">
      <p className="text-sm text-fg-soft">
        {enabled ? "On. Signing in with your password also asks for a code from your authenticator app."
          : "Off. Turn it on to also ask for a code from an authenticator app when you sign in."}
      </p>

      {step.kind === "idle" ? (
        <div className="flex flex-wrap gap-2">
          {enabled ? (
            <>
              <Button size="sm" variant="secondary" disabled={!hydrated} onClick={() => start("codes")}>New backup codes</Button>
              <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => start("disable")}>Turn off two-factor</Button>
            </>
          ) : (
            <Button size="sm" disabled={!hydrated} onClick={() => start("enable")}>Turn on two-factor</Button>
          )}
        </div>
      ) : null}

      {step.kind === "password" ? (
        <form method="post" className="space-y-3" onSubmit={(event) => withPassword(event, step.action)}>
          <div>
            <label htmlFor={ids.password} className="text-xs text-muted">Your password</label>
            <input id={ids.password} name="password" type="password" required autoComplete="current-password" autoFocus className={input} />
          </div>
          <div className="flex gap-2">
            <Button type="submit" size="sm" disabled={busy}>{spinner}{label[step.action]}</Button>
            <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setStep({ kind: "idle" })}>Cancel</Button>
          </div>
        </form>
      ) : null}

      {step.kind === "setup" ? (
        <div className="space-y-4">
          <p className="text-sm text-fg-soft">Scan this with an authenticator app (1Password, Google Authenticator, Authy and others), or type the key.</p>
          {/* eslint-disable-next-line @next/next/no-img-element -- a generated data: SVG, not a remote image */}
          <img src={`data:image/svg+xml;utf8,${encodeURIComponent(renderSVG(step.uri))}`} alt="QR code for your authenticator app"
            width={176} height={176} className="rounded-lg border border-line bg-white p-2" />
          <p className="text-xs text-muted">Key: <code className="select-all break-all font-mono text-fg" data-testid="totp-secret">{secretFromUri(step.uri)}</code></p>
          <BackupCodes codes={step.codes} />
          <form method="post" className="space-y-3" onSubmit={confirm}>
            <div>
              <label htmlFor={ids.code} className="text-xs text-muted">Code from the app, to confirm</label>
              <input id={ids.code} name="code" required inputMode="numeric" autoComplete="one-time-code" className={`${input} font-mono tracking-widest`} />
            </div>
            <div className="flex gap-2">
              <Button type="submit" size="sm" disabled={busy}>{spinner}Confirm and turn on</Button>
              <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setStep({ kind: "idle" })}>Cancel</Button>
            </div>
          </form>
        </div>
      ) : null}

      {step.kind === "codes" ? (
        <div className="space-y-3">
          <BackupCodes codes={step.codes} />
          <p className="text-xs text-muted">Your old backup codes no longer work.</p>
          <Button size="sm" variant="secondary" onClick={() => setStep({ kind: "idle" })}>Done</Button>
        </div>
      ) : null}

      {error ? <p role="alert" className="text-sm text-bad">{error}</p> : null}
      {notice ? <p role="status" className="text-sm text-ok">{notice}</p> : null}
    </div>
  );
}
