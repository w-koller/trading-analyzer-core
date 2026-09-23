"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { GlossaryTerm } from "@/components/glossary-term";
import { num } from "@/lib/format";
import type { Fundamentals } from "@/lib/api";
import { calcNAV, verdict as computeVerdict } from "@/lib/valuation";
import { NumericField } from "./numeric-field";
import { VerdictCard } from "./verdict-card";
import { FormulaBreakdown } from "./formula-breakdown";
import { InterpretPanel } from "./interpret-panel";

export function NAVForm({
  code,
  marketPrice,
  fundamentals,
}: {
  code: string;
  marketPrice: number;
  fundamentals?: Fundamentals | null;
}) {
  const [assets, setAssets] = useState(0);
  const [liabilities, setLiabilities] = useState(0);
  const [preferred, setPreferred] = useState(0);
  const [sharesOutstanding, setSharesOutstanding] = useState(0);
  const [marginOfSafety, setMarginOfSafety] = useState(25);

  const prefilled = useRef(false);
  useEffect(() => {
    if (prefilled.current) return;
    if (fundamentals?.available && fundamentals.outstanding_shares) {
      setSharesOutstanding(fundamentals.outstanding_shares);
      prefilled.current = true;
    }
  }, [fundamentals]);

  const inputs = useMemo(
    () => ({ assets, liabilities, preferred, sharesOutstanding }),
    [assets, liabilities, preferred, sharesOutstanding],
  );
  const result = useMemo(() => calcNAV(inputs), [inputs]);
  const v = result.ok ? computeVerdict(result.intrinsicValuePerShare, marketPrice, marginOfSafety) : null;

  return (
    <div className="grid gap-4 xl:grid-cols-3">
      <Card className="xl:col-span-2">
        <CardHeader className="pb-2">
          <CardTitle>
            <GlossaryTerm term="nav_per_share">Net Asset Value</GlossaryTerm>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <NumericField
              label="Adjusted fair value of total assets" value={assets} onChange={setAssets} suffix="$"
            />
            <NumericField
              label="Total liabilities" value={liabilities} onChange={setLiabilities} suffix="$"
            />
            <NumericField
              label="Preferred stock / minority interest" value={preferred} onChange={setPreferred} suffix="$"
            />
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

          {fundamentals?.available && fundamentals.net_asset_per_share != null && (
            <p className="rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
              For reference, Moomoo&rsquo;s own last-reported book value per share for
              this ticker is <span className="tabular font-medium text-foreground">
                {num(fundamentals.net_asset_per_share)}
              </span> — a single computed figure, not a breakdown of assets and
              liabilities, so it can&rsquo;t fill the fields above directly.
            </p>
          )}

          {result.error && (
            <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {result.error}
            </p>
          )}

          {result.ok && <FormulaBreakdown steps={result.steps} />}

          <InterpretPanel
            code={code}
            modelKind="nav"
            inputs={{
              assets, liabilities, preferred, shares_outstanding: sharesOutstanding,
            }}
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
