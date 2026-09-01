import * as React from "react";
import { MarketTabs } from "@/components/layout/market-tabs";

export function PageHeader({
  title,
  description,
  showMarkets = true,
  actions,
}: {
  title: string;
  description?: string;
  showMarkets?: boolean;
  actions?: React.ReactNode;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">{title}</h1>
        {description && <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>}
      </div>
      <div className="flex items-center gap-2">
        {actions}
        {showMarkets && <MarketTabs />}
      </div>
    </div>
  );
}
