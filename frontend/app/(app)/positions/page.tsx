"use client";

import Link from "next/link";
import { WalletMinimal } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ChangeBadge } from "@/components/market/indicators";
import { useHoldings } from "@/lib/holdings";
import { bareTicker, num } from "@/lib/format";

/**
 * Holdings, read straight from the Moomoo account.
 *
 * Values are shown per position in that position's own currency and are
 * deliberately NOT totalled: the account holds both USD and AUD lines, and
 * summing them without an FX rate would produce a confident, wrong number.
 */
export default function PositionsPage() {
  const { positions, available, reason, isLoading } = useHoldings();

  return (
    <>
      <div className="mb-4">
        <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">Holdings</h1>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Read-only view of your Moomoo positions. This tool never places orders.
        </p>
      </div>

      {isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : !available ? (
        <Card className="border-dashed p-10 text-center">
          <WalletMinimal className="mx-auto h-6 w-6 text-muted-foreground" />
          <p className="mt-3 text-sm font-medium">Holdings unavailable</p>
          <p className="mx-auto mt-1 max-w-md text-xs text-muted-foreground">
            {reason ?? "The Moomoo trade session could not be reached."}
          </p>
        </Card>
      ) : positions.length === 0 ? (
        <Card className="border-dashed p-10 text-center">
          <p className="text-sm text-muted-foreground">No open positions.</p>
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="border-b bg-muted/40 text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                  <th className="px-3 py-2 font-medium">Symbol</th>
                  <th className="px-3 py-2 font-medium">Name</th>
                  <th className="px-3 py-2 text-right font-medium">Qty</th>
                  <th className="px-3 py-2 text-right font-medium">Avg cost</th>
                  <th className="px-3 py-2 text-right font-medium">Last</th>
                  <th className="px-3 py-2 text-right font-medium">Value</th>
                  <th className="px-3 py-2 text-right font-medium">Unrealised</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => (
                  <tr key={p.code} className="border-b transition-colors last:border-0 hover:bg-muted/40">
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-1.5">
                        <Link
                          href={`/ticker/${encodeURIComponent(p.code)}`}
                          className="font-semibold hover:text-primary"
                        >
                          {bareTicker(p.code)}
                        </Link>
                        <Badge variant="outline" className="px-1 py-0 text-[9px]">
                          {p.market}
                        </Badge>
                      </div>
                    </td>
                    <td className="max-w-[220px] truncate px-3 py-2 text-xs text-muted-foreground">
                      {p.name}
                    </td>
                    <td className="px-3 py-2 text-right tabular">{num(p.qty, 4)}</td>
                    <td className="px-3 py-2 text-right tabular">{num(p.avg_cost)}</td>
                    <td className="px-3 py-2 text-right tabular">{num(p.last_price)}</td>
                    <td className="px-3 py-2 text-right tabular">
                      {num(p.market_value)}{" "}
                      <span className="text-[10px] text-muted-foreground">{p.currency}</span>
                    </td>
                    <td className="px-3 py-2 text-right">
                      <div className="flex flex-col items-end gap-0.5">
                        <span
                          className={
                            (p.unrealized_pnl ?? 0) < 0 ? "tabular text-bear" : "tabular text-bull"
                          }
                        >
                          {num(p.unrealized_pnl)}
                        </span>
                        <ChangeBadge value={p.unrealized_pnl_pct} />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {available && positions.length > 0 && (
        <p className="mt-2 text-[11px] text-muted-foreground">
          Values are shown in each position&apos;s own currency and are not totalled.
        </p>
      )}
    </>
  );
}
