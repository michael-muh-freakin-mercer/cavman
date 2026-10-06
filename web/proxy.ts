import { getSessionCookie } from "better-auth/cookies";
import { NextResponse, type NextRequest } from "next/server";

/**
 * Every page gets a per-request nonce-based Content Security Policy: scripts
 * run only if Next.js stamped them with this request's nonce, the page talks
 * only to its own origin, and it can never be framed.
 *
 * For /app, visitors without a session cookie are sent to sign in with the
 * full destination preserved. The app layout still verifies the session
 * server-side on every request; this check is only a fast path.
 */
function contentSecurityPolicy(nonce: string, secure: boolean): string {
  const development = process.env.NODE_ENV !== "production";
  // Cloudflare Turnstile (sign-up CAPTCHA) runs in its own frame and calls home.
  const captcha = process.env.TURNSTILE_SITE_KEY ? " https://challenges.cloudflare.com" : "";
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${development ? " 'unsafe-eval'" : ""}`,
    // Inline style attributes (meters, field sizing) need 'unsafe-inline'; styles cannot execute code.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: https://avatars.githubusercontent.com",
    "font-src 'self'",
    `connect-src 'self'${captcha}${development ? " ws:" : ""}`,
    `frame-src ${captcha ? captcha.trim() : "'none'"}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    // Only when the page itself came over HTTPS (directly, or through Caddy). On
    // plain-HTTP localhost, WebKit would upgrade the page's own requests to
    // https://localhost, which nothing serves; Chromium and Firefox exempt localhost.
    ...(development || !secure ? [] : ["upgrade-insecure-requests"]),
  ].join("; ");
}

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  if ((pathname === "/app" || pathname.startsWith("/app/")) && !getSessionCookie(request)) {
    const url = new URL(`/sign-in?next=${encodeURIComponent(pathname + search)}`, request.url);
    return NextResponse.redirect(url);
  }
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const secure = request.nextUrl.protocol === "https:" || request.headers.get("x-forwarded-proto") === "https";
  const policy = contentSecurityPolicy(nonce, secure);
  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", policy);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", policy);
  return response;
}

export const config = {
  matcher: [
    // Pages only: skip API routes (JSON/SSE), static assets and image optimisation.
    { source: "/((?!api/|_next/static|_next/image|favicon.ico).*)", missing: [{ type: "header", key: "next-router-prefetch" }] },
  ],
};
