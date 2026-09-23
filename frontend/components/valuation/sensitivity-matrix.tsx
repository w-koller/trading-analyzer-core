import { GlossaryTerm } from "@/components/glossary-term";
import { num, DASH } from "@/lib/format";
import type { SensitivityMatrix as SensitivityMatrixData } from "@/lib/valuation";

/**
 * Discount rate (columns) × terminal growth (rows), the only existing table
 * pattern in this codebase (`watchlist/page.tsx`'s ticker table) — a plain
 * `<table>` in an `overflow-x-auto` wrapper rather than a component library.
 */
export function SensitivityMatrix({
  matrix,
  currentDiscountRate,
  currentTerminalGrowth,
}: {
  matrix: SensitivityMatrixData;
  currentDiscountRate: number;
  currentTerminalGrowth: number;
}) {
  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full min-w-[420px] text-xs">
        <thead>
          <tr className="border-b bg-muted/40 text-left uppercase tracking-wide text-muted-foreground">
            <th className="px-2.5 py-1.5 text-left font-medium">
              <GlossaryTerm term="terminal_growth">Term. growth</GlossaryTerm>
              {" ∕ "}
              <GlossaryTerm term="discount_rate">discount rate</GlossaryTerm>
            </th>
            {matrix.discountRates.map((r) => (
              <th
                key={r}
                className={
                  "px-2.5 py-1.5 text-right font-medium tabular " +
                  (r === currentDiscountRate ? "text-foreground" : "")
                }
              >
                {num(r, 1)}%
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.terminalGrowths.map((g, rowIdx) => (
            <tr key={g} className="border-b last:border-0">
              <th
                scope="row"
                className={
                  "px-2.5 py-1.5 text-left font-medium tabular " +
                  (g === currentTerminalGrowth ? "text-foreground" : "text-muted-foreground")
                }
              >
                {num(g, 1)}%
              </th>
              {matrix.grid[rowIdx].map((value, colIdx) => {
                const isCurrent =
                  g === currentTerminalGrowth &&
                  matrix.discountRates[colIdx] === currentDiscountRate;
                return (
                  <td
                    key={colIdx}
                    className={
                      "px-2.5 py-1.5 text-right tabular " +
                      (isCurrent ? "bg-primary/10 font-semibold text-primary" : "")
                    }
                  >
                    {value != null ? num(value) : DASH}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
