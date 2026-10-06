"use client";

import { LoaderCircle } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { StatusPill } from "@/components/ui/status";
import { formatUsd } from "@/lib/format";
import type { BillingView } from "@/lib/types";
import { useHydrated } from "@/lib/use-hydrated";

const STATUS: Record<string, string> = {
  trialing: "Free trial",
  past_due: "Payment failed, retrying",
};

/** The account's plan, with Stripe Checkout for Builder and top-ups and the Stripe portal for the rest. */
export function BillingPanel({ billing, outcome }: { billing: BillingView; outcome?: string }) {
  const hydrated = useHydrated();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const builder = billing.plan === "builder";

  async function go(path: string, body: object, action: string) {
    setBusy(action);
    setError(null);
    try {
      const response = await fetch(`/api/cavman/billing/${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || typeof result.url !== "string") {
        throw new Error(typeof result.detail === "string" ? result.detail : "Billing is unavailable. Try again shortly.");
      }
      window.location.assign(result.url);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Billing is unavailable. Try again shortly.");
      setBusy(null);
    }
  }

  const spinner = (action: string) =>
    busy === action ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : null;

  return (
    <div className="space-y-4">
      {outcome === "success" ? (
        <p role="status" className="text-sm text-ok">Thanks. Your payment is confirmed by Stripe within a minute; reload if your plan has not changed yet.</p>
      ) : outcome === "cancelled" ? (
        <p role="status" className="text-sm text-muted">Checkout was cancelled. Nothing was charged.</p>
      ) : null}
      <div className="rounded-lg border border-line p-4">
        <div className="flex items-baseline justify-between gap-3">
          <p className="text-sm font-medium text-fg">{builder ? "Builder" : "Free"}</p>
          {billing.status && STATUS[billing.status] ? (
            <StatusPill tone={billing.status === "past_due" ? "warn" : "ok"}>{STATUS[billing.status]}</StatusPill>
          ) : null}
        </div>
        <p className="mt-1 text-sm text-fg-soft">{formatUsd(billing.monthly_usage_usd)} of build usage a month.</p>
        {builder && billing.period_end ? (
          <p className="mt-1 text-xs text-muted">
            {billing.cancel_at_period_end ? "Ends" : "Renews"} on {new Date(billing.period_end).toLocaleDateString()}.
          </p>
        ) : null}
        {billing.credit_usd > 0 ? (
          <p className="mt-1 text-xs text-muted">Plus {formatUsd(billing.credit_usd)} of top-up credit, used after the month&apos;s usage.</p>
        ) : null}
      </div>
      <div className="flex flex-wrap gap-2">
        {builder ? null : (
          <Button size="sm" disabled={!hydrated || busy !== null} onClick={() => go("checkout", { kind: "builder" }, "builder")}>
            {spinner("builder")}
            {billing.trial_days > 0
              ? `Try Builder free for ${billing.trial_days} days`
              : `Upgrade to Builder, $${billing.builder.price_usd}/month`}
          </Button>
        )}
        <Button size="sm" variant="secondary" disabled={!hydrated || busy !== null} onClick={() => go("checkout", { kind: "topup" }, "topup")}>
          {spinner("topup")}
          Add {formatUsd(billing.topup.usage_usd)} of usage for ${billing.topup.price_usd}
        </Button>
        {billing.has_customer ? (
          <Button size="sm" variant="ghost" disabled={!hydrated || busy !== null} onClick={() => go("portal", {}, "portal")}>
            {spinner("portal")}
            Invoices and plan
          </Button>
        ) : null}
      </div>
      {builder ? null : (
        <p className="text-xs text-muted">
          Builder is ${billing.builder.price_usd} a month with {formatUsd(billing.builder.usage_usd)} of usage included
          {billing.trial_days > 0 ? `; the first ${billing.trial_days} days are free, and you can cancel before you are charged` : ""}.
          Top-up credit does not expire. Payments are handled by Stripe, which adds any sales tax or VAT.
        </p>
      )}
      {error ? <p role="alert" className="text-sm text-bad">{error}</p> : null}
    </div>
  );
}
