"use client";

import Link from "next/link";
import { ChangeBadge, HoldingBadge } from "@/components/market/indicators";
import { bareTicker, num } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * One compact ticker line, shared by every list on the dashboard.
 *
 * `note` carries whatever made this row worth showing (a thesis direction, an
 * overnight gap), so the lists differ in reasoning without differing in shape.
 */
export function TickerRow({
  code,
  name,
  price,
  changePct,
  note,
  rank,
  className,
}: {
  code: string;
  name?: string | null;
  price?: number | null;
  changePct?: number | null;
  note?: React.ReactNode;
  rank?: number;
  className?: string;
}) {
  return (
    <Link
      href={`/ticker/${encodeURIComponent(code)}`}
      className={cn(
        "group flex items-center gap-3 rounded-md px-2 py-2 transition-colors hover:bg-muted/70",
        className,
      )}
    >
      {rank !== undefined && (
        <span className="w-4 shrink-0 text-center text-[11px] font-semibold tabular text-muted-foreground">
          {rank}
        </span>
      )}

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-sm font-semibold group-hover:text-primary">
            {bareTicker(code)}
          </span>
          <Badge variant="outline" className="px-1 py-0 text-[9px]">
            {code.split(".")[0]}
          </Badge>
          <HoldingBadge code={code} showLabel={false} />
        </div>
        {name && <p className="truncate text-[11px] text-muted-foreground">{name}</p>}
        {note && <div className="mt-0.5 text-[11px] text-muted-foreground">{note}</div>}
      </div>

      <div className="shrink-0 text-right">
        {price !== undefined && price !== null && (
          <p className="text-sm font-semibold tabular">{num(price)}</p>
        )}
        {changePct !== undefined && <ChangeBadge value={changePct} className="mt-0.5" />}
      </div>
    </Link>
  );
}
