# Billing

Paid plans run through Stripe with **Managed Payments**: Stripe is the
merchant of record, so it collects and files sales tax and VAT, screens for
fraud, handles disputes and refunds, and sends receipts. Cavman keeps only what
its limits need. The code is in `src/cavman/billing.py`.

| Plan | Price | Usage a month |
| --- | --- | --- |
| Free | $0 | `CAVMAN_ACCOUNT_MONTHLY_BUDGET_USD` |
| Builder | $20 a month (first subscription: `CAVMAN_BILLING_TRIAL_DAYS` free) | `CAVMAN_BUILDER_USAGE_USD` (12) |
| Top-up | $10 once | adds `CAVMAN_TOPUP_USAGE_USD` (6) of credit that does not expire |

An account's monthly limit is its plan's allowance plus the top-up credit it has
left. Credit pays only for spend beyond a month's allowance; each month is
settled once, against the largest allowance the account had that month. Months
are calendar months (UTC), the same as the existing account limit.

Builder holds while the subscription is `active`, `trialing` or `past_due`
(Stripe still retrying a failed payment). When Stripe gives up, it cancels the
subscription and the account is back on Free. Deleting an account cancels its
subscription.

## Flow

1. Settings > Billing posts to `/api/billing/checkout`. The API creates a
   Stripe-hosted Checkout Session with `managed_payments[enabled]=true`, the
   account id as `client_reference_id` and metadata, and sends the browser there.
2. Stripe sends events to `https://<site>/api/stripe/webhook`. The web server
   relays them unchanged to the private API, which verifies the signature.
3. Subscription state is always read back from Stripe, not taken from the
   event, so late or repeated events cannot revive a cancelled plan. Top-up
   credit is granted once per payment intent; a refund takes back the refunded
   share.
4. "Invoices and plan" opens Stripe's customer portal.

## Set up (once per Stripe account; repeat in live mode)

1. **Managed Payments:** turn it on at Settings > Managed Payments.
2. **Products:** "Cavman Builder", $20 monthly, price lookup key
   `cavman_builder_monthly`; "Cavman usage top-up", $10 one time, lookup key
   `cavman_topup_10`. Both use tax code `txcd_10105002` (AI as a Service,
   business use). The code finds prices by lookup key, so no price ids go in
   the configuration. (Done in the sandbox on 2026-10-06.)
3. **Customer portal:** Settings > Billing > Customer portal: invoice history
   on, cancel at end of period, payment method updates on.
4. **Revenue recovery:** Smart Retries and failed-payment emails on (Billing >
   Revenue recovery).
5. **Webhook endpoint:** `https://cavman.dev/api/stripe/webhook` with the events
   `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
   `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted` and `charge.refunded`.
6. **Keys:** put the secret key (a restricted key with write access to Checkout
   Sessions, Customer portal and Subscriptions, and read access to Prices, is
   enough) and the endpoint's signing secret in `/opt/cavman/.env` as
   `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`, set
   `CAVMAN_PUBLIC_URL=https://cavman.dev`, and redeploy. Billing stays hidden
   until both secrets are set.

## Test in the sandbox

Use a `sk_test_` key and the Stripe CLI (`stripe listen --forward-to
localhost:3000/api/stripe/webhook`), then buy Builder and a top-up from
Settings with card `4242 4242 4242 4242`. Settings should show Builder and the
credit within a few seconds.
