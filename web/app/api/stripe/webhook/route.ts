import { NextResponse } from "next/server";
import { apiBase } from "@/lib/cavman";
import { setting } from "@/lib/env";

/**
 * Stripe's webhook endpoint (https://<site>/api/stripe/webhook).
 *
 * Stripe cannot reach the private API, so this relays each event to it byte for
 * byte with the Stripe-Signature header. The API verifies the signature against
 * STRIPE_WEBHOOK_SECRET; nothing is trusted or parsed here.
 */
export const dynamic = "force-dynamic";

const MAX_BYTES = 512 * 1024;

export async function POST(request: Request) {
  const signature = request.headers.get("stripe-signature");
  const token = setting("API_TOKEN");
  if (!signature) return NextResponse.json({ detail: "Missing Stripe-Signature." }, { status: 400 });
  if (!token) return NextResponse.json({ detail: "Not configured." }, { status: 503 });
  const payload = await request.arrayBuffer();
  if (payload.byteLength > MAX_BYTES) return NextResponse.json({ detail: "Too large." }, { status: 413 });

  let upstream: Response;
  try {
    upstream = await fetch(`${apiBase()}/api/billing/webhook`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Stripe-Signature": signature, "Content-Type": "application/json" },
      body: payload,
      cache: "no-store",
    });
  } catch {
    // Stripe retries failed deliveries for up to three days.
    return NextResponse.json({ detail: "The Cavman API is unavailable." }, { status: 503 });
  }
  return new Response(upstream.body, { status: upstream.status, headers: { "Content-Type": "application/json" } });
}
