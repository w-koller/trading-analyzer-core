"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowDownRight, ArrowUpRight } from "lucide-react";
import { Empty, RowSkeletons, SectionCard } from "@/components/ui/section-card";
import { QueryErrorCard } from "@/components/query-error-card";
import { GlossaryTerm } from "@/components/glossary-term";
import { SectorRow } from "@/components/dashboard/sector-rotation";
import {
  api,
  type PlateClass,
  type RotationBoard as Board,
  type SectorScore,
  type SectorWindow,
} from "@/lib/api";

/**
 * The full rotation board: money in and money out, for one window.
 *
 * Deliberately the same two-ranked-lists shape as the dashboard's
 * gainers/losers cards, because that is what this actually is — gainers and
 * losers for sectors. One dimension, which is what was measured.
 *
 * A heatmap over 145 industries was the obvious alternative and is worse:
 * unreadable at 320px, colour-only encoding, and it hides the very numbers
 * that let a reader check whether a score is worth anything.
 */
export function RotationBoard({
  market,
  window,
  plateClass,
  selected,
  onSelect,
}: {
  market: string;
  window: SectorWindow;
  plateClass: PlateClass | null;
  selected: string | null;
  onSelect: (plateCode: string) => void;
}) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["sectors", "rotation", market, window, plateClass, 12],
    queryFn: () =>
      api.sectorRotation({
        market: market || undefined,
        window,
        topN: 12,
        plateClass: plateClass ?? undefined,
      }),
  });

  if (isError) return <QueryErrorCard error={error} what="the rotation board" />;

  if (isLoading) {
    return (
      <div className="grid gap-3 lg:grid-cols-2">
        <SectionCard
          title="Money moving in"
          description="Loading…"
          icon={<ArrowUpRight className="h-4 w-4" />}
          className="min-w-0"
        >
          <RowSkeletons n={6} />
        </SectionCard>
        <SectionCard
          title="Money moving out"
          description="Loading…"
          icon={<ArrowDownRight className="h-4 w-4" />}
          className="min-w-0"
        >
          <RowSkeletons n={6} />
        </SectionCard>
      </div>
    );
  }

  // "Nothing scored yet" and "we could not ask" are different things and are
  // never rendered the same way — QueryErrorCard covers the second, above.
  if (!data?.available) {
    return (
      <div className="rounded-lg border border-dashed p-10 text-center">
        <p className="text-sm font-medium">No rotation scores yet</p>
        <p className="mt-1 text-xs text-muted-foreground">
          {data?.reason ?? "The sector refresh has not run."}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="grid gap-3 lg:grid-cols-2">
        <Side
          title="Money moving in"
          description="Outran the median sector"
          icon={<ArrowUpRight className="h-4 w-4" />}
          accent="text-bull"
          rows={data.inflow}
          board={data}
          selected={selected}
          onSelect={onSelect}
        />
        <Side
          title="Money moving out"
          description="Lagged the median sector"
          icon={<ArrowDownRight className="h-4 w-4" />}
          accent="text-bear"
          rows={data.outflow}
          board={data}
          selected={selected}
          onSelect={onSelect}
        />
      </div>
      <Footnote data={data} />
    </div>
  );
}

function Side({
  title,
  description,
  icon,
  accent,
  rows,
  board,
  selected,
  onSelect,
}: {
  title: string;
  description: string;
  icon: React.ReactNode;
  accent: string;
  rows: SectorScore[];
  board: Board;
  selected: string | null;
  onSelect: (plateCode: string) => void;
}) {
  return (
    // min-w-0: a grid item is min-width:auto and cannot otherwise shrink below
    // its own content, which drags the page sideways at 412px and takes the
    // fixed bottom nav off screen with it.
    <SectionCard
      title={title}
      description={description}
      icon={icon}
      accent={accent}
      className="min-w-0"
      meta={<span className="text-[11px] tabular text-muted-foreground">{rows.length}</span>}
    >
      {rows.length === 0 ? (
        <Empty>Nothing scored for this window yet.</Empty>
      ) : (
        <div className="space-y-0.5">
          {rows.map((row, i) => (
            <button
              key={row.plate_code}
              type="button"
              aria-pressed={selected === row.plate_code}
              onClick={() => onSelect(row.plate_code)}
              className={
                "w-full rounded-md text-left " +
                (selected === row.plate_code ? "bg-accent" : "")
              }
            >
              <SectorRow row={row} rank={i + 1} board={board} asLink={false} />
            </button>
          ))}
        </div>
      )}
    </SectionCard>
  );
}

/**
 * What the numbers above rest on.
 *
 * Renders the sample counts beside the figures rather than under them,
 * because a score is only as good as the sessions and constituents behind it
 * — the rule the thesis scorecard had to learn.
 */
function Footnote({ data }: { data: Board }) {
  return (
    <p className="px-1 text-[11px] leading-relaxed text-muted-foreground">
      {data.scored} sectors scored to {data.as_of_date}; {data.sufficient} have at least{" "}
      {data.min_constituents}{" "}
      <GlossaryTerm term="sector_constituents">constituents</GlossaryTerm> and can be read
      with confidence. <GlossaryTerm term="rotation_score">Scores</GlossaryTerm> come from
      price and volume measured against{" "}
      <GlossaryTerm term="rotation_baseline">the median sector</GlossaryTerm> — computed in
      Python, not written by the model, and not a dollar amount.
      {data.thin_session ? " This session was unusually thin, likely a half day." : ""}
    </p>
  );
}
