"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowDownRight, ArrowUpRight } from "lucide-react";
import { Empty, RowSkeletons, SectionCard } from "@/components/ui/section-card";
import { GlossaryTerm } from "@/components/glossary-term";
import { api, type SectorScore } from "@/lib/api";
import { cn } from "@/lib/utils";

const REFETCH_MS = 300_000;
const TOP_N = 5;
/** One trading week. Long enough not to be one session's noise, short enough
 *  to still be about now — and it is the window the full view opens on. */
const DASHBOARD_WINDOW = 5 as const;

/**
 * Where money moved between sectors this week, top five each way.
 *
 * PULL, never PUSH — the same discipline `Opportunities` states, and it
 * matters more here because this engine is watchlist-wide and then some (262
 * plates rather than 50 tickers). It never raises a notification, never
 * becomes an alert, never carries a badge.
 *
 * The whole section disappears when there is nothing scored yet, rather than
 * rendering an empty shell: an always-present empty state becomes furniture,
 * and furniture is invisible exactly when it changes.
 */
export function SectorRotation({ market }: { market: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["sectors", "rotation", market, DASHBOARD_WINDOW, TOP_N],
    queryFn: () =>
      api.sectorRotation({ market: market || undefined, window: DASHBOARD_WINDOW, topN: TOP_N }),
    refetchInterval: REFETCH_MS,
  });

  // Not a safety surface — it is a ranking over data shown elsewhere — so a
  // failure is not worth a red card on the market board.
  if (isError) return null;

  if (isLoading) {
    return (
      <div className="grid gap-3 lg:grid-cols-2">
        <SectionCard
          title="Money moving in"
          description="Loading…"
          icon={<ArrowUpRight className="h-4 w-4" />}
          className="min-w-0"
        >
          <RowSkeletons n={3} />
        </SectionCard>
        <SectionCard
          title="Money moving out"
          description="Loading…"
          icon={<ArrowDownRight className="h-4 w-4" />}
          className="min-w-0"
        >
          <RowSkeletons n={3} />
        </SectionCard>
      </div>
    );
  }

  if (!data?.available) return null;
  const inflow = data.inflow ?? [];
  const outflow = data.outflow ?? [];
  if (inflow.length === 0 && outflow.length === 0) return null;

  return (
    <section>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold">Sector rotation</h2>
        <p className="text-[11px] text-muted-foreground">
          Past week, against{" "}
          <GlossaryTerm term="rotation_baseline">the median sector</GlossaryTerm>.{" "}
          <Link
            href="/watchlist?view=sectors"
            className="font-medium text-primary underline-offset-2 hover:underline"
          >
            All sectors
          </Link>
        </p>
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        <Side
          title="Money moving in"
          description="Outperformed the median sector on heavier volume"
          icon={<ArrowUpRight className="h-4 w-4" />}
          accent="text-bull"
          rows={inflow}
          board={data}
        />
        <Side
          title="Money moving out"
          description="Underperformed the median sector on heavier volume"
          icon={<ArrowDownRight className="h-4 w-4" />}
          accent="text-bear"
          rows={outflow}
          board={data}
        />
      </div>
    </section>
  );
}

function Side({
  title,
  description,
  icon,
  accent,
  rows,
  board,
}: {
  title: string;
  description: string;
  icon: React.ReactNode;
  accent: string;
  rows: SectorScore[];
  board: { min_constituents: number; min_sessions: number };
}) {
  return (
    // min-w-0 is load-bearing on a grid item: without it the column cannot
    // shrink below its own content and drags the page sideways at 412px,
    // taking the fixed bottom nav off screen with it.
    <SectionCard
      title={title}
      description={description}
      icon={icon}
      accent={accent}
      className="min-w-0"
    >
      {rows.length === 0 ? (
        <Empty>Nothing scored for this window yet.</Empty>
      ) : (
        <div className="space-y-0.5">
          {rows.map((row, i) => (
            <SectorRow key={row.plate_code} row={row} rank={i + 1} board={board} />
          ))}
        </div>
      )}
    </SectionCard>
  );
}

export function SectorRow({
  row,
  rank,
  board,
  asLink = true,
}: {
  row: SectorScore;
  rank?: number;
  board: { min_constituents: number; min_sessions: number };
  /**
   * The dashboard renders each row as its own link into the full view; the
   * board renders rows inside a <button> that opens a detail panel. An <a>
   * nested in a <button> is invalid HTML and swallows the click, so the row
   * has to be able to be neither.
   */
  asLink?: boolean;
}) {
  const score = row.score ?? 0;
  // An insufficient row keeps its numbers and loses ALL emphasis — no
  // directional colour, muted text, the shortfall stated inline. A bare
  // score is the most misleading thing on the page when it rests on three
  // constituents (decisions #69b).
  const tone = !row.sufficient
    ? "text-muted-foreground"
    : score > 0
      ? "text-bull"
      : score < 0
        ? "text-bear"
        : "text-flat";

  const inner = (
    <>
      {rank !== undefined && (
        <span className="w-4 shrink-0 text-[11px] tabular text-muted-foreground">{rank}</span>
      )}
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{row.plate_name}</span>
        <span className="block truncate text-[11px] text-muted-foreground">
          {shortfall(row, board) ?? (
            <>
              {row.constituents} names · {row.sessions_used} sessions
              {row.rel_return_pct !== null && (
                <> · {row.rel_return_pct > 0 ? "+" : ""}{row.rel_return_pct.toFixed(1)}% vs median</>
              )}
            </>
          )}
        </span>
      </span>
      <span className={cn("shrink-0 text-sm font-semibold tabular", tone)}>
        {score > 0 ? "+" : ""}
        {score.toFixed(2)}
      </span>
    </>
  );

  const className =
    "flex w-full items-center gap-2 rounded-md px-2 py-1.5 transition-colors hover:bg-muted/60";

  if (!asLink) return <span className={className}>{inner}</span>;
  return (
    <Link
      href={`/watchlist?view=sectors&plate=${encodeURIComponent(row.plate_code)}`}
      className={className}
    >
      {inner}
    </Link>
  );
}

/**
 * Why a row is not to be leaned on, in its own words.
 *
 * Returns null when the row IS sufficient, so the caller renders the normal
 * detail line instead. A 0 constituent count means the rotating member
 * refresh has not reached this plate — unknown, not empty.
 */
export function shortfall(
  row: SectorScore,
  board: { min_constituents: number; min_sessions: number },
): string | null {
  // Kept SHORT. Measured in the browser at 320-412px: the row's detail line
  // truncates, and "constituents not yet counted — treat as unconfirmed" cut
  // off mid-word as "…treat as uncon…", which reads as a rendering bug rather
  // than as a caveat. The row already signals unconfirmed by having no
  // directional colour, so the text only has to name the reason.
  if (row.sufficient) return null;
  if (!row.constituents) return "constituents not counted yet";
  if (row.constituents < board.min_constituents) {
    return `only ${row.constituents} names, needs ${board.min_constituents}`;
  }
  if (row.thin_session) return "thin session, likely a half day";
  return "unconfirmed";
}
