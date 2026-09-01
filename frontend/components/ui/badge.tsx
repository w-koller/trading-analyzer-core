import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * High-contrast metric pills. Variants are semantic (bull/bear/delayed/held)
 * rather than colour-named so a component asks for "this is a loss", not
 * "this is red" — the two themes then pick their own saturation.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-semibold leading-none transition-colors whitespace-nowrap",
  {
    variants: {
      variant: {
        default: "border-transparent bg-secondary text-secondary-foreground",
        outline: "border-border text-muted-foreground",
        bull: "border-transparent bg-bull-muted text-bull",
        bear: "border-transparent bg-bear-muted text-bear",
        flat: "border-transparent bg-flat-muted text-flat",
        delayed: "border-transparent bg-delayed-muted text-delayed",
        held: "border-transparent bg-held-muted text-held",
        solidBull: "border-transparent bg-bull text-bull-foreground",
        solidBear: "border-transparent bg-bear text-bear-foreground",
        primary: "border-transparent bg-primary text-primary-foreground",
      },
      size: {
        default: "",
        lg: "px-2 py-1 text-xs",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, size, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant, size }), className)} {...props} />;
}

export { Badge, badgeVariants };
