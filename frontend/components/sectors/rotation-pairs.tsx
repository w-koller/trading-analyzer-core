"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Shuffle } from "lucide-react";
import { SectionCard } from "@/components/ui/section-card";
import { GlossaryTerm } from "@/components/glossary-term";
import { api, type SectorWindow } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Related sectors that moved in opposite directions over the same window.
 *
 * **This is the component that most needs to under-claim.** A reader arrives
 * at "Semiconductors → AI application software" already primed to read it as
 * money physically moving, and nothing available can support that: no data
 * source here, and not 13F filings either, links a dollar leaving one sector
 * to a dollar arriving in another. A Sankey diagram would state exactly that
 * claim in the visual grammar of a conservation law, which is why this is a
 * ranked list instead.
 *
 * So the caption is inline on the card rather than a footnote, and each row
 * shows the overlap that made the two sectors related in the first place —
 * a pair is only offered when they genuinely share constituents, never
 * because they happened to sit at opposite ends of the ranking.
 */
export function RotationPairs({
  market,
  window,
  onSelect,
}: {
  market: string;
  window: SectorWindow;
  onSelect: (plateCode: string) => void;
}) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["sectors", "pairs", market, window],
    queryFn: () => api.sectorPairs({ market: market || undefined, window, topN: 6 }),
  });

  // Not a safety surface: a failure here should not put a red card next to a
  // board that rendered fine.
  if (isError || isLoading) return null;
  if (!data) return null;

  const pairs = data.pairs ?? [];

  return (
    <SectionCard
      title="Rotation pairs"
      description="Related sectors that moved opposite ways this window"
      icon={<Shuffle className="h-4 w-4" />}
      accent="text-primary"
      className="min-w-0"
    >
      {/* Inline, not a footnote: by the time a reader reaches the bottom of
          the card they have already decided what the arrow means. */}
      <p className="mb-2 px-2 text-[11px] leading-relaxed text-muted-foreground">
        Two <GlossaryTerm term="rotation_pair">related sectors</GlossaryTerm> moved in
        opposite directions over the same window. That is a correlation between them,{" "}
        <span className="font-medium text-foreground">
          not a dollar traced from one to the other
        </span>
        .
      </p>

      {!data.available ? (
        <p className="px-2 py-4 text-center text-xs text-muted-foreground">
          {data.reason}
          {data.coverage && (
            <>
              <br />
              <span className="tabular">
                {data.coverage.with_members} of {data.coverage.rows}
              </span>{" "}
              ranked sectors have constituent data so far.
            </>
          )}
        </p>
      ) : pairs.length === 0 ? (
        <p className="px-2 py-4 text-center text-xs text-muted-foreground">
          {data.reason ?? "No opposing pairs this window."}
        </p>
      ) : (
        <ul className="space-y-1">
          {pairs.map((p) => (
            <li key={`${p.from.plate_code}-${p.to.plate_code}`}>
              <div className="rounded-md px-2 py-1.5 transition-colors hover:bg-muted/60">
                <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-sm">
                  <button
                    type="button"
                    onClick={() => onSelect(p.from.plate_code)}
                    className={cn(
                      "truncate font-medium underline-offset-2 hover:underline",
                      p.from.sufficient ? "text-bear" : "text-muted-foreground",
                    )}
                  >
                    {p.from.plate_name}
                  </button>
                  <ArrowRight className="h-3 w-3 shrink-0 text-muted-foreground" />
                  <button
                    type="button"
                    onClick={() => onSelect(p.to.plate_code)}
                    className={cn(
                      "truncate font-medium underline-offset-2 hover:underline",
                      p.to.sufficient ? "text-bull" : "text-muted-foreground",
                    )}
                  >
                    {p.to.plate_name}
                  </button>
                </div>
                <p className="mt-0.5 text-[11px] text-muted-foreground">
                  <span className="tabular">
                    {p.from.score.toFixed(2)} → {p.to.score > 0 ? "+" : ""}
                    {p.to.score.toFixed(2)}
                  </span>{" "}
                  · <span className="tabular">{p.shared_members}</span> shared names
                  {!p.both_sufficient && " · unconfirmed"}
                </p>
              </div>
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  );
}
