"use client";

import { useQuery } from "@tanstack/react-query";
import { Newspaper } from "lucide-react";
import Link from "next/link";
import { NewsGrid } from "@/components/news/news-grid";
import { SectionCard } from "@/components/ui/section-card";
import { StaleLabel } from "@/components/data-freshness";
import { api } from "@/lib/api";

const REFETCH_MS = 5 * 60_000;

/**
 * Top stories on the dashboard.
 *
 * Not filtered by the page's market tabs: a Fed decision or an oil move is
 * not scoped to a market, and hiding it because "US" is selected would be
 * the wrong kind of tidy.
 */
export function TopStories({ limit = 9 }: { limit?: number }) {
  const { data, isLoading, isError, error, dataUpdatedAt } = useQuery({
    queryKey: ["news", "top", limit],
    queryFn: () => api.topStories(limit),
    refetchInterval: REFETCH_MS,
  });

  return (
    <SectionCard
      title="Top stories"
      description="Newest across every source, capped so one busy feed cannot crowd out the rest."
      icon={<Newspaper className="h-4 w-4" />}
      accent="text-primary"
      meta={
        <div className="flex items-center gap-2">
          <StaleLabel updatedAt={dataUpdatedAt} staleAfterMs={REFETCH_MS * 2} />
          <Link href="/news" className="text-[11px] font-medium text-primary hover:underline">
            All news →
          </Link>
        </div>
      }
    >
      <div className="px-2">
        <NewsGrid
          articles={data?.articles}
          isLoading={isLoading}
          isError={isError}
          error={error}
          skeletonRows={6}
        />
      </div>
    </SectionCard>
  );
}
