import { headers } from "next/headers";
import { NextResponse } from "next/server";
import { auth, ensureAuthSchema } from "@/lib/auth";
import { apiBase, serviceHeaders } from "@/lib/cavman";

/**
 * Download everything Cavman holds for the signed-in account: the account
 * record, linked sign-in methods (never their tokens) and every project and run.
 *
 * The API streams its part one run at a time and this route passes it through,
 * so a large account is never held in memory. The API's body is one JSON object
 * starting with "{"; the account section is put in front of its first member.
 */
export const dynamic = "force-dynamic";

export async function GET() {
  await ensureAuthSchema();
  const requestHeaders = await headers();
  const session = await auth.api.getSession({ headers: requestHeaders });
  if (!session) return NextResponse.json({ detail: "Sign in to continue." }, { status: 401 });
  let upstream: Response;
  try {
    upstream = await fetch(`${apiBase()}/api/account/export`, { cache: "no-store", headers: serviceHeaders(session.user.id) });
  } catch {
    return NextResponse.json({ detail: "The Cavman API is unavailable." }, { status: 503 });
  }
  if (!upstream.ok || !upstream.body) {
    const detail = await upstream.json().then((body) => body.detail, () => null);
    return NextResponse.json({ detail: typeof detail === "string" ? detail : "Export failed." }, { status: upstream.status || 502 });
  }
  const accounts = await auth.api.listUserAccounts({ headers: requestHeaders });
  const { id, name, email, emailVerified, createdAt, updatedAt } = session.user;
  const account = {
    id, name, email, emailVerified, createdAt, updatedAt,
    sign_in_methods: accounts.map((item) => ({
      provider: item.providerId, created_at: item.createdAt, scopes: item.scopes ?? [],
    })),
  };
  const encoder = new TextEncoder();
  let first = true;
  const body = upstream.body.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      if (first) {
        first = false;
        if (chunk[0] !== 0x7b) throw new Error("Unexpected export format from the Cavman API.");
        controller.enqueue(encoder.encode(`{"account": ${JSON.stringify(account)}, `));
        chunk = chunk.subarray(1);
      }
      controller.enqueue(chunk);
    },
  }));
  const stamp = new Date().toISOString().slice(0, 10);
  return new NextResponse(body, {
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Content-Disposition": `attachment; filename="cavman-export-${stamp}.json"`,
      "Cache-Control": "no-store",
    },
  });
}
