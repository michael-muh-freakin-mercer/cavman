import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import "./globals.css";

// Bundled in the repo so builds never depend on reaching Google Fonts (see assets/fonts/README.md).
const archivo = localFont({ src: "../assets/fonts/Archivo-Variable.woff2", weight: "100 900", variable: "--font-archivo", display: "swap" });
const jetbrainsMono = localFont({ src: "../assets/fonts/JetBrainsMono-Variable.woff2", weight: "100 800", variable: "--font-jetbrains-mono", display: "swap" });
const rubikMono = localFont({ src: "../assets/fonts/RubikMonoOne-Regular.woff2", weight: "400", variable: "--font-rubik-mono", display: "swap" });

const description =
  "An open-source AI build agent for developers. Describe a Python or TypeScript library, CLI or API core; Cavman builds it in a sandbox and hands it over only after real tests pass and a second reviewer signs off.";

export const metadata: Metadata = {
  // Link previews need absolute image URLs; the public origin is the one auth already uses.
  metadataBase: new URL(process.env.BETTER_AUTH_URL || "http://localhost:3000"),
  // The page title is what link previews show, so it carries the pitch; the joke stays in the hero.
  title: { default: "Cavman — AI builds that have to prove they work", template: "%s · Cavman" },
  description,
  applicationName: "Cavman",
  openGraph: { type: "website", siteName: "Cavman", description },
  twitter: { card: "summary_large_image" },
};

// Rendered per request so Next.js can stamp its scripts with the CSP nonce set in proxy.ts.
export const dynamic = "force-dynamic";

export const viewport: Viewport = {
  themeColor: "#eceee8",
  colorScheme: "light",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${archivo.variable} ${jetbrainsMono.variable} ${rubikMono.variable}`}>
      <body className="min-h-dvh">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:border-2 focus:border-ink focus:bg-ember focus:px-3 focus:py-2 focus:font-semibold focus:text-ink"
        >
          Skip to content
        </a>
        {children}
      </body>
    </html>
  );
}
