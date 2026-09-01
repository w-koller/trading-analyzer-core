"use client";

import * as React from "react";
import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import { ThemeProvider } from "next-themes";
import { ApiError } from "@/lib/api";
import { BackendStatusProvider } from "@/lib/backend-status";
import { HoldingsProvider } from "@/lib/holdings";

/**
 * Client-side providers. Kept out of layout.tsx so the layout itself stays a
 * server component.
 *
 * The QueryClient is created inside state rather than at module scope: a
 * module-level client would be shared across every request the server
 * renders, leaking one user's cached data into another's render.
 */
export function Providers({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();

  // Kept in a ref so the QueryCache callback below always sees the current
  // path without being rebuilt — the QueryClient is created once, on purpose.
  const pathRef = React.useRef(pathname);
  pathRef.current = pathname;

  const [queryClient] = React.useState(
    () =>
      new QueryClient({
        /**
         * A 401 means the session expired or was revoked — send the user to
         * sign in again.
         *
         * Without this the failure is SILENT and looks like something else
         * entirely. `ApiError.unreachable` is true only for timeouts and
         * network errors, so a 401 leaves BackendStatus reporting `online`
         * with a null body, and HealthBanner renders nothing at all while the
         * 30-second poll keeps 401ing forever. The user sees an empty
         * dashboard and no explanation.
         */
        queryCache: new QueryCache({
          onError: (error) => {
            if (
              error instanceof ApiError &&
              error.status === 401 &&
              pathRef.current !== "/login"
            ) {
              const next = pathRef.current;
              router.replace(
                next && next !== "/" ? `/login?next=${encodeURIComponent(next)}` : "/login",
              );
            }
          },
        }),
        defaultOptions: {
          queries: {
            // Market data goes stale fast, but not within a single
            // interaction — this stops a tab switch refetching everything.
            staleTime: 15_000,
            // Retry only what retrying can fix. A 404 or a 422 will fail
            // identically every time, so retrying it just delays the error
            // and triples the log noise; a timeout or a dropped connection
            // is exactly what a retry is for.
            retry: (failureCount, error) => {
              if (error instanceof ApiError && error.kind === "http") {
                const status = error.status ?? 0;
                if (status >= 400 && status < 500) return false;
              }
              return failureCount < 3;
            },
            retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 15_000),
            refetchOnWindowFocus: false,
            // The browser's own online event is a free, accurate signal that
            // the network came back — cheaper than polling our way there.
            refetchOnReconnect: true,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
        <BackendStatusProvider>
          <HoldingsProvider>{children}</HoldingsProvider>
        </BackendStatusProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
