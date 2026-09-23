"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { PillGroup } from "@/components/ui/pill-group";
import { GlossaryTerm } from "@/components/glossary-term";
import { num } from "@/lib/format";
import type { Fundamentals } from "@/lib/api";
import { calcGGM, verdict as computeVerdict } from "@/lib/valuation";
import { NumericField } from "./numeric-field";
import { VerdictCard } from "./verdict-card";
import { FormulaBreakdown } from "./formula-breakdown";
import { InterpretPanel } from "./interpret-panel";

const DIVIDEND_BASIS_OPTIONS: { value: boolean; label: string }[] = [
  { value: false, label: "Most recent (D0)" },
  { value: true, label: "Next year (D1)" },
];

export function GGMForm({
  code,
  marketPrice,
  fundamentals,
}: {
  code: string;
  marketPrice: number;
  fundamentals?: Fundamentals | null;
}) {
  const [dividend, setDividend] = useState(0);
  const [useNextYear, setUseNextYear] = useState(false);
  const [growthRate, setGrowthRate] = useState(3);
  const [discountRate, setDiscountRate] = useState(8);
  const [marginOfSafety, setMarginOfSafety] = useState(25);

  const prefilled = useRef(false);
  useEffect(() => {
    if (prefilled.current) return;
    if (fundamentals?.available && fundamentals.dividend_ttm) {
      setDividend(fundamentals.dividend_ttm);
      prefilled.current = true;
    }
  }, [fundamentals]);

  const inputs = useMemo(
    () => ({ dividend, useNextYear, growthRate, discountRate }),
    [dividend, useNextYear, growthRate, discountRate],
  );
  const result = useMemo(() => calcGGM(inputs), [inputs]);
  const v = result.ok ? computeVerdict(result.intrinsicValuePerShare, marketPrice, marginOfSafety) : null;

  return (
    <div className="grid gap-4 xl:grid-cols-3">
      <Card className="xl:col-span-2">
        <CardHeader className="pb-2">
          <CardTitle>
            <GlossaryTerm term="gordon_growth">Gordon Growth Model</GlossaryTerm>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1">
            <span className="block text-xs font-medium text-muted-foreground">Dividend basis</span>
            <PillGroup options={DIVIDEND_BASIS_OPTIONS} value={useNextYear} onChange={setUseNextYear} />
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <NumericField
              label={useNextYear ? "Expected next-year dividend (D1)" : "Most recent annual dividend (D0)"}
              value={dividend} onChange={setDividend} suffix="$" min={0}
              hint={
                fundamentals?.available && fundamentals.dividend_ttm
                  ? `Pre-filled from the trailing-twelve-month dividend (${num(fundamentals.dividend_ttm)}) — an approximation of the annual rate, not the literal last payment.`
                  : undefined
              }
            />
            <NumericField
              label="Expected dividend growth rate" value={growthRate} onChange={setGrowthRate}
              suffix="%" min={-100} max={100}
            />
            <NumericField
              label={<GlossaryTerm term="discount_rate">Required rate of return</GlossaryTerm>}
              value={discountRate} onChange={setDiscountRate} suffix="%" min={-100} max={100}
            />
          </div>

          {result.error && (
            <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {result.error}
            </p>
          )}

          {result.ok && <FormulaBreakdown steps={result.steps} />}

          <InterpretPanel
            code={code}
            modelKind="ggm"
            inputs={{ dividend, growth_rate: growthRate, discount_rate: discountRate }}
            computed={
              result.ok && v
                ? {
                    intrinsic_value: result.intrinsicValuePerShare,
                    verdict: v.status,
                    market_price: marketPrice,
                    target_buy_price: v.targetBuyPrice,
                    upside_pct: v.upsidePct ?? 0,
                  }
                : null
            }
          />
        </CardContent>
      </Card>

      <VerdictCard
        intrinsicValuePerShare={result.ok ? result.intrinsicValuePerShare : null}
        error={result.error}
        marketPrice={marketPrice}
        marginOfSafetyPct={marginOfSafety}
        onMarginOfSafetyChange={setMarginOfSafety}
      />
    </div>
  );
}
