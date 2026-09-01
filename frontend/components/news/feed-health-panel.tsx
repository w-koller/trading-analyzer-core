"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, RefreshCw } from "lucide-react";
import { SourceIcon } from "@/components/news/source-icon";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Which sources are alive.
 *
 * This panel exists because the dead Reuters feed logged a warning every
 * fifteen minutes for months and nothing surfaced it. Deliberately a panel
 * here rather than a HealthBanner condition — a stale feed is not an outage,
 * and banners about non-outages train people to ignore banners.
 */
export function FeedHealthPanel() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["news", "feeds"],
    queryFn: () => api.newsFeeds(),
    refetchInterval: 5 * 60_000,
  });

  const refresh = useMutation({
    mutationFn: () => api.refreshNews(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["news"] });
    },
  });

  if (isLoading || !data) return null;

  return (
    <Card className="mt-6">
      <CardHeader className="flex-row items-start justify-between space-y-0 pb-2">
        <div>
          <CardTitle>Sources</CardTitle>
          <CardDescription>
            {data.count} feeds
            {data.failing.length > 0 ? ` · ${data.failing.length} not responding` : " · all responding"}
          </CardDescription>
        </div>
        <button
          type="button"
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11px] font-medium transition-colors hover:text-foreground disabled:opacity-50"
        >
          {refresh.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RefreshCw className="h-3 w-3" />
          )}
          {refresh.isPending ? "Fetching…" : "Refresh now"}
        </button>
      </CardHeader>
      <CardContent>
        <div className="grid gap-x-6 gap-y-1 md:grid-cols-2 lg:grid-cols-3">
          {data.feeds.map((f) => {
            const ok = f.last_status === "ok";
            const untried = f.last_status === "unknown";
            return (
              <div key={f.key} className="flex items-center gap-2 py-1 text-[11px]">
                <span
                  className={cn(
                    "h-1.5 w-1.5 shrink-0 rounded-full",
                    ok ? "bg-bull" : untried ? "bg-flat" : "bg-bear",
                  )}
                  aria-hidden
                />
                <SourceIcon icon={f.icon} category={f.category} />
                <span className="truncate font-medium">{f.label}</span>
                <span className="ml-auto shrink-0 text-muted-foreground" title={f.last_error ?? undefined}>
                  {ok
                    ? `${f.articles_last_run} · ${timeAgo(f.last_success_at)}`
                    : untried
                      ? "not yet fetched"
                      : f.last_status}
                </span>
              </div>
            );
          })}
        </div>
        {data.failing.length > 0 && (
          <p className="mt-3 border-t pt-2 text-[11px] text-delayed">
            {data.feeds
              .filter((f) => data.failing.includes(f.key))
              .map((f) => `${f.label}: ${f.last_error ?? f.last_status}`)
              .join(" · ")}
          </p>
        )}
        {refresh.isError && (
          <p className="mt-2 text-[11px] text-bear">
            {refresh.error instanceof Error ? refresh.error.message : "Refresh failed."}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
