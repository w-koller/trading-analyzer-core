"use client";

import { Suspense, useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUpDown, RefreshCw, Search } from "lucide-react";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { PageHeader } from "@/components/layout/page-header";
import { PillGroup } from "@/components/ui/pill-group";
import { SectorsView } from "@/components/sectors/sectors-view";
import { useMarketFilter } from "@/components/layout/market-tabs";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ChangeBadge,
  DelayedPill,
  extendedHoursNote,
  HoldingBadge,
} from "@/components/market/indicators";
import { api } from "@/lib/api";
import { bareTicker, compactNum, num } from "@/lib/format";
import { cn } from "@/lib/utils";

type SortKey = "code" | "change" | "price";

/**
 * The sector board lives here rather than on a nav item of its own.
 *
 * NAV_ITEMS is at six and `mobile-nav.tsx` hardcodes grid-cols-6 — six cells
 * is already ~53px each at 320px — so a seventh destination is a nav
 * redesign, not a route (decisions #69a took the same way out for the
 * scorecard). It belongs on this page on the merits too: `?market=` already
 * lives here and composes with it, and on a 50-ticker account the payoff of a
 * market-wide sector view is the join back to the names you actually follow.
 */
type View = "tickers" | "sectors";

const VIEWS: { value: View; label: string }[] = [
  { value: "tickers", label: "Tickers" },
  { value: "sectors", label: "Sectors" },
];

function WatchlistView() {
  const market = useMarketFilter();
  const queryClient = useQueryClient();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  // In the URL, not in state, so the dashboard's "All sectors" link can point
  // straight at it. Anything unrecognised falls back to the ticker list.
  const view: View = params.get("view") === "sectors" ? "sectors" : "tickers";

  // Preserves every other param — notably ?market=, which this page shares
  // with the market tabs and does not own.
  const selectView = (next: View) => {
    const q = new URLSearchParams(params.toString());
    if (next === "tickers") q.delete("view");
    else q.set("view", next);
    const query = q.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  };
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("change");
  const [desc, setDesc] = useState(true);

  const tickersQuery = useQuery({
    queryKey: ["watchlist", market],
    queryFn: () => api.watchlist(market || undefined),
  });
  const listUnavailable = tickersQuery.isError;

  const moversQuery = useQuery({
    queryKey: ["movers", market],
    queryFn: () => api.movers(market || undefined),
    refetchInterval: 60_000,
  });

  // Both mutations report failure. Without onError a failed sync or a failed
  // enable/disable was completely silent — the toggle snapped back and
  // nothing said why, which reads as the UI being broken.
  const [actionError, setActionError] = useState<string | null>(null);

  const sync = useMutation({
    mutationFn: () => api.syncWatchlist(),
    onSuccess: () => {
      setActionError(null);
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      queryClient.invalidateQueries({ queryKey: ["movers"] });
    },
    onError: (e) =>
      setActionError(`Sync failed: ${e instanceof Error ? e.message : String(e)}`),
  });

  const toggle = useMutation({
    mutationFn: ({ code, enabled }: { code: string; enabled: boolean }) =>
      api.setEnabled(code, enabled),
    onSuccess: () => {
      setActionError(null);
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    },
    onError: (e, vars) =>
      setActionError(
        `Could not ${vars.enabled ? "enable" : "disable"} ${vars.code}: ` +
          `${e instanceof Error ? e.message : String(e)}`,
      ),
  });

  const rows = useMemo(() => {
    const moverBy = new Map((moversQuery.data?.movers ?? []).map((m) => [m.code, m]));
    const list = (tickersQuery.data?.tickers ?? []).map((t) => ({
      ...t,
      mover: moverBy.get(t.code),
    }));
    const filtered = query
      ? list.filter(
          (r) =>
            r.code.toLowerCase().includes(query.toLowerCase()) ||
            (r.name ?? "").toLowerCase().includes(query.toLowerCase()),
        )
      : list;

    const dir = desc ? -1 : 1;
    return [...filtered].sort((a, b) => {
      if (sort === "code") return a.code.localeCompare(b.code) * -dir;
      const av = sort === "change" ? a.mover?.change_pct : a.mover?.last_price;
      const bv = sort === "change" ? b.mover?.change_pct : b.mover?.last_price;
      // Rows without a quote sort last regardless of direction — they are
      // missing data, not a value of zero.
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      return (av - bv) * dir;
    });
  }, [tickersQuery.data, moversQuery.data, query, sort, desc]);

  const setSorting = (key: SortKey) => {
    if (sort === key) setDesc((v) => !v);
    else {
      setSort(key);
      setDesc(true);
    }
  };

  const skipped = Object.entries(moversQuery.data?.skipped_markets ?? {});

  return (
    <>
      <PageHeader
        title="Watchlist"
        description={
          view === "sectors"
            ? "Where capital moved across sectors and sub-industries, and which of your names sit in them."
            : "Synced from your Moomoo groups. Toggle a ticker to include it in scans."
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <PillGroup options={VIEWS} value={view} onChange={selectView} ariaLabel="View" />
            {/* Sync is about the ticker list, so it would imply the wrong
                thing sitting above the sector board — which has its own
                refresh. */}
            {view === "tickers" && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => sync.mutate()}
                disabled={sync.isPending}
              >
                <RefreshCw className={cn("h-3.5 w-3.5", sync.isPending && "animate-spin")} />
                {sync.isPending ? "Syncing…" : "Sync"}
              </Button>
            )}
          </div>
        }
      />

      {view === "sectors" && <SectorsView market={market} />}

      {view === "tickers" && (
      <>
      {sync.isPending && (
        <p className="mb-3 rounded-md border bg-muted/50 px-3 py-2 text-xs text-muted-foreground">
          Moomoo rate-limits group reads to 8 per 30s, so a full sync takes about a minute.
        </p>
      )}

      {actionError && (
        <p className="mb-3 rounded-md border border-bear/40 bg-bear-muted px-3 py-2 text-xs text-bear">
          {actionError}
        </p>
      )}

      {skipped.length > 0 && (
        <p className="mb-3 rounded-md border border-delayed/40 bg-delayed-muted px-3 py-2 text-xs text-delayed">
          No live quotes for {skipped.map(([m]) => m).join(", ")} — {skipped[0][1]}
        </p>
      )}

      <div className="relative mb-3">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Find a symbol"
          className="h-9 w-full rounded-md border bg-card pl-9 pr-3 text-sm outline-none transition-colors placeholder:text-muted-foreground focus:border-primary sm:max-w-xs"
        />
      </div>

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-b bg-muted/40 text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                <Th onClick={() => setSorting("code")} active={sort === "code"}>
                  Symbol
                </Th>
                <th className="px-3 py-2 font-medium">Name</th>
                <Th onClick={() => setSorting("price")} active={sort === "price"} align="right">
                  Price
                </Th>
                <Th onClick={() => setSorting("change")} active={sort === "change"} align="right">
                  Change
                </Th>
                {/* The densest surface where this actually matters: scanning
                    the list before the open is exactly when an overnight gap
                    decides what to look at first. */}
                <th className="px-3 py-2 text-right font-medium">Ext. hours</th>
                <th className="px-3 py-2 text-right font-medium">Volume</th>
                <th className="px-3 py-2 text-right font-medium">Scan</th>
              </tr>
            </thead>
            <tbody>
              {tickersQuery.isLoading &&
                Array.from({ length: 8 }).map((_, i) => (
                  <tr key={i} className="border-b last:border-0">
                    <td colSpan={6} className="px-3 py-2">
                      <Skeleton className="h-6 w-full" />
                    </td>
                  </tr>
                ))}

              {!tickersQuery.isLoading && rows.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-3 py-10 text-center text-xs">
                    {listUnavailable ? (
                      // Not "empty — run a sync": we never got an answer.
                      <span className="text-bear">
                        Could not load the watchlist — the backend is unreachable.
                        Retrying automatically.
                      </span>
                    ) : query ? (
                      <span className="text-muted-foreground">Nothing matches “{query}”.</span>
                    ) : (
                      <span className="text-muted-foreground">
                        Watchlist is empty — run a sync.
                      </span>
                    )}
                  </td>
                </tr>
              )}

              {rows.map((r) => (
                <tr key={r.code} className="border-b transition-colors last:border-0 hover:bg-muted/40">
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-1.5">
                      <Link
                        href={`/ticker/${encodeURIComponent(r.code)}`}
                        className="font-semibold hover:text-primary"
                      >
                        {bareTicker(r.code)}
                      </Link>
                      <Badge variant="outline" className="px-1 py-0 text-[9px]">
                        {r.market}
                      </Badge>
                      <HoldingBadge code={r.code} showLabel={false} />
                    </div>
                  </td>
                  <td className="max-w-[220px] truncate px-3 py-2 text-xs text-muted-foreground">
                    {r.name}
                  </td>
                  <td className="px-3 py-2 text-right tabular">
                    {r.mover ? num(r.mover.last_price) : <span className="text-muted-foreground">—</span>}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {r.mover ? <ChangeBadge value={r.mover.change_pct} /> : null}
                  </td>
                  <td className="px-3 py-2 text-right text-xs">
                    {r.mover ? extendedHoursNote(r.mover) : null}
                  </td>
                  <td className="px-3 py-2 text-right text-xs tabular text-muted-foreground">
                    {compactNum(r.mover?.volume)}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button
                      onClick={() => toggle.mutate({ code: r.code, enabled: !r.enabled })}
                      disabled={toggle.isPending}
                      className={cn(
                        "inline-flex h-5 w-9 items-center rounded-full border transition-colors disabled:opacity-50",
                        r.enabled ? "border-primary bg-primary" : "border-border bg-muted",
                      )}
                      aria-label={r.enabled ? `Disable ${r.code}` : `Enable ${r.code}`}
                    >
                      <span
                        className={cn(
                          "h-3.5 w-3.5 rounded-full bg-background transition-transform",
                          r.enabled ? "translate-x-[19px]" : "translate-x-[3px]",
                        )}
                      />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="mt-2 flex items-center justify-between text-[11px] text-muted-foreground">
        <span>{rows.length} tickers</span>
        {moversQuery.data && (
          <DelayedPill
            isDelayed={moversQuery.data.movers.some((m) => m.is_delayed_data)}
            asOf={moversQuery.data.movers[0]?.data_as_of}
          />
        )}
      </div>
      </>
      )}
    </>
  );
}

function Th({
  children,
  onClick,
  active,
  align = "left",
}: {
  children: React.ReactNode;
  onClick: () => void;
  active: boolean;
  align?: "left" | "right";
}) {
  return (
    <th className={cn("px-3 py-2 font-medium", align === "right" && "text-right")}>
      <button
        onClick={onClick}
        className={cn(
          "inline-flex items-center gap-1 uppercase tracking-wide transition-colors hover:text-foreground",
          active && "text-foreground",
        )}
      >
        {children}
        <ArrowUpDown className="h-3 w-3" />
      </button>
    </th>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Skeleton className="h-96 w-full" />}>
      <WatchlistView />
    </Suspense>
  );
}
