"use client";

import { Wallet } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { GlossaryTerm } from "@/components/glossary-term";
import { useHoldings } from "@/lib/holdings";
import { num, pct, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * "You own this."
 *
 * Renders nothing when the trade session is unavailable rather than showing
 * an error or an empty slot: absence of a badge already reads as "not held",
 * and a broken-looking marker on every row would be worse than a missing one.
 */
export function HoldingBadge({
  code,
  className,
  showLabel = true,
}: {
  code: string;
  className?: string;
  showLabel?: boolean;
}) {
  const { heldCodes, available, positions } = useHoldings();
  if (!available || !heldCodes.has(code)) return null;

  const position = positions.find((p) => p.code === code);
  const title = position
    ? `Held: ${position.qty} @ ${position.avg_cost} ${position.currency} (${pct(position.unrealized_pnl_pct)})`
    : "You hold this";

  return (
    <Badge variant="held" className={cn("shrink-0", className)} title={title}>
      <Wallet className="h-3 w-3" aria-hidden />
      {showLabel && <span>Held</span>}
    </Badge>
  );
}

/** A percentage move, coloured by direction. */
export function ChangeBadge({
  value,
  solid = false,
  className,
}: {
  value: number | null | undefined;
  solid?: boolean;
  className?: string;
}) {
  const dir = value === null || value === undefined || value === 0 ? "flat" : value > 0 ? "bull" : "bear";
  const variant = solid
    ? dir === "bull"
      ? "solidBull"
      : dir === "bear"
        ? "solidBear"
        : "flat"
    : dir;

  return (
    <Badge variant={variant} className={cn("tabular", className)}>
      {pct(value)}
    </Badge>
  );
}

/**
 * Extended hours: pre-market, after-hours and overnight.
 *
 * Three rules, each learned from the live data rather than assumed.
 *
 * 1. **Decide by nullity, never by the clock.** Measured mid-session, all 48
 *    US tickers carried all three values at once — they are a record of the
 *    last three off-hours sessions, not an indicator of the current one. So
 *    there is no reason to branch on `market_hours.session_of`, which is
 *    what CLAUDE.md asks callers not to do while HK's midday break is
 *    unmodelled, and which has no OVERNIGHT member to branch on anyway.
 *
 * 2. **They do not share a base.** `pre` and `overnight` are measured from
 *    the previous close; `after` is measured from today's close. Verified
 *    48/48 both ways. Each therefore carries its own glossary entry saying
 *    what it is measured from, and they are never presented as a series a
 *    reader could subtract across.
 *
 * 3. **A threshold is required, not cosmetic.** Because all three are
 *    always populated, "render what is non-null" would put three extra
 *    figures on every row of every list forever. Only moves that clear
 *    EXT_HOURS_THRESHOLD_PCT are worth the space.
 */
const EXT_HOURS_THRESHOLD_PCT = 0.5;

type ExtSession = {
  key: "pre" | "after" | "overnight";
  label: string;
  glossary: string;
  pct: number;
  price: number | null;
};

/** The sessions worth showing for this ticker, in the order they occur. */
export function extendedHoursMoves(m: {
  pre_change_pct: number | null;
  pre_price: number | null;
  after_change_pct: number | null;
  after_price: number | null;
  overnight_change_pct: number | null;
  overnight_price: number | null;
}): ExtSession[] {
  const all: ExtSession[] = [
    { key: "pre", label: "Pre", glossary: "pre_market_change",
      pct: m.pre_change_pct as number, price: m.pre_price },
    { key: "after", label: "After", glossary: "after_hours_change",
      pct: m.after_change_pct as number, price: m.after_price },
    { key: "overnight", label: "O/N", glossary: "overnight_change",
      pct: m.overnight_change_pct as number, price: m.overnight_price },
  ];
  return all.filter(
    (s) => s.pct !== null && s.pct !== undefined && Math.abs(s.pct) >= EXT_HOURS_THRESHOLD_PCT,
  );
}

/**
 * The single most significant extended-hours move, labelled with its OWN
 * session name.
 *
 * This replaces a version that fell back from overnight to pre-market while
 * keeping the word "Overnight" on both — so a pre-market move was reported
 * under the wrong session's name.
 */
export function extendedHoursNote(m: Parameters<typeof extendedHoursMoves>[0]): React.ReactNode {
  const moves = extendedHoursMoves(m);
  if (moves.length === 0) return null;
  const top = moves.reduce((a, b) => (Math.abs(b.pct) > Math.abs(a.pct) ? b : a));
  return (
    <span className={top.pct < 0 ? "text-bear" : "text-bull"}>
      {top.label} {pct(top.pct)}
    </span>
  );
}

/** Every significant session as one wrapping strip. Renders nothing if none. */
export function ExtendedHours({
  mover,
  showPrice = false,
  className,
}: {
  mover: Parameters<typeof extendedHoursMoves>[0];
  showPrice?: boolean;
  className?: string;
}) {
  const moves = extendedHoursMoves(mover);
  if (moves.length === 0) return null;
  return (
    // Wrapping flex, not a table: at 320px three sessions fall to two lines
    // instead of forcing the page into a horizontal scroll (rule #6).
    <div className={cn("flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[11px]", className)}>
      {moves.map((s) => (
        <span key={s.key} className="whitespace-nowrap text-muted-foreground">
          <GlossaryTerm term={s.glossary} className="no-underline">
            {s.label}
          </GlossaryTerm>{" "}
          <span className={cn("tabular font-medium", s.pct < 0 ? "text-bear" : "text-bull")}>
            {pct(s.pct)}
          </span>
          {showPrice && s.price !== null && (
            <span className="tabular text-muted-foreground"> @ {num(s.price)}</span>
          )}
        </span>
      ))}
    </div>
  );
}

/**
 * Rule #7 made visible: a delayed quote must never be presented as live.
 * Shows how stale the data actually is, not just that it is stale.
 */
export function DelayedPill({
  isDelayed,
  asOf,
  className,
}: {
  isDelayed: boolean;
  asOf?: string | null;
  className?: string;
}) {
  if (!isDelayed) {
    return (
      <Badge variant="outline" className={cn("gap-1", className)}>
        <span className="h-1.5 w-1.5 rounded-full bg-bull animate-pulse-soft" aria-hidden />
        Live
      </Badge>
    );
  }
  return (
    <GlossaryTerm term="is_delayed_data" className="no-underline">
      <Badge variant="delayed" className={cn(className)}>
        Delayed{asOf ? ` · ${timeAgo(asOf)}` : ""}
      </Badge>
    </GlossaryTerm>
  );
}

/** Bullish / Bearish / Neutral, the thesis direction. */
export function DirectionBadge({
  direction,
  className,
}: {
  direction: string;
  className?: string;
}) {
  const variant =
    direction === "Bullish" ? "solidBull" : direction === "Bearish" ? "solidBear" : "flat";
  return (
    <Badge variant={variant} size="lg" className={className}>
      {direction}
    </Badge>
  );
}

/** Conviction 1-10, escalating emphasis at the top of the range. */
export function ConvictionBadge({ score, className }: { score: number; className?: string }) {
  const variant = score >= 8 ? "primary" : score >= 6 ? "default" : "outline";
  return (
    <GlossaryTerm term="conviction_score" className="no-underline">
      <Badge variant={variant} size="lg" className={cn("tabular", className)}>
        {score}/10
      </Badge>
    </GlossaryTerm>
  );
}
