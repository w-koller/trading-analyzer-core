"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError, type Health } from "@/lib/api";

/**
 * One shared view of "is the backend there, and is it well".
 *
 * Two different failures need two different words, and the UI has to tell
 * them apart:
 *
 *   unreachable  the request never landed — process down, wedged, wrong
 *                address, CORS. Retrying may fix it; nothing on screen is
 *                trustworthy any more.
 *   degraded     the backend answered, and told us one of ITS dependencies
 *                (OpenD, Ollama) is unwell. The app still works, partly.
 *
 * Collapsing them into one "error" state is how you end up telling someone
 * their backend is down when it is actually fine and merely reporting that
 * the model host is unreachable.
 */
type BackendStatus = {
  health: Health | null;
  online: boolean;
  degraded: boolean;
  /** Set only when the backend could not be reached at all. */
  unreachableError: string | null;
  /** When we last got any successful answer. */
  lastOkAt: number | null;
  isLoading: boolean;
};

const Ctx = React.createContext<BackendStatus>({
  health: null,
  online: true,
  degraded: false,
  unreachableError: null,
  lastOkAt: null,
  isLoading: true,
});

export function BackendStatusProvider({ children }: { children: React.ReactNode }) {
  // `dataUpdatedAt` is the timestamp of the last *successful* fetch and
  // survives subsequent failures, which is exactly "when did we last hear
  // from it" — no need to track it separately.
  const { data, error, isLoading, dataUpdatedAt } = useQuery<Health>({
    queryKey: ["health"],
    queryFn: () => api.health(),
    refetchInterval: 30_000,
  });

  const value = React.useMemo<BackendStatus>(() => {
    const unreachable =
      error instanceof ApiError ? error.unreachable : Boolean(error);
    return {
      health: data ?? null,
      online: !unreachable,
      degraded: data?.status === "degraded",
      unreachableError: unreachable
        ? error instanceof Error
          ? error.message
          : String(error)
        : null,
      lastOkAt: dataUpdatedAt || null,
      isLoading,
    };
  }, [data, error, isLoading, dataUpdatedAt]);

  return React.createElement(Ctx.Provider, { value }, children);
}

export function useBackendStatus() {
  return React.useContext(Ctx);
}
