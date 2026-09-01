import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * Redirects only. This is NOT the authentication boundary.
 *
 * All this can see is whether a session cookie is PRESENT — it cannot tell
 * whether it is valid, because validating means hashing it and hitting
 * SQLite, which middleware has no business doing on every request. FastAPI
 * remains the real gate and rejects a forged or expired cookie with a 401
 * regardless of what happens here.
 *
 * So the worst a fabricated cookie buys is the dashboard shell, whose every
 * data call then 401s and bounces the user to /login via the handler in
 * app/providers.tsx. The point of this file is that a signed-out user sees a
 * login form instead of a screen full of failed requests.
 */
const COOKIE = "ta_session";

export function middleware(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  const hasCookie = req.cookies.has(COOKIE);

  if (pathname === "/login") {
    if (hasCookie) return NextResponse.redirect(new URL("/", req.url));
    return NextResponse.next();
  }

  if (!hasCookie) {
    const url = new URL("/login", req.url);
    // Preserve where they were heading, so a tapped push notification lands
    // on the ticker rather than dumping them on the dashboard after signing in.
    const target = `${pathname}${search}`;
    if (target && target !== "/") url.searchParams.set("next", target);
    return NextResponse.redirect(url);
  }

  return NextResponse.next();
}

export const config = {
  /**
   * Everything except:
   *   api/*        - proxied to FastAPI, which does its own auth. Redirecting
   *                  an XHR to an HTML login page turns a clean 401 into a
   *                  JSON parse error, which is far harder to diagnose.
   *   _next/*      - build assets. Gating these breaks the login page itself.
   *   sw.js,
   *   manifest,
   *   icons        - the service worker and PWA manifest must be fetchable
   *                  while signed out, or the app cannot be installed and
   *                  push registration cannot recover after a logout.
   */
  matcher: [
    "/((?!api|_next/static|_next/image|favicon.ico|sw.js|manifest.webmanifest|icons/).*)",
  ],
};
