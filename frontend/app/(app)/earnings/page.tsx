"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Loader2, RefreshCw, Sparkles } from "lucide-react";
import { PageHeader } from "@/components/layout/page-header";
import { HoldingBadge } from "@/components/market/indicators";
import { QueryErrorCard } from "@/components/query-error-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Empty, RowSkeletons } from "@/components/ui/section-card";
import { useMarketFilter } from "@/components/layout/market-tabs";
import { api, type EarningsEvent } from "@/lib/api";
import { bareTicker, num, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The fortnight ahead for the watchlist, and what to watch for each report.
 *
 * Grouped by day with light headers rather than a card per day: fourteen
 * cards is a wall, and the thing being scanned for is a date.
 *
 * Everything here is read from storage — the page never waits on OpenD, so
 * it keeps working while a pre-market scan owns the gateway for an hour.
 */

const PUB_TYPE_LABEL: Record<string, string> = {
  BEFORE: "Before open",
  AFTER: "After close",
  REGULAR: "During session",
  UNKNOWN: "Time unknown",
};

function EarningsView() {
  const market = useMarketFilter();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState<string | null>(null);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["earnings", market],
    queryFn: () => api.earnings({ days: 14, market }),
    refetchInterval: 5 * 60_000,
  });

  const refresh = useMutation({
    mutationFn: () => api.refreshEarnings(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["earnings"] }),
  });

  const outlook = useMutation({
    mutationFn: (code: string) => api.generateOutlook(code),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["earnings"] }),
  });

  const byDay = groupByDay(data?.events ?? []);

  return (
    <>
      <PageHeader
        title="Earnings"
        description="Upcoming reports for your watchlist, and what to watch for each."
        actions={
          <Button
            size="sm"
            variant="outline"
            onClick={() => refresh.mutate()}
            disabled={refresh.isPending}
          >
            {refresh.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            Refresh
          </Button>
        }
      />

      {refresh.isError && (
        <p className="mb-3 rounded-md border border-delayed/40 bg-delayed-muted px-3 py-2 text-xs text-delayed">
          {refresh.error instanceof Error ? refresh.error.message : "Refresh failed."}{" "}
          A running scan holds the market-data gateway; the calendar refreshes
          on its own schedule regardless.
        </p>
      )}

      {isError ? (
        <QueryErrorCard error={error} what="the earnings calendar" />
      ) : isLoading ? (
        <Card>
          <CardContent className="pt-4">
            <RowSkeletons n={6} />
          </CardContent>
        </Card>
      ) : byDay.length === 0 ? (
        <Card>
          <CardContent className="pt-4">
            <Empty>
              {data?.refreshed_at
                ? "No watchlist tickers report in the next 14 days."
                : "The calendar has not been fetched yet — hit Refresh."}
            </Empty>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          {byDay.map(([day, events]) => (
            <section key={day}>
              <h2 className="mb-1.5 px-1 text-xs font-semibold text-muted-foreground">
                {dayLabel(day, events[0].days_until)}
              </h2>
              <Card>
                <CardContent className="divide-y p-0">
                  {events.map((e) => (
                    <EventRow
                      key={e.code}
                      event={e}
                      expanded={open === e.code}
                      onToggle={() => setOpen(open === e.code ? null : e.code)}
                      onGenerate={() => outlook.mutate(e.code)}
                      generating={outlook.isPending && outlook.variables === e.code}
                    />
                  ))}
                </CardContent>
              </Card>
            </section>
          ))}
        </div>
      )}

      {outlook.isError && (
        <p className="mt-3 text-xs text-bear">
          {outlook.error instanceof Error ? outlook.error.message : "Outlook failed."}
        </p>
      )}

      {/* A permanent, known gap in the source — one muted line in the page
          flow, never a banner. It is not an outage, and banners about
          non-outages train people to ignore banners. */}
      {data?.unsupported_markets && Object.keys(data.unsupported_markets).length > 0 && (
        <p className="mt-4 px-1 text-[11px] leading-relaxed text-muted-foreground">
          {Object.keys(data.unsupported_markets).join(", ")} is not covered by
          this calendar source, so ASX reporting dates will not appear here.
          {data.refreshed_at && ` Last refreshed ${timeAgo(data.refreshed_at)}.`}
        </p>
      )}
    </>
  );
}

function EventRow({
  event,
  expanded,
  onToggle,
  onGenerate,
  generating,
}: {
  event: EarningsEvent;
  expanded: boolean;
  onToggle: () => void;
  onGenerate: () => void;
  generating: boolean;
}) {
  const o = event.outlook;
  return (
    <div className="px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Link
          href={`/ticker/${encodeURIComponent(event.code)}`}
          className="font-semibold hover:text-primary"
        >
          {bareTicker(event.code)}
        </Link>
        <HoldingBadge code={event.code} />
        <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
          {event.name}
        </span>
        <Badge variant={event.pub_type === "UNKNOWN" ? "outline" : "default"}>
          {PUB_TYPE_LABEL[event.pub_type]}
        </Badge>
        {event.iv_rank !== null && (
          <Badge variant={event.iv_rank >= 60 ? "delayed" : "outline"}>
            IV rank {num(event.iv_rank, 0)}
          </Badge>
        )}
      </div>

      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
        {event.period_text && <span>{event.period_text}</span>}
        {event.eps_predict !== null && (
          <span className="tabular">EPS est. {num(event.eps_predict, 2)}</span>
        )}
        {event.revenue_predict !== null && (
          <span className="tabular">Rev est. {compact(event.revenue_predict)}</span>
        )}
      </div>

      {o ? (
        <div className="mt-1.5">
          <button
            type="button"
            onClick={onToggle}
            className="flex w-full items-start gap-1.5 text-left text-xs hover:text-primary"
          >
            <Sparkles className="mt-0.5 h-3 w-3 shrink-0 text-primary" />
            <span className="flex-1">{o.headline}</span>
            <ChevronDown
              className={cn("mt-0.5 h-3 w-3 shrink-0 transition-transform", expanded && "rotate-180")}
            />
          </button>

          {expanded && (
            <div className="mt-2 space-y-2 border-l-2 border-border pl-3 text-xs leading-relaxed">
              {o.what_to_watch.length > 0 && (
                <div>
                  <p className="font-medium">What to watch</p>
                  <ul className="mt-0.5 list-disc space-y-0.5 pl-4 text-muted-foreground">
                    {o.what_to_watch.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                </div>
              )}
              {o.news_summary && (
                <div>
                  <p className="font-medium">Recent coverage</p>
                  <p className="text-muted-foreground">{o.news_summary}</p>
                </div>
              )}
              {o.uncertainty && (
                <div>
                  <p className="font-medium">What would make this wrong</p>
                  <p className="text-muted-foreground">{o.uncertainty}</p>
                </div>
              )}
              <p className="text-[11px] text-muted-foreground">
                Generated {o.generated_at ? timeAgo(o.generated_at) : "at an unknown time"}
                {o.model && ` by ${o.model}`}. Advisory only — it carries no
                direction, conviction or levels.
              </p>
            </div>
          )}
        </div>
      ) : (
        <button
          type="button"
          onClick={onGenerate}
          disabled={generating}
          className="mt-1.5 inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-primary disabled:opacity-50"
        >
          {generating ? (
            <>
              <Loader2 className="h-3 w-3 animate-spin" /> Writing the outlook — 1-2 minutes…
            </>
          ) : (
            <>
              <Sparkles className="h-3 w-3" /> Generate an outlook
            </>
          )}
        </button>
      )}
    </div>
  );
}

function groupByDay(events: EarningsEvent[]): [string, EarningsEvent[]][] {
  const map = new Map<string, EarningsEvent[]>();
  for (const e of events) {
    const list = map.get(e.earnings_date) ?? [];
    list.push(e);
    map.set(e.earnings_date, list);
  }
  return [...map.entries()].sort(([a], [b]) => a.localeCompare(b));
}

function dayLabel(iso: string, daysUntil: number): string {
  // Parsed as a plain calendar date, deliberately. It is an exchange-local
  // reporting date, not an instant — running it through a timezone would
  // shift it a day for anyone west of UTC.
  const [y, m, d] = iso.split("-").map(Number);
  const date = new Date(y, m - 1, d);
  const pretty = date.toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
  if (daysUntil === 0) return `Today · ${pretty}`;
  if (daysUntil === 1) return `Tomorrow · ${pretty}`;
  return `${pretty} · in ${daysUntil} days`;
}

function compact(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${(value / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  return num(value, 0);
}

export default function EarningsPage() {
  return (
    // useMarketFilter reads useSearchParams, which needs a Suspense boundary.
    <Suspense
      fallback={
        <>
          <PageHeader title="Earnings" showMarkets={false} />
          <Card>
            <CardContent className="pt-4">
              <RowSkeletons n={6} />
            </CardContent>
          </Card>
        </>
      }
    >
      <EarningsView />
    </Suspense>
  );
}
