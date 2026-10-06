import { headers } from "next/headers";
import { NextResponse } from "next/server";
import { auth, ensureAuthSchema } from "@/lib/auth";

/** Turn the user's build emails on or off. */
export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const origin = request.headers.get("origin");
  if (!origin || new URL(origin).host !== request.headers.get("host")) {
    return NextResponse.json({ detail: "Cross-origin request refused." }, { status: 403 });
  }
  await ensureAuthSchema();
  const session = await auth.api.getSession({ headers: await headers() });
  if (!session) return NextResponse.json({ detail: "Sign in to continue." }, { status: 401 });
  let body: { buildEmails?: unknown };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: "JSON body required." }, { status: 415 });
  }
  if (typeof body.buildEmails !== "boolean") {
    return NextResponse.json({ detail: "buildEmails must be true or false." }, { status: 422 });
  }
  const context = await auth.$context;
  await context.internalAdapter.updateUser(session.user.id, { buildEmails: body.buildEmails });
  return NextResponse.json({ buildEmails: body.buildEmails });
}
