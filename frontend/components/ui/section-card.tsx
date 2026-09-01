import * as React from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * A titled panel holding a short list.
 *
 * Shared by the dashboard's gainers/losers/watch cards and the news grid, so
 * the two read as the same kind of thing rather than two designs that happen
 * to sit on one page.
 *
 * `meta` is the top-right slot: freshness labels, delayed-data pills, feed
 * health. `accent` tints the icon only — the card itself stays neutral so a
 * row of them doesn't turn into a colour chart.
 */
export function SectionCard({
  title,
  description,
  icon,
  accent,
  children,
  meta,
  className,
}: {
  title: string;
  description: string;
  icon: React.ReactNode;
  accent?: string;
  children: React.ReactNode;
  meta?: React.ReactNode;
  /**
   * Escape hatch for the CARD's own box, not its contents. Exists because a
   * grid item defaults to `min-width: auto` and so cannot shrink below its
   * own content — a caller holding anything with a min-width has to pass
   * `min-w-0` here, or the column drags the whole page past the viewport.
   */
  className?: string;
}) {
  return (
    <Card className={cn("flex flex-col", className)}>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className={accent}>{icon}</span>
            <CardTitle>{title}</CardTitle>
          </div>
          {meta}
        </div>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="flex-1 px-2 pb-2">{children}</CardContent>
    </Card>
  );
}

/** Placeholder rows sized like the real ones, so loading doesn't reflow. */
export function RowSkeletons({ n = 5 }: { n?: number }) {
  return (
    <div className="space-y-1 px-2">
      {Array.from({ length: n }).map((_, i) => (
        <div key={i} className="flex items-center gap-3 py-2">
          <Skeleton className="h-8 flex-1" />
          <Skeleton className="h-6 w-16" />
        </div>
      ))}
    </div>
  );
}

/** "Nothing here" — distinct from "we couldn't ask", which is QueryErrorCard. */
export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="px-2 py-6 text-center text-xs text-muted-foreground">{children}</p>;
}
