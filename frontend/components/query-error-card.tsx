"use client";

import { WifiOff, AlertTriangle } from "lucide-react";
import { Card } from "@/components/ui/card";
import { ApiError } from "@/lib/api";

/**
 * "We could not ask" — as distinct from "the answer was nothing".
 *
 * Every list in this app has an empty state, and before this an unreachable
 * backend fell straight into it: "No theses match this filter", "Watchlist is
 * empty — run a sync". Both are confident, both were wrong, and both send the
 * reader off to fix the wrong problem.
 */
export function QueryErrorCard({
  error,
  what = "data",
}: {
  error: unknown;
  what?: string;
}) {
  const unreachable = error instanceof ApiError ? error.unreachable : true;
  const detail = error instanceof Error ? error.message : String(error);

  return (
    <Card className="border-dashed border-bear/40 p-8 text-center">
      {unreachable ? (
        <WifiOff className="mx-auto h-5 w-5 text-bear" />
      ) : (
        <AlertTriangle className="mx-auto h-5 w-5 text-bear" />
      )}
      <p className="mt-3 text-sm font-medium">
        {unreachable ? `Could not load ${what}` : `The backend rejected the ${what} request`}
      </p>
      <p className="mx-auto mt-1 max-w-md text-xs text-muted-foreground">
        {unreachable
          ? "The backend is unreachable. This is not an empty result — retrying automatically."
          : detail}
      </p>
    </Card>
  );
}
