"use client";

import { useBackendStatus } from "@/lib/backend-status";
import { timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * "This is what we last saw, and it is older than it should be."
 *
 * Only renders once data has actually gone stale — a permanent "updated 3s
 * ago" is noise, and noise next to a number is worse than nothing because it
 * stops being read. The threshold is expressed against the query's own
 * refetch interval so each section decides what late means for itself.
 */
export function StaleLabel({
  updatedAt,
  staleAfterMs,
  className,
}: {
  updatedAt: number | undefined;
  staleAfterMs: number;
  className?: string;
}) {
  const { online } = useBackendStatus();
  if (!updatedAt) return null;

  const age = Date.now() - updatedAt;
  if (online && age < staleAfterMs) return null;

  return (
    <span className={cn("text-[11px] font-normal text-delayed", className)}>
      updated {timeAgo(new Date(updatedAt).toISOString())}
    </span>
  );
}

/**
 * Wraps a data region so it visibly recedes when the backend is unreachable.
 *
 * Dimmed rather than blanked: the last known prices are still the most useful
 * thing on screen, and hiding them to signal staleness throws away
 * information the reader wants in order to make a point they can already see.
 */
export function StaleWhenOffline({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  const { online } = useBackendStatus();
  return (
    <div
      className={cn(
        "transition-opacity duration-500",
        !online && "opacity-50 saturate-50",
        className,
      )}
      aria-busy={!online}
      title={!online ? "Showing the last values received — the backend is unreachable" : undefined}
    >
      {children}
    </div>
  );
}
