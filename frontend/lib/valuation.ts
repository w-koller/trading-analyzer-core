/**
 * DCF / Gordon Growth / NAV valuation math.
 *
 * Pure functions, no React, no network — every result is derived entirely
 * from its arguments so a slider can call this on every frame with zero
 * round-trips. This is deliberately NOT the kind of "technical indicator"
 * CLAUDE.md rule #1 reserves for deterministic backend computation: there is
 * no canonical answer here, only arithmetic over assumptions the user is
 * actively adjusting, and no LLM is ever asked to compute anything in this
 * file or downstream of it.
 *
 * Every calc* function returns both the final number(s) and an ordered
 * `steps` array — the same arithmetic, labelled, so the "under the hood"
 * accordion can render real numbers rather than re-deriving the formula in
 * JSX. A function that cannot produce an answer (r <= g, zero shares)
 * returns `ok: false` with a human `error` rather than NaN/Infinity.
 */

import { num } from "./format";

export type CalcStep = { label: string; expression: string; value: number };

function pow1p(ratePct: number, n: number): number {
  return Math.pow(1 + ratePct / 100, n);
}

/** Clamps a live input to a plausible range, and never lets NaN/Infinity through. */
export function sanitizeNumber(
  value: number,
  opts: { min?: number; max?: number } = {},
): number {
  if (!Number.isFinite(value)) return 0;
  let v = value;
  if (opts.min !== undefined) v = Math.max(opts.min, v);
  if (opts.max !== undefined) v = Math.min(opts.max, v);
  return v;
}

// --- DCF ---------------------------------------------------------------

export type DCFForecastYears = 5 | 7 | 10;

export type DCFInputs = {
  fcf: number;
  forecastYears: DCFForecastYears;
  growthRate: number;      // percent, e.g. 8 means 8%
  discountRate: number;    // percent
  terminalGrowth: number;  // percent
  cash: number;
  debt: number;
  sharesOutstanding: number;
};

export type DCFResult = {
  ok: boolean;
  error?: string;
  projectedFCF: { year: number; fcf: number; pv: number }[];
  terminalValue: number;
  pvTerminalValue: number;
  enterpriseValue: number;
  equityValue: number;
  intrinsicValuePerShare: number;
  steps: CalcStep[];
};

const DCF_EMPTY: Omit<DCFResult, "ok" | "error"> = {
  projectedFCF: [],
  terminalValue: 0,
  pvTerminalValue: 0,
  enterpriseValue: 0,
  equityValue: 0,
  intrinsicValuePerShare: 0,
  steps: [],
};

export function calcDCF(inputs: DCFInputs): DCFResult {
  const {
    fcf, forecastYears, growthRate, discountRate, terminalGrowth, cash, debt,
    sharesOutstanding,
  } = inputs;

  if (sharesOutstanding <= 0) {
    return { ok: false, error: "Shares outstanding must be greater than zero.", ...DCF_EMPTY };
  }
  if (discountRate <= terminalGrowth) {
    return {
      ok: false,
      error: "Discount rate must be greater than the terminal growth rate — otherwise the terminal value is undefined.",
      ...DCF_EMPTY,
    };
  }

  const steps: CalcStep[] = [];
  const projectedFCF: DCFResult["projectedFCF"] = [];
  let sumPV = 0;
  let priorFCF = fcf;

  for (let year = 1; year <= forecastYears; year++) {
    const yearFCF = priorFCF * (1 + growthRate / 100);
    const pv = yearFCF / pow1p(discountRate, year);
    projectedFCF.push({ year, fcf: yearFCF, pv });
    sumPV += pv;
    steps.push({
      label: `Year ${year} projected FCF`,
      expression: `${num(priorFCF)} × (1 + ${num(growthRate)}%) = ${num(yearFCF)}`,
      value: yearFCF,
    });
    steps.push({
      label: `Year ${year} present value`,
      expression: `${num(yearFCF)} ÷ (1 + ${num(discountRate)}%)^${year} = ${num(pv)}`,
      value: pv,
    });
    priorFCF = yearFCF;
  }

  const terminalFCF = priorFCF * (1 + terminalGrowth / 100);
  const terminalValue = terminalFCF / ((discountRate - terminalGrowth) / 100);
  const pvTerminalValue = terminalValue / pow1p(discountRate, forecastYears);
  steps.push({
    label: "Terminal value",
    expression: `${num(terminalFCF)} ÷ (${num(discountRate)}% − ${num(terminalGrowth)}%) = ${num(terminalValue)}`,
    value: terminalValue,
  });
  steps.push({
    label: "PV of terminal value",
    expression: `${num(terminalValue)} ÷ (1 + ${num(discountRate)}%)^${forecastYears} = ${num(pvTerminalValue)}`,
    value: pvTerminalValue,
  });

  const enterpriseValue = sumPV + pvTerminalValue;
  steps.push({
    label: "Enterprise value",
    expression: `${num(sumPV)} (sum of PVs) + ${num(pvTerminalValue)} (PV of terminal value) = ${num(enterpriseValue)}`,
    value: enterpriseValue,
  });

  const equityValue = enterpriseValue + cash - debt;
  steps.push({
    label: "Equity value",
    expression: `${num(enterpriseValue)} + ${num(cash)} (cash) − ${num(debt)} (debt) = ${num(equityValue)}`,
    value: equityValue,
  });

  const intrinsicValuePerShare = equityValue / sharesOutstanding;
  steps.push({
    label: "Intrinsic value per share",
    expression: `${num(equityValue)} ÷ ${num(sharesOutstanding, 0)} shares = ${num(intrinsicValuePerShare)}`,
    value: intrinsicValuePerShare,
  });

  return {
    ok: true, projectedFCF, terminalValue, pvTerminalValue, enterpriseValue,
    equityValue, intrinsicValuePerShare, steps,
  };
}

/** Optional helper for filling the discount-rate field: Rf + beta × ERP. */
export function capmDiscountRate(
  riskFreeRate: number, beta: number, marketRiskPremium: number,
): { value: number; steps: CalcStep[] } {
  const value = riskFreeRate + beta * marketRiskPremium;
  return {
    value,
    steps: [{
      label: "CAPM discount rate",
      expression: `${num(riskFreeRate)}% + ${num(beta)} × ${num(marketRiskPremium)}% = ${num(value)}%`,
      value,
    }],
  };
}

// --- Gordon Growth / Dividend Discount ----------------------------------

export type GGMInputs = {
  dividend: number;
  /** true: `dividend` IS next year's D1. false: `dividend` is D0 and gets grown one year. */
  useNextYear: boolean;
  growthRate: number;
  discountRate: number;
};

export type GGMResult = {
  ok: boolean;
  error?: string;
  d1: number;
  intrinsicValuePerShare: number;
  steps: CalcStep[];
};

export function calcGGM(inputs: GGMInputs): GGMResult {
  const { dividend, useNextYear, growthRate, discountRate } = inputs;

  if (discountRate <= growthRate) {
    return {
      ok: false,
      error: "Required return must be greater than the dividend growth rate — otherwise the model is undefined.",
      d1: 0, intrinsicValuePerShare: 0, steps: [],
    };
  }

  const steps: CalcStep[] = [];
  const d1 = useNextYear ? dividend : dividend * (1 + growthRate / 100);
  if (!useNextYear) {
    steps.push({
      label: "Next year's dividend (D1)",
      expression: `${num(dividend)} × (1 + ${num(growthRate)}%) = ${num(d1)}`,
      value: d1,
    });
  }

  const intrinsicValuePerShare = d1 / ((discountRate - growthRate) / 100);
  steps.push({
    label: "Intrinsic value per share",
    expression: `${num(d1)} ÷ (${num(discountRate)}% − ${num(growthRate)}%) = ${num(intrinsicValuePerShare)}`,
    value: intrinsicValuePerShare,
  });

  return { ok: true, d1, intrinsicValuePerShare, steps };
}

// --- NAV -----------------------------------------------------------------

export type NAVInputs = {
  assets: number;
  liabilities: number;
  preferred: number;
  sharesOutstanding: number;
};

export type NAVResult = {
  ok: boolean;
  error?: string;
  intrinsicValuePerShare: number;
  steps: CalcStep[];
};

export function calcNAV(inputs: NAVInputs): NAVResult {
  const { assets, liabilities, preferred, sharesOutstanding } = inputs;

  if (sharesOutstanding <= 0) {
    return {
      ok: false, error: "Shares outstanding must be greater than zero.",
      intrinsicValuePerShare: 0, steps: [],
    };
  }

  const steps: CalcStep[] = [];
  const netAssets = assets - liabilities - preferred;
  steps.push({
    label: "Net assets",
    expression: `${num(assets)} − ${num(liabilities)} − ${num(preferred)} = ${num(netAssets)}`,
    value: netAssets,
  });

  const intrinsicValuePerShare = netAssets / sharesOutstanding;
  steps.push({
    label: "NAV per share",
    expression: `${num(netAssets)} ÷ ${num(sharesOutstanding, 0)} shares = ${num(intrinsicValuePerShare)}`,
    value: intrinsicValuePerShare,
  });

  return { ok: true, intrinsicValuePerShare, steps };
}

// --- sensitivity matrix (DCF only) --------------------------------------

export const DEFAULT_RATE_DELTAS = [-2, -1, 0, 1, 2];
export const DEFAULT_GROWTH_DELTAS = [-1, -0.5, 0, 0.5, 1];

export type SensitivityMatrix = {
  discountRates: number[];
  terminalGrowths: number[];
  /** [row=terminalGrowth][col=discountRate], null where that combination is undefined (r <= g). */
  grid: (number | null)[][];
};

export function sensitivityMatrix(
  base: DCFInputs,
  rateDeltas: number[] = DEFAULT_RATE_DELTAS,
  growthDeltas: number[] = DEFAULT_GROWTH_DELTAS,
): SensitivityMatrix {
  const discountRates = rateDeltas.map((d) => round2(base.discountRate + d));
  const terminalGrowths = growthDeltas.map((d) => round2(base.terminalGrowth + d));

  const grid = terminalGrowths.map((g) =>
    discountRates.map((r) => {
      const result = calcDCF({ ...base, discountRate: r, terminalGrowth: g });
      return result.ok ? result.intrinsicValuePerShare : null;
    }),
  );

  return { discountRates, terminalGrowths, grid };
}

export function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

// --- market comparison / margin of safety -------------------------------

export type ValuationStatus = "undervalued" | "fair" | "overvalued";

export type Verdict = {
  status: ValuationStatus;
  targetBuyPrice: number;
  /** null when the market price is not a usable positive number. */
  upsidePct: number | null;
};

/**
 * `marketPrice` must be a real positive quote for `upsidePct`/`status` to
 * mean anything — callers should not invoke this before the price field has
 * a value. A non-positive price degrades to a "fair" status with no upside
 * figure rather than dividing by zero.
 */
export function verdict(
  intrinsicValuePerShare: number,
  marketPrice: number,
  marginOfSafetyPct: number,
): Verdict {
  const targetBuyPrice = intrinsicValuePerShare * (1 - marginOfSafetyPct / 100);

  if (marketPrice <= 0) {
    return { status: "fair", targetBuyPrice, upsidePct: null };
  }

  const upsidePct = ((intrinsicValuePerShare - marketPrice) / marketPrice) * 100;
  let status: ValuationStatus;
  if (marketPrice <= targetBuyPrice) {
    status = "undervalued";
  } else if (marketPrice > intrinsicValuePerShare) {
    status = "overvalued";
  } else {
    status = "fair";
  }

  return { status, targetBuyPrice, upsidePct };
}
