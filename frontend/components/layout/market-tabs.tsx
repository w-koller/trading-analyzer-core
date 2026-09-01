"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import * as React from "react";
import { MARKETS } from "@/lib/api";
import { cn } from "@/lib/utils";

const OPTIONS = [{ value: "", label: "All" }, ...MARKETS.map((m) => ({ value: m, label: m }))];

/**
 * Market filter, held in the URL (`?market=US`) rather than component state.
 *
 * The URL is the right home for it because the filter spans several sections
 * of a page at once and outlives a navigation — a filtered view stays
 * shareable and survives a refresh.
 */
export function MarketTabs({ className }: { className?: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const current = params.get("market") ?? "";

  const select = (value: string) => {
    const next = new URLSearchParams(params.toString());
    if (value) next.set("market", value);
    else next.delete("market");
    const query = next.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  };

  return (
    <div
      role="tablist"
      aria-label="Filter by market"
      className={cn("inline-flex items-center gap-0.5 rounded-lg bg-muted p-0.5", className)}
    >
      {OPTIONS.map((opt) => {
        const active = current === opt.value;
        return (
          <button
            key={opt.value || "all"}
            role="tab"
            aria-selected={active}
            onClick={() => select(opt.value)}
            className={cn(
              "rounded-md px-3 py-1 text-xs font-semibold transition-colors",
              active
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

/** Reads the active market filter. Empty string means "all markets". */
export function useMarketFilter(): string {
  const params = useSearchParams();
  return params.get("market") ?? "";
}
