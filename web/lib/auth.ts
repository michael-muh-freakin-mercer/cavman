import "server-only";
import { betterAuth } from "better-auth";
import { APIError, createAuthMiddleware } from "better-auth/api";
import { getMigrations } from "better-auth/db/migration";
import { nextCookies } from "better-auth/next-js";
import { captcha, twoFactor } from "better-auth/plugins";
import { existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { Pool } from "pg";
import { CAPTCHA_ENDPOINTS, captchaSecretKey } from "./captcha";
import { CavmanApiError, cavmanFetch } from "./cavman";
import { linkEmail, sendEmail } from "./email";
import { SIGNUP_CLOSED_MESSAGE, parseSignupAllowlist, signupAllowed } from "./signups";
import { setting } from "./env";

/**
 * Authentication uses Better Auth, an established library: password hashing,
 * sessions, CSRF-safe cookies, OAuth and rate limiting are its responsibility.
 * Cavman adds no cryptography of its own.
 *
 * AUTH_DATABASE_URL selects storage: a postgres:// URL for managed Postgres,
 * otherwise a local SQLite file (default .local/cavman-auth.db).
 */
function database() {
  const url = process.env.AUTH_DATABASE_URL ?? "";
  if (url.startsWith("postgres://") || url.startsWith("postgresql://")) {
    return new Pool({ connectionString: url, max: 5 });
  }
  // A development database from before the Caveman -> Cavman rename is still found.
  const fallback = !existsSync(".local/cavman-auth.db") && existsSync(".local/caveman-auth.db")
    ? ".local/caveman-auth.db" : ".local/cavman-auth.db";
  const file = resolve(/*turbopackIgnore: true*/ url.replace(/^file:/, "") || fallback);
  mkdirSync(dirname(file), { recursive: true });
  return new DatabaseSync(file);
}

const github =
  process.env.GITHUB_CLIENT_ID && process.env.GITHUB_CLIENT_SECRET
    ? {
        github: {
          clientId: process.env.GITHUB_CLIENT_ID,
          clientSecret: process.env.GITHUB_CLIENT_SECRET,
          // Identity only. Repository access would be requested separately and explicitly.
          scope: ["read:user", "user:email"],
        },
      }
    : undefined;

export const githubEnabled = Boolean(github);

const signupAllowlist = parseSignupAllowlist(setting("SIGNUP_ALLOWLIST"));
// An allowlist is only as good as proof that the address belongs to the person
// signing up, so turning it on also requires email verification.
const requireEmailVerification = setting("REQUIRE_EMAIL_VERIFICATION") === "1" || signupAllowlist !== null;

const options = {
  appName: "Cavman",
  database: database(),
  secret: process.env.BETTER_AUTH_SECRET,
  baseURL: process.env.BETTER_AUTH_URL,
  emailAndPassword: {
    enabled: true,
    minPasswordLength: 10,
    maxPasswordLength: 128,
    autoSignIn: true,
    // Off by default so a fresh install works without an email provider;
    // hosted deployments should set CAVMAN_REQUIRE_EMAIL_VERIFICATION=1 (implied by CAVMAN_SIGNUP_ALLOWLIST).
    requireEmailVerification,
    resetPasswordTokenExpiresIn: 60 * 60,
    revokeSessionsOnPasswordReset: true,
    sendResetPassword: async ({ user, url }: { user: { email: string }; url: string }) => {
      await sendEmail(linkEmail(user.email, "Reset your Cavman password",
        "Someone asked to reset the password for your Cavman account.", "Choose a new password", url));
    },
  },
  emailVerification: {
    sendOnSignUp: requireEmailVerification,
    autoSignInAfterVerification: true,
    sendVerificationEmail: async ({ user, url }: { user: { email: string }; url: string }) => {
      await sendEmail(linkEmail(user.email, "Verify your email for Cavman",
        "Confirm this email address to start building with Cavman.", "Verify email", url));
    },
  },
  socialProviders: github,
  user: {
    // A verified address is changed only after the current address approves it
    // and the new one is verified, so a stolen session cannot quietly take the account.
    changeEmail: {
      enabled: true,
      sendChangeEmailConfirmation: async ({ user, newEmail, url }: { user: { email: string }; newEmail: string; url: string }) => {
        await sendEmail(linkEmail(user.email, "Approve your new Cavman email",
          `Someone asked to change the email for your Cavman account to ${newEmail}. If that was you, approve it; we will then send a link to the new address to confirm it.`,
          "Approve the change", url));
      },
    },
    // Deleting an account requires the password (or, for GitHub-only accounts, a
    // session from the last day). Cavman's data goes first: if a build is
    // still running the API refuses, and the account is kept.
    deleteUser: {
      enabled: true,
      beforeDelete: async (user: { id: string }) => {
        try {
          await cavmanFetch(user.id, "account", { method: "DELETE" });
        } catch (error) {
          const status = error instanceof CavmanApiError ? error.status : 503;
          const message = error instanceof Error ? error.message : "Cavman could not delete your data.";
          throw new APIError(status === 409 ? "CONFLICT" : "SERVICE_UNAVAILABLE", { message });
        }
      },
    },
  },
  hooks: {
    // Say "invite-only" up front: with verification on, Better Auth answers a refused
    // password sign-up with a generic success (to hide which emails exist).
    before: createAuthMiddleware(async (ctx) => {
      if (ctx.path !== "/sign-up/email") return;
      const email = typeof ctx.body?.email === "string" ? ctx.body.email : "";
      if (!signupAllowed(email, signupAllowlist)) throw new APIError("FORBIDDEN", { message: SIGNUP_CLOSED_MESSAGE });
    }),
  },
  databaseHooks: {
    user: {
      create: {
        // Runs for every new account, password or GitHub, so neither path skips the invite list.
        before: async (user: { email: string }) => {
          if (!signupAllowed(user.email, signupAllowlist)) throw new APIError("FORBIDDEN", { message: SIGNUP_CLOSED_MESSAGE });
          return { data: user };
        },
      },
    },
  },
  session: { expiresIn: 60 * 60 * 24 * 14, updateAge: 60 * 60 * 24 },
  // OAuth tokens (used only to publish to GitHub on explicit request) are encrypted at rest.
  account: { encryptOAuthTokens: true },
  rateLimit: { enabled: process.env.NODE_ENV === "production" && setting("E2E") !== "1" },
  telemetry: { enabled: false },
  plugins: [
    ...(captchaSecretKey
      ? [captcha({
          provider: "cloudflare-turnstile",
          secretKey: captchaSecretKey,
          endpoints: CAPTCHA_ENDPOINTS,
          // A token solved on another site with the same key is refused.
          allowedHostnames: process.env.BETTER_AUTH_URL ? [new URL(process.env.BETTER_AUTH_URL).hostname] : undefined,
        })]
      : []),
    // Optional two-factor sign-in with an authenticator app, plus single-use backup codes.
    // It guards email-and-password sign-in; GitHub sign-in relies on GitHub's own.
    // The TOTP secret is always encrypted with BETTER_AUTH_SECRET; backup codes are too (the default is plain JSON).
    twoFactor({ issuer: "Cavman", backupCodeOptions: { storeBackupCodes: "encrypted" } }),
    nextCookies(),
  ],
};

export const auth = betterAuth(options);

let migrated: Promise<void> | null = null;

/** Create or upgrade the auth tables once per process (disable with CAVMAN_AUTH_AUTO_MIGRATE=0). */
export function ensureAuthSchema(): Promise<void> {
  if (setting("AUTH_AUTO_MIGRATE") === "0") return Promise.resolve();
  migrated ??= getMigrations(options)
    .then(({ runMigrations }) => runMigrations())
    .catch((error) => {
      migrated = null;
      throw error;
    });
  return migrated;
}
