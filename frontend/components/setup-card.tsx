"use client";

import { useState } from "react";
import Link from "next/link";
import { ChevronDown } from "lucide-react";
import type { Setup } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { GlossaryTerm } from "@/components/glossary-term";
import {
  ConvictionBadge,
  DelayedPill,
  DirectionBadge,
  HoldingBadge,
} from "@/components/market/indicators";
import { bareTicker, num, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

function Row({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1">
      <dt className="shrink-0 text-xs text-muted-foreground">{label}</dt>
      <dd className="text-right text-xs font-medium tabular">{children}</dd>
    </div>
  );
}

export function SetupCard({ setup, compact = false }: { setup: Setup; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const ind = setup.indicator_snapshot?.indicators;
  const walls = setup.indicator_snapshot?.walls;
  // Distance from the last close to the suggested entry, as a signed
  // percentage. Guarded on a truthy close because a missing or zero price
  // would render Infinity.
  const entryGapPct =
    setup.suggested_entry !== null && ind?.close
      ? ((setup.suggested_entry - ind.close) / ind.close) * 100
      : null;

  return (
    <Card className="overflow-hidden">
      <header className="flex flex-wrap items-start justify-between gap-2 p-4 pb-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Link
              href={`/ticker/${encodeURIComponent(setup.code)}`}
              className="text-base font-semibold hover:text-primary"
            >
              {bareTicker(setup.code)}
            </Link>
            <span className="text-xs text-muted-foreground">{setup.market}</span>
            <HoldingBadge code={setup.code} />
          </div>
          <p className="mt-0.5 text-[11px] text-muted-foreground">
            {timeAgo(setup.created_at)} · {setup.indicator_snapshot?.bars_used} bars
            {setup.indicator_snapshot?.session ? ` · ${setup.indicator_snapshot.session}` : ""}
            {/* Only the deduped list supplies thesis_count, so this lights up
                exactly where one card stands in for many and nowhere else —
                the dashboard and ticker-page cards are untouched. */}
            {(setup.thesis_count ?? 0) > 1 && (
              <>
                {" · "}
                <Link
                  href={`/ticker/${encodeURIComponent(setup.code)}`}
                  className="underline decoration-dotted underline-offset-2 hover:text-foreground"
                >
                  {setup.thesis_count} theses
                </Link>
              </>
            )}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <DelayedPill isDelayed={setup.is_delayed_data} asOf={setup.data_as_of} />
          <DirectionBadge direction={setup.trade_direction} />
          <ConvictionBadge score={setup.conviction_score} />
        </div>
      </header>

      <div className="px-4 pb-3">
        <p className="text-sm leading-relaxed">{setup.reasoning}</p>

        <dl className="mt-3 grid grid-cols-1 gap-x-8 sm:grid-cols-2">
          <Row label="Last close">{num(ind?.close)}</Row>
          <Row label={<GlossaryTerm term="suggested_entry">Suggested entry</GlossaryTerm>}>
            {setup.suggested_entry === null ? (
              <span className="font-normal text-muted-foreground">at market</span>
            ) : (
              <>
                {num(setup.suggested_entry)}
                {/* How far away the entry sits. The validator checks that the
                    three levels are ORDERED, never that the entry is near
                    today's price — that is judgement, not incoherence. So an
                    entry the price may not reach for weeks is legal, and this
                    is what keeps it from reading as "buy now". */}
                {entryGapPct !== null && (
                  <span className="ml-1 font-normal text-muted-foreground">
                    ({entryGapPct > 0 ? "+" : ""}
                    {entryGapPct.toFixed(1)}%)
                  </span>
                )}
              </>
            )}
          </Row>
          <Row label={<GlossaryTerm term="suggested_stop">Suggested stop</GlossaryTerm>}>
            <span className="text-bear">{num(setup.suggested_stop)}</span>
          </Row>
          <Row label={<GlossaryTerm term="suggested_target">Suggested target</GlossaryTerm>}>
            <span className="text-bull">{num(setup.suggested_target)}</span>
          </Row>
          <Row label={<GlossaryTerm term="sma">SMA 50 / 200</GlossaryTerm>}>
            {num(ind?.sma_fast)} / {num(ind?.sma_slow)}{" "}
            <span
              className={cn(
                "font-normal",
                ind?.sma_trend === "bullish"
                  ? "text-bull"
                  : ind?.sma_trend === "bearish"
                    ? "text-bear"
                    : "text-muted-foreground",
              )}
            >
              ({ind?.sma_trend})
            </span>
          </Row>
        </dl>

        {setup.key_levels_notes && (
          <p className="mt-2 text-xs text-muted-foreground">{setup.key_levels_notes}</p>
        )}
      </div>

      {!compact && (
        <>
          <Separator />
          <button
            onClick={() => setOpen((v) => !v)}
            className="flex w-full items-center justify-center gap-1 py-2 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground"
          >
            {open ? "Hide" : "Show"} indicator detail
            <ChevronDown className={cn("h-3 w-3 transition-transform", open && "rotate-180")} />
          </button>
        </>
      )}

      {open && ind && (
        <div className="grid grid-cols-1 gap-x-8 border-t bg-muted/30 px-4 py-3 sm:grid-cols-2">
          <Row label={<GlossaryTerm term="macd">MACD / signal</GlossaryTerm>}>
            {num(ind.macd, 3)} / {num(ind.macd_signal, 3)}
          </Row>
          <Row label={<GlossaryTerm term="macd_hist">MACD histogram</GlossaryTerm>}>
            {num(ind.macd_hist, 3)}{" "}
            <span
              className={cn(
                "font-normal",
                ind.macd_state === "bullish" ? "text-bull" : ind.macd_state === "bearish" ? "text-bear" : "text-muted-foreground",
              )}
            >
              ({ind.macd_state})
            </span>
          </Row>
          <Row label={<GlossaryTerm term="bollinger">Bollinger u/m/l</GlossaryTerm>}>
            {num(ind.bb_upper)} / {num(ind.bb_mid)} / {num(ind.bb_lower)}
          </Row>
          <Row label={<GlossaryTerm term="percent_b">%B</GlossaryTerm>}>
            {num(ind.bb_percent_b, 3)}{" "}
            <span className="font-normal text-muted-foreground">({ind.bb_state})</span>
          </Row>
          <Row label={<GlossaryTerm term="sma_cross">SMA cross</GlossaryTerm>}>{ind.sma_cross}</Row>
          <Row label={<GlossaryTerm term="macd">MACD cross</GlossaryTerm>}>{ind.macd_cross}</Row>
          {walls?.has_walls && (
            <>
              <Row label={<GlossaryTerm term="call_wall">{`Call wall (${walls.expiry})`}</GlossaryTerm>}>
                {num(walls.call_wall)}{" "}
                <span className="font-normal text-muted-foreground">
                  ({num(walls.call_wall_distance_pct, 1)}%)
                </span>
              </Row>
              <Row label={<GlossaryTerm term="put_wall">Put wall</GlossaryTerm>}>
                {num(walls.put_wall)}{" "}
                <span className="font-normal text-muted-foreground">
                  ({num(walls.put_wall_distance_pct, 1)}%)
                </span>
              </Row>
              <Row label={<GlossaryTerm term="put_call_ratio">P/C by OI</GlossaryTerm>}>
                {num(walls.put_call_oi_ratio, 3)}
              </Row>
              <Row label={<GlossaryTerm term="put_call_ratio">P/C by volume</GlossaryTerm>}>
                {num(walls.put_call_volume_ratio, 3)}
              </Row>
            </>
          )}
          {ind.warnings?.length > 0 && (
            <p className="col-span-full mt-2 text-[11px] text-delayed">{ind.warnings.join(" · ")}</p>
          )}
          <p className="col-span-full mt-2 text-[11px] text-muted-foreground">
            <GlossaryTerm term="similar_setups">RAG</GlossaryTerm>:{" "}
            {setup.similar_setup_ids?.length > 0
              ? `${setup.similar_setup_ids.length} historical setup(s) injected into the prompt (#${setup.similar_setup_ids.join(", #")})`
              : "no comparable historical setups with outcomes yet"}
          </p>
        </div>
      )}
    </Card>
  );
}
