import type { Metadata } from "next";
import { headers } from "next/headers";
import { AccountData } from "@/components/app/account-data";
import { BuildEmailsToggle } from "@/components/app/build-emails-toggle";
import { ChangeEmail } from "@/components/app/change-email";
import { TwoFactorSettings } from "@/components/app/two-factor-settings";
import { SecurityPanel } from "@/components/app/security-panel";
import { ApiError } from "@/components/app/api-error";
import { PageHeader } from "@/components/app/page-header";
import { Panel } from "@/components/ui/panel";
import { StatusPill } from "@/components/ui/status";
import { auth, githubEnabled } from "@/lib/auth";
import { describeDevice } from "@/lib/devices";
import { formatUsd } from "@/lib/format";
import { load } from "@/lib/load";
import { requireUser } from "@/lib/session";
import type { AccountSpending, SystemView } from "@/lib/types";

export const metadata: Metadata = { title: "Settings" };

const MODES: Record<string, { name: string; body: string }> = {
  automatic: { name: "Automatic", body: "Cavman uses the models configured on the server." },
  budget: { name: "Budget", body: "Economical models for planning and specialist work." },
  balanced: { name: "Balanced", body: "A middle ground between cost and quality." },
  quality: { name: "Maximum Quality", body: "The strongest configured models, regardless of cost." },
};

export default async function SettingsPage({ searchParams }: { searchParams: Promise<{ email?: string }> }) {
  const user = await requireUser("/app/settings");
  const { email: emailChange } = await searchParams;
  const requestHeaders = await headers();
  const [system, account, signIns, sessions, current] = await Promise.all([
    load<SystemView>(user.id, "system"),
    load<{ spending: AccountSpending }>(user.id, "account"),
    auth.api.listUserAccounts({ headers: requestHeaders }),
    auth.api.listSessions({ headers: requestHeaders }),
    auth.api.getSession({ headers: requestHeaders }),
  ]);
  // Only ids and descriptions reach the browser; session tokens stay on the server.
  const devices = sessions
    .map((item) => ({ id: item.id, device: describeDevice(item.userAgent), createdAt: new Date(item.createdAt).toISOString(),
      current: item.id === current?.session.id }))
    .sort((a, b) => Number(b.current) - Number(a.current) || b.createdAt.localeCompare(a.createdAt));
  const hasPassword = signIns.some((item) => item.providerId === "credential");
  return (
    <>
      <PageHeader eyebrow="Settings" title="Settings" />
      <div className="grid gap-6 px-4 py-6 sm:px-8 sm:py-8 xl:grid-cols-2">
        <Panel title="Account">
          <dl className="space-y-3 text-sm">
            <div><dt className="text-xs text-muted">Name</dt><dd className="text-fg">{user.name}</dd></div>
            <div><dt className="text-xs text-muted">Email</dt><dd className="text-fg">{user.email}</dd></div>
          </dl>
          {emailChange === "changed" ? (
            <p role="status" className="mt-3 text-sm text-ok">Your email address was changed.</p>
          ) : null}
          <div className="mt-4"><ChangeEmail email={user.email} verified={user.emailVerified} /></div>
        </Panel>
        <Panel title="Email" description="Builds can take a while. Cavman can tell you when one is worth coming back for.">
          <BuildEmailsToggle initial={current?.user.buildEmails !== false} />
        </Panel>
        <Panel title="GitHub" description="Cavman never pushes or creates repositories without your explicit approval.">
          <p className="text-sm text-fg-soft">
            {githubEnabled
              ? "GitHub sign-in is available. It grants Cavman your public profile and email only."
              : "GitHub sign-in is not configured on this server."}
          </p>
          <p className="mt-3 text-sm text-muted">
            {githubEnabled
              ? "From a completed run, you can publish the verified project to a new repository. Cavman asks for repository access only at that moment and pushes only after you confirm the exact name and visibility."
              : "Publishing to GitHub needs GitHub sign-in to be configured by an operator. Download the verified project from a completed run instead."}
          </p>
        </Panel>
        {system.ok ? (
          <>
            <Panel title="Models" description="Routing happens on the server. Pick a mode per build in its advanced settings.">
              <ul className="space-y-3">
                {system.data.model_modes.map((mode) => (
                  <li key={mode.mode} className="flex items-start justify-between gap-4 rounded-lg border border-line p-3">
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-fg">{MODES[mode.mode]?.name ?? mode.mode}</p>
                      <p className="text-xs text-muted">{MODES[mode.mode]?.body}</p>
                      {mode.available && mode.worker_model ? (
                        <p className="mt-1 truncate font-mono text-[0.7rem] text-faint">{mode.manager_model} · {mode.worker_model}</p>
                      ) : null}
                    </div>
                    <StatusPill tone={mode.available ? "ok" : "neutral"}>{mode.available ? "Available" : "Not configured"}</StatusPill>
                  </li>
                ))}
              </ul>
              <dl className="mt-4 space-y-2 border-t border-line pt-4 text-xs">
                <div className="flex justify-between gap-3"><dt className="text-muted">Provider</dt><dd className="text-fg-soft">{system.data.provider.configured ? system.data.provider.provider : "Not configured"}</dd></div>
                {system.data.provider.manager_model ? <div className="flex justify-between gap-3"><dt className="text-muted">Manager model</dt><dd className="font-mono text-fg-soft">{system.data.provider.manager_model}</dd></div> : null}
                {system.data.provider.worker_model ? <div className="flex justify-between gap-3"><dt className="text-muted">Specialist model</dt><dd className="font-mono text-fg-soft">{system.data.provider.worker_model}</dd></div> : null}
              </dl>
            </Panel>
            <Panel title="Spending" description="Every run gets a ceiling, and your account has a monthly limit.">
              {account.ok ? (
                <div className="mb-5 rounded-lg border border-line p-4">
                  <div className="flex items-baseline justify-between">
                    <p className="text-sm text-fg">This month</p>
                    {account.data.spending.exhausted ? (
                      <StatusPill tone="bad">Limit reached</StatusPill>
                    ) : account.data.spending.warning ? (
                      <StatusPill tone="warn">Near limit</StatusPill>
                    ) : null}
                  </div>
                  <p className="mt-2 text-2xl font-semibold tabular-nums text-fg">
                    {formatUsd(account.data.spending.spent_usd, { complete: account.data.spending.cost_complete })}
                    <span className="text-sm font-normal text-muted"> of {formatUsd(account.data.spending.limit_usd)}</span>
                  </p>
                  <p className="mt-1 text-xs text-muted">
                    {account.data.spending.model_calls} of {account.data.spending.max_model_calls} model calls
                    {account.data.spending.cost_complete ? "" : ` · ${account.data.spending.calls_without_cost} calls reported no cost`}
                  </p>
                </div>
              ) : null}
              <dl className="space-y-2 text-sm">
                <div className="flex justify-between"><dt className="text-muted">Default budget per run</dt><dd className="tabular-nums text-fg">{formatUsd(system.data.budget.default_usd)}</dd></div>
                <div className="flex justify-between"><dt className="text-muted">Maximum budget per run</dt><dd className="tabular-nums text-fg">{formatUsd(system.data.budget.max_usd)}</dd></div>
                <div className="flex justify-between"><dt className="text-muted">Model-call limit per run</dt><dd className="tabular-nums text-fg">{system.data.budget.default_max_model_calls}</dd></div>
                <div className="flex justify-between"><dt className="text-muted">Warning at</dt><dd className="tabular-nums text-fg">{Math.round(system.data.budget.warning_ratio * 100)}%</dd></div>
              </dl>
            </Panel>
            <Panel title="Sandbox" description="Generated code runs isolated from Cavman and your secrets.">
              <div className="flex items-center justify-between text-sm">
                <span className="text-fg-soft">Isolation on the API server</span>
                <StatusPill tone={system.data.sandbox.available ? "ok" : "bad"}>{system.data.sandbox.available ? "Available" : "Unavailable"}</StatusPill>
              </div>
              <p className="mt-3 text-xs text-muted">Workers verify isolation themselves and refuse to start without it. Supported toolchains: {system.data.capabilities.sandbox_toolchains.join(", ")}. Live previews: not available.</p>
            </Panel>
          </>
        ) : (
          <div className="xl:col-span-2"><ApiError status={system.status} message={system.message} /></div>
        )}
        <Panel title="Security" description="Where you are signed in, and your password.">
          <SecurityPanel sessions={devices} hasPassword={hasPassword} />
        </Panel>
        <Panel title="Two-factor sign-in" description="A code from an authenticator app, as well as your password.">
          <TwoFactorSettings enabled={user.twoFactorEnabled} hasPassword={hasPassword} />
        </Panel>
        <Panel title="Your data" description="Take a copy with you, or remove everything.">
          <AccountData hasPassword={hasPassword} />
        </Panel>
      </div>
    </>
  );
}
