"use client";

import { use } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Loader2, Radar } from "lucide-react";
import { NextEarnings } from "@/components/ticker/next-earnings";
import { TickerChat } from "@/components/ticker/ticker-chat";
import { ThesisHistory } from "@/components/ticker/thesis-history";
import { CandleChart } from "@/components/charts/candle-chart";
import { SetupCard } from "@/components/setup-card";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ChangeBadge,
  DelayedPill,
  ExtendedHours,
  HoldingBadge,
} from "@/components/market/indicators";
import { GlossaryTerm } from "@/components/glossary-term";
import { api } from "@/lib/api";
import { useHoldings } from "@/lib/holdings";
import { bareTicker, marketOf, num, pct, timeAgo } from "@/lib/format";

export default function TickerPage({ params }: { params: Promise<{ code: string }> }) {
  const { code: raw } = use(params);
  const code = decodeURIComponent(raw);
  const queryClient = useQueryClient();
  const { positions, available } = useHoldings();
  const position = positions.find((p) => p.code === code);

  const klines = useQuery({
    queryKey: ["klines", code],
    queryFn: () => api.klines(code),
  });

  const setup = useQuery({
    queryKey: ["setup", "latest", code],
    queryFn: () => api.latestSetup(code),
    retry: false,
  });

  const movers = useQuery({
    queryKey: ["movers", marketOf(code)],
    queryFn: () => api.movers(marketOf(code)),
    refetchInterval: 60_000,
  });
  const quote = movers.data?.movers.find((m) => m.code === code);

  const scan = useMutation({
    mutationFn: () => api.runScan([code]),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["setup", "latest", code] });
      queryClient.invalidateQueries({ queryKey: ["setups"] });
    },
  });

  return (
    <>
      <Link
        href="/watchlist"
        className="mb-3 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-3 w-3" /> Back to watchlist
      </Link>

      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">{bareTicker(code)}</h1>
            <Badge variant="outline">{marketOf(code)}</Badge>
            <HoldingBadge code={code} />
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">{quote?.name ?? code}</p>
        </div>

        <div className="flex items-center gap-3">
          {quote && (
            <div className="text-right">
              <p className="text-lg font-semibold tabular">{num(quote.last_price)}</p>
              <ChangeBadge value={quote.change_pct} />
              {/* Pre / after / overnight, each with its own price. This page
                  already had all six fields in scope and rendered none of
                  them. Shown by which sessions actually traded, never by the
                  clock — see the note on ExtendedHours. */}
              <ExtendedHours mover={quote} showPrice className="mt-1 justify-end" />
            </div>
          )}
          <Button size="sm" onClick={() => scan.mutate()} disabled={scan.isPending}>
            {scan.isPending ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" /> Scanning…
              </>
            ) : (
              <>
                <Radar className="h-4 w-4" /> Scan
              </>
            )}
          </Button>
        </div>
      </div>

      {scan.isPending && (
        <p className="mb-3 rounded-md border bg-muted/50 px-3 py-2 text-xs text-muted-foreground">
          Generating a thesis with the local model — this takes 1-2 minutes.
        </p>
      )}

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle>Daily chart</CardTitle>
            {klines.data?.available && (
              <DelayedPill
                isDelayed={klines.data.is_delayed_data}
                asOf={klines.data.data_as_of ?? ""}
              />
            )}
          </CardHeader>
          <CardContent>
            {klines.isLoading ? (
              <Skeleton className="h-[520px] w-full" />
            ) : klines.isError ? (
              <p className="py-16 text-center text-sm text-muted-foreground">
                No chart data — {klines.error instanceof Error ? klines.error.message : "unavailable"}
              </p>
            ) : klines.data && !klines.data.available ? (
              /* Not an error: this account has no market-data entitlement for
                 this exchange, so there is nothing to retry and nothing
                 broken. Said plainly, in the chart's own space, while the
                 rest of the page — the position especially — still renders. */
              <div className="py-16 text-center">
                <p className="text-sm font-medium">No chart for {klines.data.market} tickers</p>
                <p className="mx-auto mt-1.5 max-w-md text-xs leading-relaxed text-muted-foreground">
                  {klines.data.reason} Your position and holdings still work — it
                  is the price history this account cannot subscribe to, not the
                  connection.
                </p>
              </div>
            ) : klines.data ? (
              <CandleChart data={klines.data} />
            ) : null}
          </CardContent>
        </Card>

        <div className="space-y-4">
          <NextEarnings code={code} />

          {available && position && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle>Your position</CardTitle>
              </CardHeader>
              <CardContent className="space-y-1.5 text-xs">
                <Line label="Quantity">{num(position.qty, 4)}</Line>
                <Line label="Average cost">
                  {num(position.avg_cost)} {position.currency}
                </Line>
                <Line label="Market value">
                  {num(position.market_value)} {position.currency}
                </Line>
                <Line label="Unrealised P/L">
                  <span className={position.unrealized_pnl_pct && position.unrealized_pnl_pct < 0 ? "text-bear" : "text-bull"}>
                    {num(position.unrealized_pnl)} ({pct(position.unrealized_pnl_pct)})
                  </span>
                </Line>
              </CardContent>
            </Card>
          )}

          <div>
            <h2 className="mb-2 text-sm font-semibold">Latest thesis</h2>
            {setup.isLoading ? (
              <Skeleton className="h-64" />
            ) : setup.isError || !setup.data ? (
              <Card className="border-dashed p-6 text-center">
                <p className="text-xs text-muted-foreground">
                  No thesis for {bareTicker(code)} yet.
                </p>
                <Button
                  size="sm"
                  variant="outline"
                  className="mt-3"
                  onClick={() => scan.mutate()}
                  disabled={scan.isPending}
                >
                  Generate one
                </Button>
              </Card>
            ) : (
              <SetupCard setup={setup.data} />
            )}
          </div>

          {klines.data && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle>How to read this</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-xs text-muted-foreground">
                <p>
                  <GlossaryTerm term="sma">SMA 50/200</GlossaryTerm> lines show trend;{" "}
                  <GlossaryTerm term="golden_cross">crosses</GlossaryTerm> are marked on the price
                  chart.
                </p>
                <p>
                  <GlossaryTerm term="bollinger">Bollinger bands</GlossaryTerm> widen with
                  volatility — see <GlossaryTerm term="bandwidth">bandwidth</GlossaryTerm>.
                </p>
                <p>
                  <GlossaryTerm term="macd">MACD</GlossaryTerm> below tracks momentum against its{" "}
                  <GlossaryTerm term="macd_signal">signal line</GlossaryTerm>.
                </p>
                <p className="pt-1 text-[11px]">
                  {klines.data.min_rows_available} daily bars ·{" "}
                  {timeAgo(klines.data.data_as_of)}
                </p>
              </CardContent>
            </Card>
          )}
        </div>
      </div>

      {/* Full width, below the grid rather than in the right rail. The rail is
          a third of the page and already stacks three cards; a transcript plus
          chips plus an input is the tallest thing here, and a conversation
          wants line length. On mobile everything is one column anyway, so this
          lands after the thesis — chart, then thesis, then ask about it. */}
      {/* History before the chat: chart, then what the model thinks now,
          then how that read has moved, then ask about it. */}
      <ThesisHistory code={code} className="mt-4" />

      <TickerChat code={code} className="mt-4" />
    </>
  );
}

function Line({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium tabular">{children}</span>
    </div>
  );
}
