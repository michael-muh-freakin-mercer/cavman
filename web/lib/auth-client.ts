"use client";
import { twoFactorClient } from "better-auth/client/plugins";
import { createAuthClient } from "better-auth/react";

// The sign-in form follows a two-factor challenge itself, so no redirect here.
export const authClient = createAuthClient({ plugins: [twoFactorClient()] });
export const { signIn, signUp, signOut, useSession, requestPasswordReset, resetPassword, linkSocial, deleteUser, changePassword, changeEmail, twoFactor } = authClient;
