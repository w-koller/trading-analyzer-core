import type { Metadata, Viewport } from "next";
import { Providers } from "./providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "Trading Analyzer",
  description: "Advisory-only AI trading analyzer and watchlist scanner",
  manifest: "/manifest.webmanifest",
  appleWebApp: { capable: true, title: "Analyzer", statusBarStyle: "default" },
  icons: {
    icon: [
      { url: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
      { url: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
    apple: "/icons/apple-touch-icon.png",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // Matches manifest.webmanifest's theme_color: the brand purple, which is
  // dark in BOTH themes (decisions #48), so the Android status bar does not
  // need to flip with the light/dark toggle.
  themeColor: "#442786",
  // The installed PWA runs edge-to-edge; without this the content is inset
  // by the system bars and the layout loses its full-bleed header.
  viewportFit: "cover",
};

/**
 * Root layout — html, body, providers, and nothing else.
 *
 * AppShell moved down to app/(app)/layout.tsx when login was added. The
 * sidebar and bottom nav are for signed-in pages; rendering them around a
 * login form would offer navigation to pages that will just bounce back.
 */
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen bg-background text-foreground antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
