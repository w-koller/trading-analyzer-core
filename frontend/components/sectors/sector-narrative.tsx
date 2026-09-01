"use client";

import { useQuery } from "@tanstack/react-query";
import { Quote, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { api, type SectorWindow } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * What the model made of a sector's move.
 *
 * **This block has to look different from everything around it**, and the
 * styling is the argument rather than decoration. Every other number on the
 * sector panel is computed in Python from price and volume; this is the one
 * piece a language model wrote. Rendered in the same visual register as the
 * scores it sits beside, a reader would reasonably take it for another
 * measurement — so it gets its own tinted, bordered block, its own icon, and
 * the disclaimer inline rather than as a footnote.
 *
 * It renders NOTHING when no narrative has been written. An empty shell
 * saying "no narrative yet" would be furniture, and this is not a safety
 * surface: the score above it stands on its own.
 */
export function SectorNarrative({
  plateCode,
  window,
}: {
  plateCode: string;
  window: SectorWindow;
}) {
  const { data, isError } = useQuery({
    queryKey: ["sectors", "narrative", plateCode, window],
    queryFn: () => api.sectorNarrative(plateCode, window),
  });

  if (isError || !data?.available) return null;

  return (
    <section
      className="mt-3 rounded-lg border border-primary/25 bg-accent/40 p-2.5"
      aria-label="Model interpretation"
    >
      <div className="flex items-center gap-1.5">
        {/* accent-foreground, NOT primary. This block sits on a tinted
            bg-accent surface, and --accent-foreground is the token paired
            with it; --primary is tuned for the page background. Measured in
            the browser: primary gave 4.28:1 here in dark mode, under the
            4.5:1 floor, and 11px is too small for the large-text exemption.
            Same lesson as the sidebar's own token family (decisions #48) — a
            surface with its own background needs colours tuned for it. */}
        <Sparkles className="h-3.5 w-3.5 shrink-0 text-accent-foreground" />
        <span className="text-[11px] font-semibold uppercase tracking-wide text-accent-foreground">
          What the news says
        </span>
        <ConfidenceBadge label={data.confidence_label} />
      </div>

      <p className="mt-1.5 text-sm font-medium leading-snug">{data.headline}</p>
      <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
        {data.candidate_driver}
      </p>

      {(data.supporting_headlines?.length ?? 0) > 0 && (
        <ul className="mt-2 space-y-1">
          {data.supporting_headlines!.map((h) => (
            <li key={h} className="flex gap-1.5 text-[11px] text-muted-foreground">
              <Quote className="mt-0.5 h-2.5 w-2.5 shrink-0" />
              {/* Rendered as a quotation because it IS one: the backend only
                  accepts titles that were in the model's own prompt, so this
                  cannot name an article that does not exist. */}
              <span className="min-w-0">{h}</span>
            </li>
          ))}
        </ul>
      )}

      {data.contradicts && (
        <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
          <span className="font-medium text-foreground">Against it: </span>
          {data.contradicts}
        </p>
      )}

      {/* Inline, never a footnote. By the time a reader reaches the bottom of
          a card they have already decided what they are looking at. */}
      <p className="mt-2 border-t border-primary/15 pt-1.5 text-[10px] leading-relaxed text-muted-foreground">
        {data.disclaimer ??
          "Interpretation, not measurement. The rotation score was computed in Python and this text did not affect it."}
        {data.model && <> Written by {data.model}</>}
        {data.generated_at && <>, {timeAgo(data.generated_at)}</>}.
      </p>
    </section>
  );
}

/**
 * The model's own confidence, in its own three words.
 *
 * Never coloured green for "explains it": the label says how well the news
 * accounts for a move, not whether the move is good, and a directional colour
 * here would read as a recommendation.
 */
function ConfidenceBadge({ label }: { label?: string }) {
  if (!label) return null;
  return (
    <Badge
      variant="outline"
      className={cn(
        "ml-auto shrink-0 font-medium",
        label === "no news explains it" && "text-muted-foreground",
      )}
    >
      {label}
    </Badge>
  );
}
