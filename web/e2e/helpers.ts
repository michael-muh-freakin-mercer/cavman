import { expect, type Page } from "@playwright/test";
import { createHmac } from "node:crypto";

let counter = 0;

export async function signUp(page: Page, next?: string) {
  const email = `e2e-${Date.now()}-${counter++}@example.com`;
  await page.goto(next ? `/sign-up?next=${encodeURIComponent(next)}` : "/sign-up");
  await page.getByLabel("Name").fill("Test Cavman");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("a-long-enough-password");
  await page.getByRole("button", { name: "Create account" }).click();
  await page.waitForURL(next ? (url) => url.pathname === new URL(next, "http://x").pathname : /\/app$/);
  return email;
}

export async function startBuild(page: Page, prompt: string): Promise<string> {
  await page.goto("/app/new");
  await page.getByLabel("What do you want to build?").fill(prompt);
  await page.getByRole("button", { name: "Build it" }).click();
  await page.waitForURL(/\/app\/runs\/[0-9a-f]{32}$/);
  return page.url().split("/").pop()!;
}

export async function runJson(page: Page, runId: string) {
  const response = await page.request.get(`/api/cavman/runs/${runId}`);
  expect(response.status()).toBe(200);
  return response.json();
}

/** The current 6-digit TOTP code for a base32 secret (RFC 6238, SHA-1, 30 s). */
export function totp(secret: string, at = Date.now()): string {
  let bits = "";
  for (const char of secret.replace(/=+$/, "").toUpperCase()) {
    // Base32: A-Z are 0-25, 2-7 are 26-31.
    const value = char >= "A" ? char.charCodeAt(0) - 65 : char.charCodeAt(0) - 24;
    bits += value.toString(2).padStart(5, "0");
  }
  const key = Buffer.from((bits.match(/.{8}/g) ?? []).map((byte) => parseInt(byte, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 30_000)));
  const digest = createHmac("sha1", key).update(counter).digest();
  const offset = digest[digest.length - 1] & 0xf;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}
