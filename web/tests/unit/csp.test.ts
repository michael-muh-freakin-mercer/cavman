import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { proxy } from "@/proxy";

afterEach(() => vi.unstubAllEnvs());

function policy(url: string, headers: Record<string, string> = {}) {
  return proxy(new NextRequest(url, { headers })).headers.get("Content-Security-Policy") ?? "";
}

describe("content security policy", () => {
  it("upgrades insecure requests only on pages served over HTTPS", () => {
    vi.stubEnv("NODE_ENV", "production");
    expect(policy("https://cavman.dev/")).toContain("upgrade-insecure-requests");
    // Behind Caddy, the app sees plain HTTP and the original scheme in X-Forwarded-Proto.
    expect(policy("http://web:3000/", { "x-forwarded-proto": "https" })).toContain("upgrade-insecure-requests");
    // Plain-HTTP localhost (the end-to-end stack): WebKit would otherwise send its requests to https://localhost.
    expect(policy("http://localhost:3100/")).not.toContain("upgrade-insecure-requests");
    expect(policy("http://localhost:3100/")).toContain("frame-ancestors 'none'");
  });
});
