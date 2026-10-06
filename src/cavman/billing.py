"""Paid plans through Stripe: Checkout for the Builder plan and top-ups, the
Customer Portal for invoices and cancelling, and webhooks that set limits.

Stripe is the merchant of record (Managed Payments): it collects tax, handles
fraud and disputes, and sends receipts. Cavman keeps only what its limits need:
the account's plan, the usage credit bought, and how much of it is used.

Plans: Free keeps the server's monthly allowance; Builder replaces it with
``builder_usage_usd`` a month while its subscription is active, trialing or
being retried. A top-up adds ``topup_usage_usd`` of credit that does not
expire; it is spent only after the month's plan allowance is used up.

Webhooks are the only way a payment changes limits. Every event is verified
against ``STRIPE_WEBHOOK_SECRET``, subscription state is read back from Stripe
rather than trusted from the payload (events can arrive out of order), and each
payment grants credit once.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

log = logging.getLogger(__name__)

BUILDER_LOOKUP_KEY = "cavman_builder_monthly"
TOPUP_LOOKUP_KEY = "cavman_topup_10"
# Artificial Intelligence as a Service, cloud based, business use: eligible
# for Managed Payments since 2026-06-02.
TAX_CODE = "txcd_10105002"
# past_due: Stripe is still retrying the payment (Smart Retries); the plan holds
# until the retries end and Stripe cancels the subscription.
PAID_STATUSES = frozenset({"active", "trialing", "past_due"})
USER_ID = re.compile(r"[A-Za-z0-9_.:@-]{1,128}")  # as the API accepts


class BillingError(RuntimeError):
    """A billing request that cannot be done; the message is safe to show."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class StripeGateway:
    """The few Stripe calls Cavman makes, behind one seam tests can replace."""

    def __init__(self, secret_key: str, webhook_secret: str):
        import stripe

        self._client = stripe.StripeClient(secret_key)
        self._webhook_secret = webhook_secret
        self._prices: dict[str, str] = {}

    def price(self, lookup_key: str) -> str:
        if lookup_key not in self._prices:
            found = self._client.v1.prices.list(params={"lookup_keys": [lookup_key], "active": True, "limit": 1})
            if not found.data:
                raise BillingError(f"The Stripe price {lookup_key!r} does not exist.", 503)
            self._prices[lookup_key] = found.data[0].id
        return self._prices[lookup_key]

    def create_checkout(self, params: dict) -> str:
        return self._client.v1.checkout.sessions.create(params=params).url

    def create_portal(self, customer_id: str, return_url: str) -> str:
        return self._client.v1.billing_portal.sessions.create(
            params={"customer": customer_id, "return_url": return_url}).url

    def subscription(self, subscription_id: str) -> dict:
        return self._client.v1.subscriptions.retrieve(subscription_id).to_dict()

    def cancel_subscription(self, subscription_id: str) -> None:
        self._client.v1.subscriptions.cancel(subscription_id)

    def event(self, payload: bytes, signature: str) -> dict:
        """The verified event; raises ValueError when the signature does not match."""
        import stripe

        try:
            return self._client.construct_event(payload, signature, self._webhook_secret).to_dict()
        except stripe.SignatureVerificationError as exc:
            raise ValueError("Invalid Stripe signature.") from exc


def _month(now: datetime) -> str:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


def _next_month(month: str) -> str:
    start = datetime.fromisoformat(month)
    return (start.replace(year=start.year + 1, month=1) if start.month == 12
            else start.replace(month=start.month + 1)).isoformat()


def on_paid_plan(account: dict | None) -> bool:
    return bool(account and account.get("subscription_id") and account.get("status") in PAID_STATUSES)


def plan_allowance(settings, account: dict | None) -> float:
    return settings.builder_usage_usd if on_paid_plan(account) else settings.account_monthly_budget_usd


def monthly_plan_usd(settings, platform, owner_id: str, now: datetime) -> float:
    """This month's plan allowance, remembered for settling top-up credit later."""
    usd = plan_allowance(settings, platform.billing_account(owner_id))
    noted = platform.plan_month(owner_id, _month(now))
    if noted is None or noted < usd:
        platform.note_plan_month(owner_id, _month(now), usd)
    return usd


def available_credit(settings, platform, owner_id: str, now: datetime, spent_in) -> float:
    """Top-up credit left for this month: grants less what earlier months used.

    A month uses credit for what it spent beyond its plan allowance. Months
    are settled once, the first time a later month asks, using the largest
    plan allowance the account had in that month.
    ``spent_in(since, until)`` returns provider-reported USD spent.
    """
    grants = platform.credit_grants(owner_id)
    if not grants:
        return 0.0
    debits = platform.credit_debits(owner_id)
    current = _month(now)
    month = _month(datetime.fromisoformat(grants[0][1]))
    while month < current:
        following = _next_month(month)
        if month not in debits:
            granted = sum(usd for usd, created in grants if created < following)
            left = granted - sum(debits.values())
            debit = 0.0
            if left > 0:
                allowance = platform.plan_month(owner_id, month)
                if allowance is None:
                    allowance = settings.account_monthly_budget_usd
                debit = min(left, max(0.0, spent_in(month, following) - allowance))
            platform.record_debit(owner_id, month, round(debit, 6))
            debits[month] = round(debit, 6)
        month = following
    return max(0.0, sum(usd for usd, _ in grants) - sum(debits.values()))


class Billing:
    def __init__(self, settings, platform, gateway: StripeGateway | None = None):
        self.settings = settings
        self.platform = platform
        self.gateway = gateway or (StripeGateway(settings.stripe_secret_key, settings.stripe_webhook_secret)
                                   if settings.billing_enabled else None)

    @property
    def enabled(self) -> bool:
        return self.gateway is not None

    def _require(self) -> StripeGateway:
        if self.gateway is None:
            raise BillingError("Paid plans are not available on this server.", 404)
        return self.gateway

    # Views and allowance ------------------------------------------------

    def view(self, owner_id: str, credit_usd: float) -> dict:
        account = self.platform.billing_account(owner_id)
        paid = on_paid_plan(account)
        return {
            "enabled": self.enabled,
            "plan": "builder" if paid else "free",
            "status": account.get("status") if account else None,
            "period_end": account.get("period_end") if account else None,
            "cancel_at_period_end": bool(account and account.get("cancel_at_period_end")),
            "monthly_usage_usd": plan_allowance(self.settings, account),
            "credit_usd": round(credit_usd, 6),
            "has_customer": bool(account and account.get("customer_id")),
            "trial_days": self.trial_days(account),
            "builder": {"price_usd": 20, "usage_usd": self.settings.builder_usage_usd},
            "topup": {"price_usd": 10, "usage_usd": self.settings.topup_usage_usd},
        }

    def trial_days(self, account: dict | None) -> int:
        """A free trial on the first Builder subscription only."""
        return 0 if account and account.get("trial_used") else self.settings.billing_trial_days

    # Checkout and portal ------------------------------------------------

    def _urls(self, outcome: str) -> str:
        return f"{self.settings.public_url}/app/settings?billing={outcome}#billing"

    def checkout(self, owner_id: str, kind: str) -> str:
        gateway = self._require()
        account = self.platform.billing_account(owner_id)
        customer = account.get("customer_id") if account else None
        params: dict = {
            "managed_payments": {"enabled": True},
            "client_reference_id": owner_id,
            "metadata": {"owner_id": owner_id, "kind": kind},
            "success_url": self._urls("success"),
            "cancel_url": self._urls("cancelled"),
        }
        if customer:
            params["customer"] = customer
        if kind == "builder":
            if on_paid_plan(account):
                raise BillingError("You already have the Builder plan. Manage it from Billing.")
            subscription_data: dict = {"metadata": {"owner_id": owner_id}}
            trial = self.trial_days(account)
            if trial:
                subscription_data["trial_period_days"] = trial
            params.update(mode="subscription", subscription_data=subscription_data,
                          line_items=[{"price": gateway.price(BUILDER_LOOKUP_KEY), "quantity": 1}])
        elif kind == "topup":
            params.update(mode="payment", line_items=[{"price": gateway.price(TOPUP_LOOKUP_KEY), "quantity": 1}],
                          payment_intent_data={"metadata": {"owner_id": owner_id, "kind": "topup"}})
            if not customer:
                params["customer_creation"] = "always"
        else:
            raise BillingError("Choose the Builder plan or a top-up.", 422)
        return gateway.create_checkout(params)

    def portal(self, owner_id: str) -> str:
        gateway = self._require()
        account = self.platform.billing_account(owner_id)
        if not account or not account.get("customer_id"):
            raise BillingError("There is nothing to manage yet: you have not paid for anything.")
        return gateway.create_portal(account["customer_id"], f"{self.settings.public_url}/app/settings#billing")

    def cancel_for_deletion(self, account: dict | None) -> None:
        """Stop charging a deleted account (its billing row, read before erasure)."""
        if self.gateway is not None and on_paid_plan(account):
            self.gateway.cancel_subscription(account["subscription_id"])

    # Webhooks -----------------------------------------------------------

    def handle(self, payload: bytes, signature: str) -> str:
        gateway = self._require()
        event = gateway.event(payload, signature)
        if self.platform.stripe_event_seen(event["id"]):
            return "duplicate"
        kind = event["type"]
        item = event["data"]["object"]
        if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            self._checkout_done(item)
        elif kind.startswith("customer.subscription."):
            owner = self._owner(item)
            if owner:
                self._sync_subscription(owner, item["id"])
        elif kind == "charge.refunded":
            self._refunded(item)
        self.platform.record_stripe_event(event["id"])
        return kind

    def _owner(self, item: dict) -> str | None:
        owner = (item.get("metadata") or {}).get("owner_id") or item.get("client_reference_id")
        if not owner and item.get("customer"):
            owner = self.platform.billing_owner(item["customer"])
        if not owner or not USER_ID.fullmatch(owner):
            log.warning("Stripe object %s names no Cavman account", item.get("id"))
            return None
        return owner

    def _checkout_done(self, session: dict) -> None:
        owner = self._owner(session)
        if owner is None:
            return
        if session.get("customer"):
            self.platform.save_billing(owner, customer_id=session["customer"])
        if session.get("mode") == "subscription" and session.get("subscription"):
            self._sync_subscription(owner, session["subscription"])
        elif (session.get("mode") == "payment" and session.get("payment_status") == "paid"
              and (session.get("metadata") or {}).get("kind") == "topup"):
            grant = session.get("payment_intent") or session["id"]
            if self.platform.grant_credit(grant, owner, self.settings.topup_usage_usd):
                log.info("Granted $%.2f usage credit to %s", self.settings.topup_usage_usd, owner)

    def _sync_subscription(self, owner: str, subscription_id: str) -> None:
        subscription = self.gateway.subscription(subscription_id)
        items = (subscription.get("items") or {}).get("data") or [{}]
        period_end = items[0].get("current_period_end") or subscription.get("current_period_end")
        fields = {
            "subscription_id": subscription["id"],
            "status": subscription["status"],
            "period_end": (datetime.fromtimestamp(period_end, timezone.utc).isoformat() if period_end else None),
            "cancel_at_period_end": int(bool(subscription.get("cancel_at_period_end"))),
        }
        if subscription.get("customer"):
            fields["customer_id"] = subscription["customer"]
        if subscription.get("trial_end") or subscription.get("trial_start"):
            fields["trial_used"] = 1
        self.platform.save_billing(owner, **fields)
        if subscription["status"] in PAID_STATUSES:
            self.platform.note_plan_month(owner, _month(datetime.now(timezone.utc)), self.settings.builder_usage_usd)

    def _refunded(self, charge: dict) -> None:
        """A refunded top-up takes back the refunded share of its credit. Grants
        are keyed by payment intent, so other charges match nothing."""
        intent = charge.get("payment_intent")
        if not intent:
            return
        amount = charge.get("amount") or 0
        refunded = charge.get("amount_refunded") or 0
        if amount > 0:
            self.platform.reduce_credit(intent, self.settings.topup_usage_usd * (1 - refunded / amount))
