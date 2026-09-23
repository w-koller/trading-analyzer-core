"use client";

import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";
import { sanitizeNumber } from "@/lib/valuation";

/**
 * A controlled number input that keeps its own text buffer.
 *
 * A plain `value={number}` controlled input fights the user mid-keystroke —
 * clearing the field to retype a value collapses to "0" the instant the
 * field goes empty, because `Number("")` is 0. The text buffer here only
 * commits a sanitized number upstream once the buffer actually parses, and
 * re-syncs to the authoritative value on blur (or when it changes from
 * outside, e.g. a fundamentals pre-fill landing after the form already
 * mounted) — matching this file's existing input styling from
 * `watchlist/page.tsx`'s search box rather than inventing new classes.
 */
export function NumericField({
  id,
  label,
  value,
  onChange,
  suffix,
  min,
  max,
  step = "any",
  error,
  hint,
}: {
  id?: string;
  label: React.ReactNode;
  value: number;
  onChange: (v: number) => void;
  suffix?: string;
  min?: number;
  max?: number;
  step?: number | "any";
  error?: string;
  hint?: React.ReactNode;
}) {
  const [text, setText] = useState(String(value));

  useEffect(() => {
    if (Number(text) !== value) setText(String(value));
    // Only re-sync when the upstream value changes — re-running this on
    // every keystroke (i.e. depending on `text`) is exactly what would fight
    // the user while they type.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const handleChange = (raw: string) => {
    setText(raw);
    const parsed = Number(raw);
    if (raw.trim() !== "" && Number.isFinite(parsed)) {
      onChange(sanitizeNumber(parsed, { min, max }));
    }
  };

  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <div className="relative">
        <input
          id={id}
          type="number"
          inputMode="decimal"
          value={text}
          min={min}
          max={max}
          step={step}
          onChange={(e) => handleChange(e.target.value)}
          onBlur={() => setText(String(value))}
          className={cn(
            "h-9 w-full rounded-md border bg-card px-3 text-right text-sm tabular outline-none transition-colors placeholder:text-muted-foreground focus:border-primary",
            suffix && "pr-8",
            error && "border-destructive focus:border-destructive",
          )}
        />
        {suffix && (
          <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-muted-foreground">
            {suffix}
          </span>
        )}
      </div>
      {error ? (
        <span className="block text-[11px] text-destructive">{error}</span>
      ) : hint ? (
        <span className="block text-[11px] text-muted-foreground">{hint}</span>
      ) : null}
    </label>
  );
}
