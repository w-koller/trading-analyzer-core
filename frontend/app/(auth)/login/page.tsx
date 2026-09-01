"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";

/**
 * Login. Password plus a TOTP code.
 *
 * Deliberately says as little as possible. The backend returns one identical
 * 401 whether the password, the code, or both were wrong, and this screen
 * must not undo that by helpfully distinguishing them — so there is a single
 * error line and no per-field validation beyond "not empty".
 *
 * There is no "remember me" and no password reset. One user, one box: reset
 * is `scripts/set_password.py` at the console, which is the correct amount of
 * ceremony for something guarding a brokerage account.
 */
function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") || "/";

  // A Secure cookie is silently DISCARDED by the browser over plain http.
  // Login then succeeds (200, session row written) and the very next request
  // is a 401 — which lands the user back here looking exactly like a rejected
  // password. It cost a real debugging session, so it gets a real message.
  //
  // Checked in an effect, not during render: window does not exist on the
  // server and reading it inline would mismatch on hydration.
  const [insecure, setInsecure] = useState(false);
  useEffect(() => {
    setInsecure(!window.isSecureContext);
  }, []);

  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(password, totp);
      // replace, not push: the login page must not sit in history behind the
      // dashboard, or Back lands on a form that immediately redirects away.
      router.replace(next);
      router.refresh();
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        setError("Too many failed attempts. Wait a few minutes and try again.");
      } else if (err instanceof ApiError && err.status === 503) {
        setError("Login is not configured on this server yet.");
      } else if (err instanceof ApiError && err.unreachable) {
        setError("Could not reach the server.");
      } else {
        setError("Invalid credentials.");
      }
      setPassword("");
      setTotp("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Trading Analyzer</h1>
          <p className="mt-1 text-sm text-muted-foreground">Sign in to continue</p>
        </div>

        {insecure && (
          <div
            role="alert"
            className="mb-4 space-y-2 rounded-lg border border-delayed bg-delayed-muted p-4 text-sm"
          >
            <p className="font-medium">This page is not being served over HTTPS.</p>
            <p className="text-[13px] leading-snug">
              The session cookie is marked <code>Secure</code>, so your browser
              will throw it away and sign-in cannot persist — the password
              would be accepted and you would land straight back here.
            </p>
            {CANONICAL_URL ? (
              <p className="text-[13px] leading-snug">
                Use{" "}
                <a
                  className="font-medium underline underline-offset-2"
                  href={`${CANONICAL_URL}/login`}
                >
                  {CANONICAL_URL}
                </a>{" "}
                instead — it works from inside your network too.
              </p>
            ) : (
              <p className="text-[13px] leading-snug">
                Open this dashboard at its <code>https://</code> address instead.
              </p>
            )}
          </div>
        )}

        <form onSubmit={onSubmit} className="space-y-4 rounded-lg border border-border bg-card p-6 shadow-sm">
          <div className="space-y-1.5">
            <label htmlFor="password" className="text-sm font-medium">
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              autoFocus
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none ring-offset-background focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>

          <div className="space-y-1.5">
            <label htmlFor="totp" className="text-sm font-medium">
              Authenticator code
            </label>
            <input
              id="totp"
              // "one-time-code" lets Android autofill the code from the
              // notification shade instead of making the user switch apps.
              autoComplete="one-time-code"
              inputMode="numeric"
              pattern="[0-9]*"
              maxLength={6}
              required
              value={totp}
              onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
              className="w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm tracking-[0.3em] outline-none ring-offset-background focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>

          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={insecure || busy || !password || totp.length < 6}
            className="w-full rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition-opacity disabled:opacity-50"
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <p className="mt-6 text-center text-xs text-muted-foreground">
          Advisory only. This tool never places orders.
        </p>
      </div>
    </main>
  );
}


/**
 * useSearchParams() forces a client-side bailout, so the page cannot be
 * prerendered without a Suspense boundary — the build fails outright rather
 * than warning. The boundary is here rather than `export const dynamic =
 * "force-dynamic"` because only the ?next= lookup needs to be dynamic; the
 * shell around it stays static, so the login screen still paints instantly
 * on a cold load over mobile data.
 */
/**
 * Optional. When set, the insecure-context warning can point at the real
 * address instead of saying "use https" and leaving the user to guess which
 * host that is. Deployment-specific, so it is configuration rather than a
 * constant baked into the source.
 */
const CANONICAL_URL = (process.env.NEXT_PUBLIC_CANONICAL_URL || "").replace(/\/$/, "");

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <main className="flex min-h-screen items-center justify-center px-4 py-12">
          <div className="text-sm text-muted-foreground">Loading…</div>
        </main>
      }
    >
      <LoginForm />
    </Suspense>
  );
}
