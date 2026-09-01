"use client";

import * as React from "react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useIsTouch } from "@/hooks/use-media-query";
import { glossaryEntry } from "@/lib/glossary";
import { cn } from "@/lib/utils";

/**
 * A term the reader can ask about.
 *
 * Built on Popover rather than Tooltip because it has to work on a phone:
 * a tooltip only opens on hover, and touch devices have no hover, so on
 * mobile the definitions would simply be unreachable. Popover takes a
 * controlled `open`, so one component covers both — hover on a mouse,
 * tap on a touchscreen.
 */
export function GlossaryTerm({
  term,
  children,
  className,
}: {
  term: string;
  children?: React.ReactNode;
  className?: string;
}) {
  const entry = glossaryEntry(term);
  const isTouch = useIsTouch();
  const [open, setOpen] = React.useState(false);

  // An unknown key should show plain text, not an invisible dead control.
  if (!entry) return <>{children ?? term}</>;

  const hoverProps = isTouch
    ? {}
    : { onMouseEnter: () => setOpen(true), onMouseLeave: () => setOpen(false) };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        asChild
        onClick={(e) => {
          e.preventDefault();
          if (isTouch) setOpen((v) => !v);
        }}
      >
        <button
          type="button"
          aria-label={`What is ${entry.term}?`}
          className={cn(
            "cursor-help underline decoration-dotted decoration-muted-foreground/60 underline-offset-4 hover:decoration-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring rounded-sm text-left",
            className,
          )}
          {...hoverProps}
        >
          {children ?? entry.term}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-72"
        onOpenAutoFocus={(e) => e.preventDefault()}
      >
        <p className="text-xs font-semibold text-foreground">{entry.term}</p>
        <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{entry.short}</p>
        {entry.detail && (
          <p className="mt-2 border-t border-border pt-2 text-[11px] leading-relaxed text-muted-foreground/80">
            {entry.detail}
          </p>
        )}
      </PopoverContent>
    </Popover>
  );
}
