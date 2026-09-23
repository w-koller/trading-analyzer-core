"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { PillGroup } from "@/components/ui/pill-group";
import { GlossaryTerm } from "@/components/glossary-term";
import { num } from "@/lib/format";
import type { Fundamentals } from "@/lib/api";
import {
  calcDCF, capmDiscountRate, round2, sensitivityMatrix, verdict as computeVerdict,
  type DCFForecastYears,
} from "@/lib/valuation";
import { NumericField } from "./numeric-field";
import { VerdictCard } from "./verdict-card";
import { SensitivityMatrix } from "./sensitivity-matrix";
import { CashflowChart } from "./cashflow-chart";
import { FormulaBreakdown } from "./formula-breakdown";
import { InterpretPanel } from "./interpret-panel";

const FORECAST_OPTIONS: { value: DCFForecastYears; label: string }[] = [
  { value: 5, label: "5 years" },
  { value: 7, label: "7 years" },
  { value: 10, label: "10 years" },
];

export function DCFForm({
  code,
  marketPrice,
  fundamentals,
}: {
  code: string;
  marketPrice: number;
  fundamentals?: Fundamentals | null;
}) {
  const [fcf, setFcf] = useState(0);
  const [forecastYears, setForecastYears] = useState<DCFForecastYears>(5);
  const [growthRate, setGrowthRate] = useState(8);
  const [discountRateInput, setDiscountRateInput] = useState(9);
  const [terminalGrowth, setTerminalGrowth] = useState(2.5);
  const [cash, setCash] = useState(0);
  const [debt, setDebt] = useState(0);
  const [sharesOutstanding, setSharesOutstanding] = useState(0);
  const [marginOfSafety, setMarginOfSafety] = useState(25);

  const [useCapm, setUseCapm] = useState(false);
  const [riskFreeRate, setRiskFreeRate] = useState(4.0);
  const [beta, setBeta] = useState(1.0);
  const [marketRiskPremium, setMarketRiskPremium] = useState(5.0);

  // Pre-fill shares outstanding once, when the fundamentals fetch lands —
  // never overwrite something the user has already typed.
  const prefilled = useRef(false);
  useEffect(() => {
    if (prefilled.current) return;
    if (fundamentals?.available && fundamentals.outstanding_shares) {
      setSharesOutstanding(fundamentals.outstanding_shares);
      prefilled.current = true;
    }
  }, [fundamentals]);

  const capm = useMemo(
    () => capmDiscountRate(riskFreeRate, beta, marketRiskPremium),
    [riskFreeRate, beta, marketRiskPremium],
  );
  const discountRate = useCapm ? capm.value : discountRateInput;

  const inputs = useMemo(
    () => ({
      fcf, forecastYears, growthRate, discountRate, terminalGrowth, cash,
      debt, sharesOutstanding,
    }),
    [fcf, forecastYears, growthRate, discountRate, terminalGrowth, cash, debt, sharesOutstanding],
  );

  const result = useMemo(() => calcDCF(inputs), [inputs]);
  const matrix = useMemo(() => sensitivityMatrix(inputs), [inputs]);
  const v = result.ok ? computeVerdict(result.intrinsicValuePerShare, marketPrice, marginOfSafety) : null;

  const gridValues = matrix.grid.flat().filter((x): x is number => x != null);
  const sensitivityMin = gridValues.length ? Math.min(...gridValues) : null;
  const sensitivityMax = gridValues.length ? Math.max(...gridValues) : null;

  return (
    <div className="grid gap-4 xl:grid-cols-3">
      <Card className="xl:col-span-2">
        <CardHeader className="pb-2">
          <CardTitle>
            <GlossaryTerm term="dcf">Discounted Cash Flow</GlossaryTerm>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <NumericField
              label={<GlossaryTerm term="fcf">Initial free cash flow</GlossaryTerm>}
              value={fcf} onChange={setFcf} suffix="$"
            />
            <div className="space-y-1">
              <span className="block text-xs font-medium text-muted-foreground">
                Forecast period
              </span>
              <PillGroup options={FORECAST_OPTIONS} value={forecastYears} onChange={setForecastYears} />
            </div>
            <NumericField
              label="Annual FCF growth rate" value={growthRate} onChange={setGrowthRate}
              suffix="%" min={-100} max={1000}
            />
            <NumericField
              label={<GlossaryTerm term="terminal_growth">Terminal growth rate</GlossaryTerm>}
              value={terminalGrowth} onChange={setTerminalGrowth} suffix="%" min={-100} max={100}
            />
            <NumericField
              label="Cash & short-term investments" value={cash} onChange={setCash} suffix="$"
            />
            <NumericField label="Total debt" value={debt} onChange={setDebt} suffix="$" />
            <NumericField
              label="Diluted shares outstanding" value={sharesOutstanding}
              onChange={setSharesOutstanding} min={0}
              hint={
                fundamentals?.available && fundamentals.outstanding_shares
                  ? `Pre-filled from the last snapshot (${num(fundamentals.outstanding_shares, 0)}).`
                  : undefined
              }
            />
          </div>

          <div className="space-y-2 rounded-md border bg-muted/30 p-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium">
                <GlossaryTerm term="discount_rate">Discount rate</GlossaryTerm>
              </span>
              <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                <input
                  type="checkbox" checked={useCapm}
                  onChange={(e) => setUseCapm(e.target.checked)}
                  className="h-3.5 w-3.5 accent-primary"
                />
                Derive from <GlossaryTerm term="capm">CAPM</GlossaryTerm>
              </label>
            </div>

            {useCapm ? (
              <>
                <div className="grid gap-3 sm:grid-cols-3">
                  <NumericField label="Risk-free rate" value={riskFreeRate} onChange={setRiskFreeRate} suffix="%" />
                  <NumericField label="Beta" value={beta} onChange={setBeta} />
                  <NumericField label="Market risk premium" value={marketRiskPremium} onChange={setMarketRiskPremium} suffix="%" />
                </div>
                <p className="text-xs text-muted-foreground">
                  Discount rate: <span className="tabular font-medium text-foreground">{num(capm.value)}%</span>
                </p>
              </>
            ) : (
              <NumericField label="Discount rate (WACC or cost of equity)" value={discountRateInput}
                onChange={setDiscountRateInput} suffix="%" />
            )}
          </div>

          {result.error && (
            <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {result.error}
            </p>
          )}

          {result.ok && <CashflowChart result={result} />}

          <div className="space-y-1.5">
            <p className="text-xs font-medium text-muted-foreground">Sensitivity matrix</p>
            <SensitivityMatrix
              matrix={matrix}
              currentDiscountRate={round2(discountRate)}
              currentTerminalGrowth={round2(terminalGrowth)}
            />
          </div>

          {result.ok && <FormulaBreakdown steps={result.steps} />}

          <InterpretPanel
            code={code}
            modelKind="dcf"
            inputs={{
              fcf, forecast_years: forecastYears, growth_rate: growthRate,
              discount_rate: discountRate, terminal_growth: terminalGrowth,
              cash, debt, shares_outstanding: sharesOutstanding,
            }}
            computed={
              result.ok && v
                ? {
                    intrinsic_value: result.intrinsicValuePerShare,
                    verdict: v.status,
                    market_price: marketPrice,
                    target_buy_price: v.targetBuyPrice,
                    upside_pct: v.upsidePct ?? 0,
                    sensitivity_min: sensitivityMin,
                    sensitivity_max: sensitivityMax,
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
