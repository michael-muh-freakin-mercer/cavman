"use client";

import { LoaderCircle } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import { TurnstileWidget } from "@/components/auth/turnstile";
import { TwoFactorChallenge } from "@/components/auth/two-factor-challenge";
import { GithubIcon } from "@/components/brand/github-icon";
import { signIn, signUp } from "@/lib/auth-client";
import { safeNext } from "@/lib/prompt-storage";
import { useHydrated } from "@/lib/use-hydrated";


export function AuthForm({
  mode,
  next,
  githubEnabled,
  captchaSiteKey = null,
}: {
  mode: "sign-in" | "sign-up";
  next: string | null;
  githubEnabled: boolean;
  captchaSiteKey?: string | null;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const destination = safeNext(next);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const ids = { name: useId(), email: useId(), password: useId(), error: useId() };
  const needsCaptcha = mode === "sign-up" && Boolean(captchaSiteKey);
  const [captchaToken, setCaptchaToken] = useState<string | null>(null);
  const [captchaRound, setCaptchaRound] = useState(0);
  const [challenge, setChallenge] = useState(false);

  function finish() {
    router.push(destination);
    router.refresh();
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const email = String(data.get("email") ?? "").trim();
    const password = String(data.get("password") ?? "");
    const name = String(data.get("name") ?? "").trim() || email.split("@")[0];
    setBusy(true);
    setError(null);
    const fetchOptions = captchaToken ? { headers: { "x-captcha-response": captchaToken } } : undefined;
    const result =
      mode === "sign-up"
        ? await signUp.email({ email, password, name, fetchOptions })
        : await signIn.email({ email, password });
    // Tokens are single-use: any further attempt needs a fresh check.
    if (needsCaptcha) setCaptchaRound((round) => round + 1);
    if (result.error) {
      setError(
        result.error.code === "EMAIL_NOT_VERIFIED"
          ? "Verify your email first: we sent you a link. Check your inbox, then sign in."
          : (result.error.message ?? "That did not work. Check your details and try again."),
      );
      setBusy(false);
      return;
    }
    if (mode === "sign-in" && result.data && "twoFactorRedirect" in result.data && result.data.twoFactorRedirect) {
      // Password accepted; the account also needs a second factor before a session exists.
      setChallenge(true);
      setBusy(false);
      return;
    }
    if (mode === "sign-up" && result.data && !("token" in result.data && result.data.token)) {
      // Email verification is required on this server: no session until verified.
      setNotice(`Check ${email} for a link to verify your address, then sign in.`);
      setBusy(false);
      return;
    }
    finish();
  }

  async function github() {
    setBusy(true);
    await signIn.social({ provider: "github", callbackURL: destination });
  }

  const other = mode === "sign-in" ? "/sign-up" : "/sign-in";
  if (challenge) return <TwoFactorChallenge onDone={finish} />;

  const field = "mt-1.5 block h-11 w-full rounded-md border-2 border-line-strong bg-surface px-3 text-fg placeholder:text-faint focus:border-ink focus:outline-none";

  return (
    <div>
      {githubEnabled ? (
        <>
          <button
            type="button"
            onClick={github}
            disabled={busy}
            className="inline-flex h-11 w-full items-center justify-center gap-2 rounded-lg border border-line-strong bg-surface-2 text-sm font-medium text-fg hover:bg-surface-3 disabled:opacity-50"
          >
            <GithubIcon /> Continue with GitHub
          </button>
          <div className="my-6 flex items-center gap-3 text-xs text-faint" aria-hidden="true">
            <span className="h-px flex-1 bg-line" /> or with email <span className="h-px flex-1 bg-line" />
          </div>
        </>
      ) : null}
      <form method="post" onSubmit={submit} className="space-y-4" aria-describedby={error ? ids.error : undefined}>
        {mode === "sign-up" ? (
          <div>
            <label htmlFor={ids.name} className="text-sm text-fg-soft">Name</label>
            <input id={ids.name} name="name" autoComplete="name" className={field} placeholder="Your name" />
          </div>
        ) : null}
        <div>
          <label htmlFor={ids.email} className="text-sm text-fg-soft">Email</label>
          <input id={ids.email} name="email" type="email" required autoComplete="email" className={field} placeholder="you@example.com" />
        </div>
        <div>
          <div className="flex items-baseline justify-between">
            <label htmlFor={ids.password} className="text-sm text-fg-soft">Password</label>
            {mode === "sign-in" ? (
              <Link href="/forgot-password" className="text-xs text-muted hover:text-fg">Forgot password?</Link>
            ) : null}
          </div>
          <input
            id={ids.password}
            name="password"
            type="password"
            required
            minLength={10}
            autoComplete={mode === "sign-up" ? "new-password" : "current-password"}
            className={field}
            placeholder={mode === "sign-up" ? "At least 10 characters" : ""}
          />
        </div>
        {needsCaptcha && captchaSiteKey ? (
          <TurnstileWidget siteKey={captchaSiteKey} onToken={setCaptchaToken} resetKey={captchaRound} />
        ) : null}
        {error ? (
          <p id={ids.error} role="alert" className="text-sm text-bad">
            {error}
          </p>
        ) : null}
        {notice ? (
          <p role="status" className="rounded-lg border border-ok/30 bg-ok/5 px-3 py-2 text-sm text-ok">
            {notice}
          </p>
        ) : null}
        <button
          type="submit"
          disabled={busy || !hydrated || (needsCaptcha && !captchaToken)}
          className="inline-flex h-11 w-full items-center justify-center gap-2 rounded-md border-2 border-ink bg-ember text-sm font-bold text-ink shadow-[3px_3px_0_0_var(--color-ink)] active:translate-x-[2px] active:translate-y-[2px] active:shadow-[1px_1px_0_0_var(--color-ink)] hover:bg-ember-hot disabled:opacity-60"
        >
          {busy ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" /> : null}
          {mode === "sign-up" ? "Create account" : "Sign in"}
        </button>
        {mode === "sign-up" ? (
          <p className="text-center text-xs text-muted">
            By creating an account you agree to the{" "}
            <Link href="/terms" className="underline-offset-4 hover:text-fg hover:underline">Terms</Link>,{" "}
            <Link href="/privacy" className="underline-offset-4 hover:text-fg hover:underline">Privacy Policy</Link> and{" "}
            <Link href="/acceptable-use" className="underline-offset-4 hover:text-fg hover:underline">Acceptable Use Policy</Link>.
          </p>
        ) : null}
      </form>
      <p className="mt-6 text-center text-sm text-muted">
        {mode === "sign-in" ? "New to Cavman? " : "Already have an account? "}
        <Link href={`${other}${next ? `?next=${encodeURIComponent(next)}` : ""}`} className="font-medium text-glacier underline-offset-2 hover:underline">
          {mode === "sign-in" ? "Create an account" : "Sign in"}
        </Link>
      </p>
    </div>
  );
}
