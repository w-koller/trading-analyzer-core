"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Position, type PositionsResponse } from "@/lib/api";

/**
 * Which tickers the user actually holds, read from the Moomoo account.
 *
 * Shared through context because the answer is needed in several unrelated
 * places at once (watchlist rows, mover cards, setup cards, the ticker page)
 * and each of them only needs a yes/no.
 *
 * When the trade session is down the query still resolves — the backend
 * returns `available: false` rather than an error — and every badge simply
 * renders nothing. Holdings are an annotation on the dashboard, so an
 * unavailable trade session should cost the annotation, not the dashboard.
 */
type HoldingsValue = {
  heldCodes: Set<string>;
  positions: Position[];
  available: boolean;
  reason: string | null;
  isLoading: boolean;
};

const HoldingsContext = React.createContext<HoldingsValue>({
  heldCodes: new Set(),
  positions: [],
  available: false,
  reason: null,
  isLoading: true,
});

export function HoldingsProvider({ children }: { children: React.ReactNode }) {
  const { data, isLoading } = useQuery<PositionsResponse>({
    queryKey: ["positions"],
    queryFn: () => api.positions(),
    refetchInterval: 60_000,
  });

  const value = React.useMemo<HoldingsValue>(() => {
    const positions = data?.positions ?? [];
    return {
      heldCodes: new Set(positions.map((p) => p.code)),
      positions,
      available: data?.available ?? false,
      reason: data?.reason ?? null,
      isLoading,
    };
  }, [data, isLoading]);

  return React.createElement(HoldingsContext.Provider, { value }, children);
}

export function useHoldings() {
  return React.useContext(HoldingsContext);
}
