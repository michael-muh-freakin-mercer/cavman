"""Paid plans: Checkout parameters, signed webhooks, plan and credit limits.

Stripe's network calls are replaced by a fake gateway; webhook signatures are
verified by the real Stripe library against a test secret.
"""
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("stripe")

from fastapi.testclient import TestClient

from cavman.api import create_app
from cavman.billing import StripeGateway, available_credit
from cavman.config import Settings
from cavman.platform_store import PlatformStore

TOKEN = "t" * 40
SECRET = "whsec_test_secret"  # pragma: allowlist secret
ALICE = {"Authorization": f"Bearer {TOKEN}", "X-Cavman-User": "alice"}
SERVICE = {"Authorization": f"Bearer {TOKEN}"}


class FakeGateway(StripeGateway):
    """Real signature checks; every network call recorded instead of sent."""

    def __init__(self):
        super().__init__("sk_test_fake", SECRET)
        self.checkouts: list[dict] = []
        self.subscriptions: dict[str, dict] = {}
        self.cancelled: list[str] = []

    def price(self, lookup_key):
        return f"price_{lookup_key}"

    def create_checkout(self, params):
        self.checkouts.append(params)
        return "https://checkout.stripe.test/session"

    def create_portal(self, customer_id, return_url):
        return f"https://billing.stripe.test/{customer_id}"

    def subscription(self, subscription_id):
        return self.subscriptions[subscription_id]

    def cancel_subscription(self, subscription_id):
        self.cancelled.append(subscription_id)


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", api_token=TOKEN, executor="scripted",
                    account_monthly_budget_usd=1.0, stripe_secret_key="sk_test_fake",  # pragma: allowlist secret
                    stripe_webhook_secret=SECRET, public_url="https://cavman.test")


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def client(settings, gateway):
    app = create_app(settings)
    app.state.billing.gateway = gateway
    with TestClient(app) as test_client:
        yield test_client


def send(client, event_type, obj, event_id=None, secret=SECRET):
    payload = json.dumps({"id": event_id or f"evt_{time.time_ns()}", "object": "event", "type": event_type,
                          "data": {"object": obj}}).encode()
    stamp = int(time.time())
    signature = hmac.new(secret.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return client.post("/api/billing/webhook", content=payload,
                       headers={**SERVICE, "Stripe-Signature": f"t={stamp},v1={signature}",
                                "Content-Type": "application/json"})


def subscription(status="active", trial=True, cancel=False):
    return {"id": "sub_1", "object": "subscription", "status": status, "customer": "cus_1",
            "cancel_at_period_end": cancel, "trial_start": 1 if trial else None, "trial_end": 2 if trial else None,
            "metadata": {"owner_id": "alice"},
            "items": {"data": [{"current_period_end": 1793000000}]}}


def spending(client):
    return client.get("/api/account", headers=ALICE).json()["spending"]


def test_free_account_sees_the_free_allowance_and_paid_plans(client):
    view = client.get("/api/billing", headers=ALICE).json()
    assert view["enabled"] and view["plan"] == "free" and view["monthly_usage_usd"] == 1.0
    assert view["trial_days"] == 30 and view["credit_usd"] == 0
    assert spending(client)["limit_usd"] == 1.0


def test_billing_is_off_without_stripe_secrets(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data", api_token=TOKEN, executor="scripted"))
    with TestClient(app) as client:
        assert client.get("/api/billing", headers=ALICE).json()["enabled"] is False
        refused = client.post("/api/billing/checkout", json={"kind": "builder"}, headers=ALICE)
        assert refused.status_code == 404


def test_builder_checkout_uses_managed_payments_and_one_trial(client, gateway):
    made = client.post("/api/billing/checkout", json={"kind": "builder"}, headers=ALICE)
    assert made.json()["url"].startswith("https://checkout.stripe.test")
    params = gateway.checkouts[-1]
    assert params["mode"] == "subscription" and params["managed_payments"] == {"enabled": True}
    assert params["client_reference_id"] == "alice"
    assert params["subscription_data"] == {"metadata": {"owner_id": "alice"}, "trial_period_days": 30}
    assert params["line_items"] == [{"price": "price_cavman_builder_monthly", "quantity": 1}]
    assert params["success_url"] == "https://cavman.test/app/settings?billing=success#billing"

    gateway.subscriptions["sub_1"] = subscription()
    assert send(client, "customer.subscription.created", subscription()).status_code == 200
    assert client.get("/api/billing", headers=ALICE).json()["plan"] == "builder"
    refused = client.post("/api/billing/checkout", json={"kind": "builder"}, headers=ALICE)
    assert refused.status_code == 409

    gateway.subscriptions["sub_1"] = subscription(status="canceled")
    send(client, "customer.subscription.deleted", subscription(status="canceled"))
    assert client.get("/api/billing", headers=ALICE).json()["plan"] == "free"
    client.post("/api/billing/checkout", json={"kind": "builder"}, headers=ALICE)
    assert "trial_period_days" not in gateway.checkouts[-1]["subscription_data"]
    assert gateway.checkouts[-1]["customer"] == "cus_1"


def test_subscription_state_is_read_from_stripe_not_the_event(client, gateway):
    gateway.subscriptions["sub_1"] = subscription(status="canceled")
    # An old "active" event arriving late does not revive a cancelled plan.
    send(client, "customer.subscription.updated", subscription(status="active"))
    assert client.get("/api/billing", headers=ALICE).json()["plan"] == "free"


def test_builder_plan_raises_the_monthly_limit(client, gateway):
    gateway.subscriptions["sub_1"] = subscription()
    send(client, "checkout.session.completed", {"id": "cs_1", "object": "checkout.session", "mode": "subscription",
                                                "customer": "cus_1", "subscription": "sub_1",
                                                "client_reference_id": "alice",
                                                "metadata": {"owner_id": "alice", "kind": "builder"}})
    view = client.get("/api/billing", headers=ALICE).json()
    assert view["plan"] == "builder" and view["status"] == "active" and view["has_customer"]
    assert spending(client)["limit_usd"] == 12.0
    assert client.post("/api/billing/portal", headers=ALICE).json()["url"] == "https://billing.stripe.test/cus_1"


def topup_session(session_id="cs_t", intent="pi_1", paid=True):
    return {"id": session_id, "object": "checkout.session", "mode": "payment", "customer": "cus_1",
            "payment_intent": intent, "payment_status": "paid" if paid else "unpaid",
            "client_reference_id": "alice", "metadata": {"owner_id": "alice", "kind": "topup"}}


def test_a_paid_top_up_adds_credit_once_and_a_refund_takes_it_back(client, gateway):
    client.post("/api/billing/checkout", json={"kind": "topup"}, headers=ALICE)
    params = gateway.checkouts[-1]
    assert params["mode"] == "payment" and params["customer_creation"] == "always"
    assert params["managed_payments"] == {"enabled": True}

    send(client, "checkout.session.completed", topup_session(paid=False))
    assert spending(client)["limit_usd"] == 1.0  # not paid yet (delayed payment method)
    send(client, "checkout.session.async_payment_succeeded", topup_session(), event_id="evt_paid")
    send(client, "checkout.session.async_payment_succeeded", topup_session(), event_id="evt_paid")
    send(client, "checkout.session.completed", topup_session())  # another event for the same payment
    assert spending(client)["limit_usd"] == 7.0 and spending(client)["credit_usd"] == 6.0

    send(client, "charge.refunded", {"id": "ch_1", "object": "charge", "payment_intent": "pi_1",
                                     "amount": 1000, "amount_refunded": 500})
    assert spending(client)["credit_usd"] == 3.0
    send(client, "charge.refunded", {"id": "ch_2", "object": "charge", "payment_intent": "pi_other",
                                     "amount": 1000, "amount_refunded": 1000})
    assert spending(client)["credit_usd"] == 3.0


def test_webhooks_need_the_service_token_and_a_valid_signature(client):
    payload = json.dumps({"id": "evt_x", "type": "checkout.session.completed", "data": {"object": {}}})
    assert client.post("/api/billing/webhook", content=payload,
                       headers={"Stripe-Signature": "t=1,v1=00"}).status_code == 401
    assert send(client, "checkout.session.completed", topup_session(), secret="whsec_wrong").status_code == 400  # pragma: allowlist secret
    assert client.post("/api/billing/webhook", content=payload, headers=SERVICE).status_code == 400
    assert spending(client)["credit_usd"] == 0


def test_an_event_for_an_unknown_account_changes_nothing(client):
    stray = {**topup_session(), "client_reference_id": None, "metadata": {"kind": "topup"}, "customer": "cus_x"}
    assert send(client, "checkout.session.completed", stray).status_code == 200
    assert spending(client)["credit_usd"] == 0


def test_deleting_an_account_cancels_its_subscription(client, gateway):
    gateway.subscriptions["sub_1"] = subscription()
    send(client, "customer.subscription.created", subscription())
    assert client.delete("/api/account", headers=ALICE).status_code == 200
    assert gateway.cancelled == ["sub_1"]
    assert client.get("/api/billing", headers=ALICE).json()["plan"] == "free"


def test_credit_pays_only_for_spend_beyond_each_months_plan(tmp_path):
    settings = Settings(data_dir=tmp_path, api_token=TOKEN, account_monthly_budget_usd=1.0)
    store = PlatformStore(":memory:")
    store.grant_credit("pi_1", "alice", 6.0)
    with store._write() as db:  # bought in August
        db.execute("UPDATE credit_grants SET created_at='2026-08-20T00:00:00+00:00'")
    store.note_plan_month("alice", "2026-09-01T00:00:00+00:00", 12.0)  # Builder in September
    spent = {"2026-08-01T00:00:00+00:00": 3.5, "2026-09-01T00:00:00+00:00": 13.0}
    october = datetime(2026, 10, 6, tzinfo=timezone.utc)
    left = available_credit(settings, store, "alice", october, lambda since, until: spent[since])
    # August: 3.50 spent on a 1.00 Free allowance uses 2.50; September: 13 on 12 uses 1.
    assert left == pytest.approx(2.5)
    assert store.credit_debits("alice") == {"2026-08-01T00:00:00+00:00": 2.5, "2026-09-01T00:00:00+00:00": 1.0}
    # Settled months are not read again.
    assert available_credit(settings, store, "alice", october, lambda *_: 1 / 0) == pytest.approx(2.5)
