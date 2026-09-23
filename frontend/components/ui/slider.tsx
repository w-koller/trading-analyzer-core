"use client";

import { cn } from "@/lib/utils";

/**
 * A styled native `<input type="range">`, not `@radix-ui/react-slider`.
 *
 * This repo has never added a Radix control where a plain HTML element does
 * the job — `model-selector.tsx` uses a native `<select>` for the same
 * reason — and six Radix packages including `@radix-ui/react-tabs` were
 * deliberately removed as unused (decisions #73e). One control, used once
 * (the margin-of-safety slider), does not justify a new dependency.
 */
export function Slider({
  value,
  onChange,
  min = 0,
  max = 100,
  step = 1,
  ariaLabel,
  className,
}: {
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  ariaLabel?: string;
  className?: string;
}) {
  return (
    <input
      type="range"
      value={value}
      min={min}
      max={max}
      step={step}
      aria-label={ariaLabel}
      onChange={(e) => onChange(Number(e.target.value))}
      className={cn(
        "h-1.5 w-full cursor-pointer appearance-none rounded-full bg-muted accent-primary",
        className,
      )}
    />
  );
}
