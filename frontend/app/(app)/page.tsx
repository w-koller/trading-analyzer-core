"use client";

import { Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/layout/page-header";
import { useMarketFilter } from "@/components/layout/market-tabs";
import { PositionAlerts } from "@/components/dashboard/position-alerts";
import { MoversSections } from "@/components/dashboard/movers-sections";
import { Opportunities } from "@/components/dashboard/opportunities";
import { SectorRotation } from "@/components/dashboard/sector-rotation";
import { TopStories } from "@/components/dashboard/top-stories";
import { SetupCard } from "@/components/setup-card";
import { QueryErrorCard } from "@/components/query-error-card";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";

/** How many thesis cards the "Recent theses" section shows. Part of this
 *  query's cache identity — see the note on `queryKey` below. */
const RECENT_THESES_LIMIT = 12;

function Dashboard() {
  const market = useMarketFilter();

  const setupsQuery = useQuery({
    // The limit is IN the key, and it is not decoration.
    //
    // movers-sections.tsx renders on this same route and also fetches
    // setups at sort=recent — but asks for 40, because buildWatchlist
    // filters them to a 36h window afterwards. Keyed only on `market`,
    // the two collapse into ONE cache entry: whichever settles first
    // decides what the other sees. Either this section renders 40 cards
    // instead of 12, or "Watch today" is handed 12 rows to filter down
    // from and quietly under-populates. No error either way — the same
    // class of silent failure decisions #37 was written about.
    //
    // Bound to the constant rather than repeating the number, so the key
    // and the request cannot drift apart later.
    queryKey: ["setups", "recent", market, RECENT_THESES_LIMIT],
    // "recent", not the conviction default — this section is headed
    // "Recent theses" and is a feed of what the scanner just produced.
    queryFn: () =>
      api.setups({
        limit: RECENT_THESES_LIMIT,
        market: market || undefined,
        sort: "recent",
      }),
    refetchInterval: 60_000,
  });

  return (
    <>
      <PageHeader
        title="Market board"
        description="Advisory only — this tool never places orders."
      />

      <div className="space-y-4">
        {/* HealthBanner now lives in the app shell so an outage is visible
            from every page, not just this one. */}
        {/* First, above the fold. Renders nothing at all when there is
            nothing wrong, so it never becomes furniture. */}
        <PositionAlerts />

        <MoversSections market={market} />

        {/* After the movers, which are raw facts, and before the news.
            Renders nothing at all when nothing clears the bar. */}
        <Opportunities market={market} />

        {/* After the per-ticker ranking and before the news: it is the same
            kind of thing as Opportunities — a PULL-only ranking the user
            chooses to read — but one level up, about groups rather than
            names. Renders nothing at all until sectors have been scored. */}
        <SectorRotation market={market} />

        <TopStories />

        <section>
          <div className="mb-2 flex items-baseline justify-between">
            <h2 className="text-sm font-semibold">Recent theses</h2>
            <span className="text-xs text-muted-foreground">
              {setupsQuery.data?.count ?? 0} shown
            </span>
          </div>

          {setupsQuery.isLoading ? (
            <div className="grid gap-3 lg:grid-cols-2">
              {Array.from({ length: 4 }).map((_, i) => (
                <Skeleton key={i} className="h-52" />
              ))}
            </div>
          ) : setupsQuery.isError ? (
            <QueryErrorCard error={setupsQuery.error} what="theses" />
          ) : (setupsQuery.data?.setups.length ?? 0) === 0 ? (
            <Card className="border-dashed p-8 text-center">
              <p className="text-sm text-muted-foreground">
                No theses stored{market ? ` for ${market}` : ""} yet.
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                Use “Scan watchlist” to generate some.
              </p>
            </Card>
          ) : (
            <div className="grid gap-3 lg:grid-cols-2">
              {setupsQuery.data?.setups.map((s) => (
                <SetupCard key={s.id} setup={s} />
              ))}
            </div>
          )}
        </section>
      </div>
    </>
  );
}

export default function Page() {
  // useSearchParams (via useMarketFilter) requires a Suspense boundary.
  return (
    <Suspense fallback={<Skeleton className="h-96 w-full" />}>
      <Dashboard />
    </Suspense>
  );
}
