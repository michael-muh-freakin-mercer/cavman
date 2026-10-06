import "server-only";
import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { auth, ensureAuthSchema } from "./auth";

export type SessionUser = {
  id: string;
  name: string;
  email: string;
  image?: string | null;
  emailVerified: boolean;
  twoFactorEnabled: boolean;
};

export async function currentUser(): Promise<SessionUser | null> {
  await ensureAuthSchema();
  const session = await auth.api.getSession({ headers: await headers() });
  if (!session) return null;
  const { id, name, email, image, emailVerified } = session.user;
  // Added by the two-factor plugin, which the inferred session type does not see.
  const { twoFactorEnabled } = session.user as { twoFactorEnabled?: boolean | null };
  return { id, name, email, image, emailVerified, twoFactorEnabled: twoFactorEnabled === true };
}

export async function requireUser(next = "/app"): Promise<SessionUser> {
  const user = await currentUser();
  if (!user) redirect(`/sign-in?next=${encodeURIComponent(next)}`);
  return user;
}
