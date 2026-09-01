"use client";

import { Suspense, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { PageHeader } from "@/components/layout/page-header";
import { NewsGrid } from "@/components/news/news-grid";
import { FeedHealthPanel } from "@/components/news/feed-health-panel";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadMoreButton } from "@/components/ui/load-more-button";
import { api, type NewsCategory } from "@/lib/api";
import { cn } from "@/lib/utils";

const TABS: { value: NewsCategory; label: string }[] = [
  { value: "all", label: "All" },
  { value: "shocks", label: "Market shocks" },
  { value: "themes", label: "Vibe & themes" },
  { value: "macro", label: "Macro & geopolitics" },
  { value: "watchlist", label: "My watchlist" },
];

function NewsView() {
  // The tab lives in the URL, like the market filter: a filtered view stays
  // shareable and survives a refresh.
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const tab = (params.get("tab") as NewsCategory) || "all";
  const [limit, setLimit] = useState(30);

  const setTab = (next: NewsCategory) => {
    const q = new URLSearchParams(params.toString());
    if (next === "all") q.delete("tab");
    else q.set("tab", next);
    const s = q.toString();
    router.replace(s ? `${pathname}?${s}` : pathname, { scroll: false });
    setLimit(30);
  };

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["news", "list", tab, limit],
    queryFn: () => api.news({ category: tab, limit }),
    refetchInterval: 5 * 60_000,
  });

  const counts = data?.counts_by_category ?? {};

  return (
    <>
      <PageHeader
        title="News"
        description="Public feeds, newest first. Context only — headlines never drive an indicator."
        showMarkets={false}
      />

      <div className="mb-4 flex flex-wrap items-center gap-0.5 rounded-lg bg-muted p-0.5">
        {TABS.map((t) => (
          <button
            key={t.value}
            onClick={() => setTab(t.value)}
            className={cn(
              "rounded-md px-3 py-1 text-xs font-semibold transition-colors",
              tab === t.value
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {t.label}
            {counts[t.value] !== undefined && (
              <span className="ml-1.5 opacity-60">{counts[t.value]}</span>
            )}
          </button>
        ))}
      </div>

      <NewsGrid
        articles={data?.articles}
        isLoading={isLoading}
        isError={isError}
        error={error}
        skeletonRows={9}
        emptyMessage={
          tab === "watchlist"
            ? "Nothing about your watchlist yet. Articles are linked to a ticker only when they come from that ticker's own feed or name it explicitly."
            : "No stories in this category yet — try Refresh now below."
        }
      />

      <LoadMoreButton
        loaded={data?.articles.length ?? 0}
        limit={limit}
        onLoadMore={() => setLimit((l) => l + 30)}
      />

      <FeedHealthPanel />
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Skeleton className="h-96 w-full" />}>
      <NewsView />
    </Suspense>
  );
}
