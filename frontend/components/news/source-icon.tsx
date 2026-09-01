"use client";

import { Building2, FileText, Globe2, Landmark, Newspaper, TrendingUp } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * A glyph per source family, tinted by category.
 *
 * Deliberately not remote favicons: fetching 16 third-party icons would tell
 * those sites what this LAN reads, break whenever the backend is the only
 * thing reachable, and add uncacheable requests to every render.
 */
const ICONS: Record<string, typeof Globe2> = {
  filing: FileText,
  bank: Landmark,
  markets: TrendingUp,
  globe: Globe2,
  news: Newspaper,
  company: Building2,
};

const TINT: Record<string, string> = {
  shocks: "text-bear",
  themes: "text-primary",
  macro: "text-muted-foreground",
};

export function SourceIcon({
  icon,
  category,
  className,
}: {
  icon: string;
  category?: string;
  className?: string;
}) {
  const Glyph = ICONS[icon] ?? Newspaper;
  return (
    <Glyph
      className={cn("h-3.5 w-3.5 shrink-0", TINT[category ?? ""] ?? "text-muted-foreground", className)}
      aria-hidden
    />
  );
}
