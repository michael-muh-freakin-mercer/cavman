import { headers } from "next/headers";
import { NextResponse } from "next/server";
import { auth, ensureAuthSchema, githubEnabled } from "@/lib/auth";
import { apiBase, serviceHeaders } from "@/lib/cavman";

/**
 * Publish a completed run to a new GitHub repository, on the user's explicit request.
 *
 * The user's GitHub token lives only in the auth store (encrypted). It is read
 * here, server-side, for this one request and forwarded to the private API,
 * which uses it once and never stores it. If the account has not granted the
 * repository scope yet, the client is told which scope to request.
 */
export const dynamic = "force-dynamic";

export async function POST(request: Request, context: { params: Promise<{ runId: string }> }) {
  const { runId } = await context.params;
  if (!/^[0-9a-f]{32}$/.test(runId)) return NextResponse.json({ detail: "Not found." }, { status: 404 });
  const origin = request.headers.get("origin");
  if (!origin || new URL(origin).host !== request.headers.get("host")) {
    return NextResponse.json({ detail: "Cross-origin request refused." }, { status: 403 });
  }
  if (!githubEnabled) return NextResponse.json({ detail: "GitHub is not configured on this server." }, { status: 404 });
  await ensureAuthSchema();
  const requestHeaders = await headers();
  const session = await auth.api.getSession({ headers: requestHeaders });
  if (!session) return NextResponse.json({ detail: "Sign in to continue." }, { status: 401 });

  let body: { name?: unknown; private?: unknown; confirm?: unknown; mode?: unknown };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: "JSON body required." }, { status: 415 });
  }
  const isPrivate = body.private !== false;
  const scope = isPrivate ? "repo" : "public_repo";

  const accounts = await auth.api.listUserAccounts({ headers: requestHeaders });
  const github = accounts.find((account) => account.providerId === "github");
  const granted = new Set(github?.scopes ?? []);
  if (!github || !(granted.has(scope) || granted.has("repo"))) {
    return NextResponse.json({ detail: "GitHub access is needed to create the repository.", needs_scope: scope },
      { status: 409 });
  }
  let token: string | undefined;
  try {
    token = (await auth.api.getAccessToken({ body: { accountId: github.id }, headers: requestHeaders })).accessToken;
  } catch {
    token = undefined;
  }
  if (!token) {
    return NextResponse.json({ detail: "Reconnect GitHub to publish.", needs_scope: scope }, { status: 409 });
  }
  let upstream: Response;
  try {
    upstream = await fetch(`${apiBase()}/api/runs/${runId}/publish`, {
      method: "POST",
      headers: { ...serviceHeaders(session.user.id), "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: body.mode === "pull_request" ? "pull_request" : "new_repository",
        name: body.name, private: isPrivate, confirm: body.confirm === true, github_token: token,
      }),
      cache: "no-store",
    });
  } catch {
    return NextResponse.json({ detail: "The Cavman API is unavailable." }, { status: 503 });
  }
  return new Response(upstream.body, { status: upstream.status, headers: { "content-type": "application/json" } });
}
