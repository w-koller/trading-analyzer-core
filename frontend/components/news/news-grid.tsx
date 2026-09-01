"use client";

import { NewsItem } from "@/components/news/news-item";
import { QueryErrorCard } from "@/components/query-error-card";
import { Empty, RowSkeletons } from "@/components/ui/section-card";
import type { NewsArticle } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * A responsive grid of headlines, three across on a wide screen.
 *
 * `dense` drops to a single column for the dashboard card, which sits beside
 * two other cards and has a third of the width to work with.
 */
export function NewsGrid({
  articles,
  isLoading,
  isError,
  error,
  dense = false,
  skeletonRows = 6,
  emptyMessage = "No stories yet — try refreshing the feeds.",
}: {
  articles: NewsArticle[] | undefined;
  isLoading: boolean;
  isError?: boolean;
  error?: unknown;
  dense?: boolean;
  skeletonRows?: number;
  emptyMessage?: string;
}) {
  if (isLoading) return <RowSkeletons n={skeletonRows} />;
  // "Couldn't ask" and "nothing to show" are different answers.
  if (isError) return <QueryErrorCard error={error} what="news" />;
  if (!articles || articles.length === 0) return <Empty>{emptyMessage}</Empty>;

  return (
    <div
      className={cn(
        "gap-x-6",
        dense ? "flex flex-col divide-y divide-border/60 px-2"
              : "grid md:grid-cols-2 lg:grid-cols-3",
      )}
    >
      {articles.map((a) => (
        <NewsItem key={a.id} article={a} className={dense ? "" : "border-b border-border/60"} />
      ))}
    </div>
  );
}
