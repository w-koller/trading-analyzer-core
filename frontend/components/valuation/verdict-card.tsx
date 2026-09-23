"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Slider } from "@/components/ui/slider";
import { GlossaryTerm } from "@/components/glossary-term";
import { num, pct, DASH } from "@/lib/format";
import { verdict as computeVerdict, type ValuationStatus } from "@/lib/valuation";

const STATUS_LABEL: Record<ValuationStatus, string> = {
  undervalued: "Undervalued",
  fair: "Fairly valued",
  overvalued: "Overvalued",
};

const STATUS_VARIANT: Record<ValuationStatus, "bull" | "bear" | "flat"> = {
  undervalued: "bull",
  fair: "flat",
  overvalued: "bear",
};

/**
 * Shared across all three models — the DCF/GGM/NAV form beside it supplies
 * only `intrinsicValuePerShare`, everything else here (market price, target
 * price, badge, upside %) is common machinery.
 */
export function VerdictCard({
  intrinsicValuePerShare,
  error,
  marketPrice,
  marginOfSafetyPct,
  onMarginOfSafetyChange,
}: {
  intrinsicValuePerShare: number | null;
  error?: string;
  marketPrice: number;
  marginOfSafetyPct: number;
  onMarginOfSafetyChange: (v: number) => void;
}) {
  const result =
    intrinsicValuePerShare != null
      ? computeVerdict(intrinsicValuePerShare, marketPrice, marginOfSafetyPct)
      : null;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle>Verdict</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {error ? (
          <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {error}
          </p>
        ) : (
          <>
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div>
                <p className="text-xs text-muted-foreground">Intrinsic value / share</p>
                <p className="text-2xl font-semibold tabular">
                  {num(intrinsicValuePerShare)}
                </p>
              </div>
              {result && (
                <Badge variant={STATUS_VARIANT[result.status]} size="lg">
                  {STATUS_LABEL[result.status]}
                </Badge>
              )}
            </div>

            <dl className="space-y-1.5 text-xs">
              <Row label="Current market price"
                   value={marketPrice > 0 ? num(marketPrice) : DASH} />
              <Row label="Target buy price" value={num(result?.targetBuyPrice)} />
              <Row label="Upside / downside" value={pct(result?.upsidePct)} />
            </dl>

            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-xs">
                <span className="font-medium text-muted-foreground">
                  <GlossaryTerm term="margin_of_safety">Margin of safety</GlossaryTerm>
                </span>
                <span className="tabular font-medium">{marginOfSafetyPct}%</span>
              </div>
              <Slider
                value={marginOfSafetyPct}
                onChange={onMarginOfSafetyChange}
                min={0}
                max={50}
                step={1}
                ariaLabel="Margin of safety"
              />
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function Row({ label, value }: { label: React.ReactNode; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium tabular">{value}</dd>
    </div>
  );
}
