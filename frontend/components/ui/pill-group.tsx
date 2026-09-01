"use client";

import { cn } from "@/lib/utils";

/**
 * One segmented pill group — the house control for switching a view.
 *
 * Lifted out of `app/(app)/setups/page.tsx`, where it was already used three
 * times, when the sector board needed a fourth and fifth. Same test as
 * decisions #63: this is one decision about how a segmented control looks,
 * written once, not several decisions that resemble each other.
 *
 * Pair it with URL state rather than component state — `?view=`, `?window=`,
 * `?market=` — so a view is linkable and survives a refresh. `setups/page.tsx`
 * has the canonical read/write pair, and the write side must preserve every
 * other param it does not own.
 */
export function PillGroup<T>({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  ariaLabel?: string;
}) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className="inline-flex items-center gap-0.5 rounded-lg bg-muted p-0.5"
    >
      {options.map((o) => (
        <button
          key={o.label}
          type="button"
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            "rounded-md px-3 py-1 text-xs font-semibold transition-colors",
            value === o.value
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
