import type { CalcStep } from "@/lib/valuation";

/**
 * "Under the hood": the exact arithmetic a `calc*` function already
 * produced, not re-derived here. Plain `<details>/<summary>` rather than a
 * Radix Accordion — see `components/ui/slider.tsx` for the same reasoning
 * against adding a dependency for one control.
 */
export function FormulaBreakdown({ steps }: { steps: CalcStep[] }) {
  if (steps.length === 0) return null;
  return (
    <details className="group rounded-md border bg-card px-3 py-2 text-xs">
      <summary className="cursor-pointer select-none font-medium text-muted-foreground group-open:text-foreground">
        Under the hood — step by step
      </summary>
      <ol className="mt-2 space-y-1.5 border-t pt-2">
        {steps.map((step, i) => (
          <li key={i} className="flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:justify-between sm:gap-3">
            <span className="text-muted-foreground">{step.label}</span>
            <span className="tabular font-mono text-[11px] text-foreground">{step.expression}</span>
          </li>
        ))}
      </ol>
    </details>
  );
}
