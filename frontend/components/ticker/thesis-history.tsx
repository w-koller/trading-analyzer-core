"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown } from "lucide-react";
import { SetupCard } from "@/components/setup-card";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadMoreButton } from "@/components/ui/load-more-button";
import { ConvictionBadge, DirectionBadge } from "@/components/market/indicators";
import { api, type Setup } from "@/lib/api";
import { num, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

const PAGE = 15;

/**
 * Every thesis this tool has written about one ticker, newest first.
 *
 * The scanner re-analyses each ticker roughly hourly, so a ticker
 * accumulates 30-45 theses a day. On the Theses page that was noise and is
 * now collapsed to one row. Here it is the actual signal: a conviction
 * drifting 6->5->4, or a direction flipping Bullish->Neutral, says something
 * no single thesis can — and it is the context a human wants before acting
 * on the current one.
 *
 * Deliberately NOT a stack of SetupCards. Fifteen full cards is the same
 * wall of text the Theses page just stopped being. One scannable line each,
 * expandable to the full card on click.
 */
export function ThesisHistory({ code, className }: { code: string; className?: string }) {
  const [limit, setLimit] = useState(PAGE);
  const [openId, setOpenId] = useState<number | null>(null);

  const { data, isLoading } = useQuery({
    // The ["setups", ...] prefix matters: the scan mutation on this page
    // already calls invalidateQueries({ queryKey: ["setups"] }), so a new
    // thesis lands here with no extra wiring.
    queryKey: ["setups", "history", code, limit],
    queryFn: () => api.setups({ code, limit, sort: "recent" }),
  });

  if (isLoading) return <Skeleton className={cn("h-64 w-full", className)} />;

  const setups = data?.setups ?? [];
  // One thesis is not a history, and a section that says so is furniture —
  // the "Latest thesis" card in the rail is already showing it.
  if (setups.length <= 1) return null;

  const convictions = setups.map((s) => s.conviction_score);
  const flips = setups.filter(
    (s, i) => i > 0 && s.trade_direction !== setups[i - 1].trade_direction,
  ).length;

  return (
    <Card className={className}>
      <CardHeader className="flex-row flex-wrap items-baseline justify-between gap-2 space-y-0 pb-3">
        <CardTitle>Thesis history</CardTitle>
        <div className="flex items-center gap-3">
          <ConvictionSparkline setups={setups} />
          <span className="text-[11px] text-muted-foreground">
            {setups.length} shown · conviction {convictions[convictions.length - 1]}→
            {convictions[0]}
            {flips > 0 ? ` · ${flips} direction change${flips > 1 ? "s" : ""}` : ""}
          </span>
        </div>
      </CardHeader>

      <CardContent className="p-0">
        <ul className="divide-y">
          {setups.map((s, i) => {
            // Compared against the NEXT-OLDER row, so the arrow reads
            // "since last time" in the same direction the eye travels.
            const prev = setups[i + 1]?.conviction_score;
            const open = openId === s.id;
            return (
              <li key={s.id}>
                <button
                  onClick={() => setOpenId(open ? null : s.id)}
                  className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-left transition-colors hover:bg-muted/50"
                >
                  <span className="w-16 shrink-0 text-[11px] tabular text-muted-foreground">
                    {timeAgo(s.created_at)}
                  </span>
                  <DirectionBadge direction={s.trade_direction} />
                  <ConvictionBadge score={s.conviction_score} />
                  <Drift from={prev} to={s.conviction_score} />
                  {i === 0 && (
                    <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                      Current
                    </span>
                  )}
                  {/* Everything from here is detail, and drops away first at
                      320px so the row stays one line on a phone. */}
                  <span className="hidden shrink-0 text-[11px] tabular text-muted-foreground sm:inline">
                    {num(s.suggested_stop)} / {num(s.suggested_target)}
                  </span>
                  <span className="hidden min-w-0 flex-1 truncate text-[11px] text-muted-foreground md:inline">
                    {s.reasoning}
                  </span>
                  <ChevronDown
                    className={cn(
                      "ml-auto h-3 w-3 shrink-0 text-muted-foreground transition-transform",
                      open && "rotate-180",
                    )}
                  />
                </button>
                {open && (
                  <div className="bg-muted/30 p-3">
                    {/* The first real use of `compact`: this row IS the
                        expander, so the card must not carry another one. */}
                    <SetupCard setup={s} compact />
                  </div>
                )}
              </li>
            );
          })}
        </ul>
        <div className="pb-4">
          <LoadMoreButton
            loaded={setups.length}
            limit={limit}
            onLoadMore={() => setLimit((l) => l + PAGE)}
          />
        </div>
      </CardContent>
    </Card>
  );
}

/** Conviction 1-10 over the fetched window, oldest to newest. */
function ConvictionSparkline({ setups }: { setups: Setup[] }) {
  const scores = [...setups].reverse().map((s) => s.conviction_score);
  if (scores.length < 2) return null;

  const W = 72;
  const H = 18;
  // Fixed 1-10 domain, never auto-scaled to the data. An autoscaled axis
  // would render a 5→6 wobble as a dramatic climb, which is exactly the
  // false impression this is meant to correct.
  const x = (i: number) => (i / (scores.length - 1)) * W;
  const y = (v: number) => H - ((v - 1) / 9) * H;
  const d = scores.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");

  return (
    <svg
      width={W}
      height={H}
      viewBox={`0 0 ${W} ${H}`}
      className="shrink-0 overflow-visible text-primary"
      role="img"
      aria-label={`Conviction over time: ${scores.join(", ")} out of 10`}
    >
      {/* currentColor throughout — the theme tokens are bare HSL components
          and inheriting sidesteps parsing them by hand. */}
      <path d={d} fill="none" stroke="currentColor" strokeWidth="1.25" opacity={0.7} />
      <circle cx={x(scores.length - 1)} cy={y(scores[scores.length - 1])} r="2" fill="currentColor" />
    </svg>
  );
}

function Drift({ from, to }: { from: number | undefined; to: number }) {
  if (from === undefined || from === to) {
    return <span className="w-8 shrink-0 text-[11px] text-muted-foreground">—</span>;
  }
  const up = to > from;
  return (
    <span className={cn("w-8 shrink-0 text-[11px] tabular font-medium", up ? "text-bull" : "text-bear")}>
      {up ? "▲" : "▼"}
      {Math.abs(to - from)}
    </span>
  );
}
