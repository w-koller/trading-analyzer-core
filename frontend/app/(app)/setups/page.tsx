"use client";

import { Suspense, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/layout/page-header";
import { useMarketFilter } from "@/components/layout/market-tabs";
import { SetupCard } from "@/components/setup-card";
import { ThesisScorecard } from "@/components/setups/thesis-scorecard";
import { QueryErrorCard } from "@/components/query-error-card";
import { Card } from "@/components/ui/card";
import { PillGroup } from "@/components/ui/pill-group";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadMoreButton } from "@/components/ui/load-more-button";
import { api, type SetupSort } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Which half of this page is showing.
 *
 * Held in the URL rather than component state for the reason the market
 * filter is (see market-tabs.tsx): it survives a refresh and it is linkable,
 * which is what lets the dashboard's calibration caption point straight at
 * the track record instead of dropping the reader on the thesis list.
 */
type View = "theses" | "scorecard";

const VIEWS: { value: View; label: string }[] = [
  { value: "theses", label: "Theses" },
  { value: "scorecard", label: "Track record" },
];

const CONVICTION_FILTERS = [
  { value: undefined, label: "All" },
  { value: 6, label: "6+" },
  { value: 8, label: "8+" },
];

const SORTS: { value: SetupSort; label: string }[] = [
  { value: "conviction", label: "Conviction" },
  { value: "recent", label: "Newest" },
];

function SetupsView() {
  const market = useMarketFilter();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const view: View = params.get("view") === "scorecard" ? "scorecard" : "theses";
  const [minConviction, setMinConviction] = useState<number | undefined>(undefined);
  const [sort, setSort] = useState<SetupSort>("conviction");
  const [limit, setLimit] = useState(24);

  // Preserves every other param — dropping ?market= here would silently reset
  // the market filter each time someone switched tabs.
  const selectView = (next: View) => {
    const q = new URLSearchParams(params.toString());
    if (next === "theses") q.delete("view");
    else q.set("view", next);
    const query = q.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  };

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["setups", "browse", market, minConviction, sort, limit],
    queryFn: () =>
      api.setups({
        limit,
        market: market || undefined,
        minConviction,
        sort,
        // One card per ticker. The rotation re-analyses the whole watchlist
        // roughly hourly, so without this the page is 40-odd near-identical
        // cards of whichever tickers were scanned last and the rest of the
        // watchlist never appears at all.
        latestPerCode: true,
      }),
    // The scorecard aggregates its own data and needs none of this, so don't
    // spend a request on a list nobody is looking at.
    enabled: view === "theses",
  });

  return (
    <>
      <PageHeader
        title="Theses"
        description={
          view === "scorecard"
            ? "How past theses actually resolved against the bars that followed them."
            : sort === "conviction"
              ? "Each ticker's current thesis, highest conviction first."
              : "Each ticker's current thesis, newest first."
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <PillGroup options={VIEWS} value={view} onChange={selectView} />
            {/* The sort and conviction pills filter the thesis LIST. Leaving
                them on the scorecard would imply they narrow it, and they do
                not — the scorecard aggregates the whole corpus. */}
            {view === "theses" && (
              <>
                <PillGroup options={SORTS} value={sort} onChange={setSort} />
                <PillGroup
                  options={CONVICTION_FILTERS}
                  value={minConviction}
                  onChange={setMinConviction}
                />
              </>
            )}
          </div>
        }
      />

      {view === "scorecard" ? (
        <ThesisScorecard />
      ) : isLoading ? (
        <div className="grid gap-3 lg:grid-cols-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-52" />
          ))}
        </div>
      ) : isError ? (
        // "Empty" and "couldn't ask" are different answers; saying the first
        // when you mean the second is how a dead backend reads as no data.
        <QueryErrorCard error={error} what="theses" />
      ) : (data?.setups.length ?? 0) === 0 ? (
        <Card className="border-dashed p-10 text-center">
          <p className="text-sm text-muted-foreground">No theses match this filter.</p>
        </Card>
      ) : (
        <>
          <div className="grid gap-3 lg:grid-cols-2">
            {data?.setups.map((s) => (
              <SetupCard key={s.id} setup={s} />
            ))}
          </div>
          <LoadMoreButton
            loaded={data?.setups.length ?? 0}
            limit={limit}
            onLoadMore={() => setLimit((l) => l + 24)}
          />
        </>
      )}
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Skeleton className="h-96 w-full" />}>
      <SetupsView />
    </Suspense>
  );
}
