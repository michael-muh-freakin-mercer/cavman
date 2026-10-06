import { headers } from "next/headers";
import { NextResponse } from "next/server";
import { auth, ensureAuthSchema, githubEnabled } from "@/lib/auth";
import { apiBase, serviceHeaders } from "@/lib/cavman";

/**
 * Authenticated proxy to the private Cavman API.
 *
 * The session is verified here, server-side, and only the verified user id is
 * forwarded. The API enforces ownership of every project and run. Only an
 * explicit allowlist of resources is reachable, and state-changing requests
 * must be same-origin JSON.
 */
export const dynamic = "force-dynamic";

const ALLOWED = /^(system|builds|projects(\/[0-9a-f]{32})?|runs(\/[0-9a-f]{32}(\/(events|stream|continue|stop|abandon|budget|delivery(\/download)?|tasks\/[A-Za-z0-9_.:-]{1,128}|artifacts\/[0-9a-f]{32}|approvals\/[0-9a-f]{32}))?)?)$/;

/**
 * The user's own GitHub token, when a new project starts from a repository and
 * the account has granted repository access: it lets the user import their
 * private repositories. Read here from the encrypted auth store, sent to the
 * API in a header the browser cannot set, used once and never stored.
 */
async function importToken(joined: string, method: string, body: string | undefined, requestHeaders: Headers) {
  if (!githubEnabled || method !== "POST" || (joined !== "builds" && joined !== "projects") || !body) return null;
  let repository: unknown;
  try {
    const parsed = JSON.parse(body);
    repository = joined === "builds" ? parsed?.settings?.repository_url : parsed?.repository_url;
  } catch {
    return null;
  }
  if (typeof repository !== "string" || !repository.trim()) return null;
  const accounts = await auth.api.listUserAccounts({ headers: requestHeaders });
  const github = accounts.find((account) => account.providerId === "github");
  if (!github || !(github.scopes ?? []).includes("repo")) return null;
  try {
    return (await auth.api.getAccessToken({ body: { accountId: github.id }, headers: requestHeaders })).accessToken ?? null;
  } catch {
    return null;
  }
}

async function forward(request: Request, params: Promise<{ path: string[] }>) {
  const { path } = await params;
  const joined = path.join("/");
  if (!ALLOWED.test(joined)) return NextResponse.json({ detail: "Not found." }, { status: 404 });

  const method = request.method.toUpperCase();
  if (method !== "GET") {
    const origin = request.headers.get("origin");
    const host = request.headers.get("host");
    if (!origin || new URL(origin).host !== host) {
      return NextResponse.json({ detail: "Cross-origin request refused." }, { status: 403 });
    }
    if (!(request.headers.get("content-type") ?? "").includes("application/json")) {
      return NextResponse.json({ detail: "JSON body required." }, { status: 415 });
    }
  }

  await ensureAuthSchema();
  const requestHeaders = await headers();
  const session = await auth.api.getSession({ headers: requestHeaders });
  if (!session) return NextResponse.json({ detail: "Sign in to continue." }, { status: 401 });

  const url = new URL(request.url);
  const target = `${apiBase()}/api/${joined}${url.search}`;
  const body = method === "GET" ? undefined : await request.text();
  const githubToken = await importToken(joined, method, body, requestHeaders);
  const init: RequestInit & { duplex?: string } = {
    method,
    headers: {
      ...serviceHeaders(session.user.id),
      ...(method === "GET" ? {} : { "Content-Type": "application/json" }),
      ...(githubToken ? { "X-Cavman-GitHub-Token": githubToken } : {}),
      ...(request.headers.get("last-event-id") ? { "Last-Event-ID": request.headers.get("last-event-id")! } : {}),
    },
    body,
    cache: "no-store",
    signal: request.signal,
  };

  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch {
    if (request.signal.aborted) return new Response(null, { status: 499 });
    return NextResponse.json({ detail: "The Cavman API is unavailable. Try again shortly." }, { status: 503 });
  }

  const passthrough = new Headers();
  for (const name of ["content-type", "content-disposition", "cache-control"]) {
    const value = upstream.headers.get(name);
    if (value) passthrough.set(name, value);
  }
  if (joined.endsWith("/stream")) passthrough.set("X-Accel-Buffering", "no");
  return new Response(upstream.body, { status: upstream.status, headers: passthrough });
}

type Context = { params: Promise<{ path: string[] }> };

export async function GET(request: Request, context: Context) {
  return forward(request, context.params);
}
export async function POST(request: Request, context: Context) {
  return forward(request, context.params);
}
export async function PATCH(request: Request, context: Context) {
  return forward(request, context.params);
}
