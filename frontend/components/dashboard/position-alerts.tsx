"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { AlertTriangle, BellOff, Info, TriangleAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { api, type AlertSeverity, type PositionAlert } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * What is going wrong in the things you actually own.
 *
 * Renders ABSOLUTELY NOTHING when there is nothing. No "0 alerts" card, no
 * green all-clear. An always-present empty state becomes furniture, and
 * furniture is invisible exactly when it changes — which is the moment this
 * component exists for. The one exception is `available: false`, because
 * "we cannot see your holdings" is a real gap, not a clear.
 *
 * A card in the page flow rather than the sticky HealthBanner: none of these
 * are outages, and putting them in the banner would follow the user onto
 * every page. Severity is a left border plus a badge; the card body stays
 * neutral, so six of them do not turn into a colour chart.
 *
 * Every rule behind these is a plain Python comparison — no model wrote this
 * text. See `services/alerts.py` for why.
 */

const STYLES: Record<AlertSeverity, { bar: string; badge: string; Icon: typeof Info }> = {
  critical: { bar: "border-l-bear", badge: "bear", Icon: TriangleAlert },
  warn: { bar: "border-l-delayed", badge: "delayed", Icon: AlertTriangle },
  info: { bar: "border-l-flat", badge: "flat", Icon: Info },
};

export function PositionAlerts() {
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: ["alerts"],
    queryFn: () => api.alerts(),
    refetchInterval: 60_000,
  });

  const ack = useMutation({
    mutationFn: (fingerprint: string) => api.ackAlert(fingerprint),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["alerts"] }),
  });

  if (!data) return null;

  if (!data.available) {
    return (
      <p className="px-1 text-xs text-muted-foreground">
        {data.reason ?? "Holdings are unavailable, so nothing can be checked."}
      </p>
    );
  }

  if (data.alerts.length === 0) return null;

  return (
    <Card>
      <CardContent className="space-y-1.5 p-3">
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-xs font-semibold">Needs your attention</h2>
          <div className="flex items-center gap-1">
            {(["critical", "warn", "info"] as const).map((s) =>
              data.counts[s] > 0 ? (
                <Badge key={s} variant={STYLES[s].badge as "bear"}>
                  {data.counts[s]} {s}
                </Badge>
              ) : null,
            )}
          </div>
        </div>

        {data.alerts.map((a) => (
          <AlertRow
            key={a.id}
            alert={a}
            onAck={() => ack.mutate(a.id)}
            acking={ack.isPending && ack.variables === a.id}
          />
        ))}

        {data.truncated > 0 && (
          <Link
            href="/positions"
            className="block px-1 pt-1 text-[11px] text-muted-foreground hover:text-primary"
          >
            +{data.truncated} more across your holdings →
          </Link>
        )}
        {data.acknowledged_count > 0 && (
          <p className="px-1 text-[11px] text-muted-foreground">
            {data.acknowledged_count} silenced — they return when the snooze
            expires, or sooner if the situation changes.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function AlertRow({
  alert,
  onAck,
  acking,
}: {
  alert: PositionAlert;
  onAck: () => void;
  acking: boolean;
}) {
  const { bar, Icon } = STYLES[alert.severity];
  return (
    <div className={cn("flex items-start gap-2 border-l-2 py-1.5 pl-2.5", bar)}>
      <Icon
        className={cn(
          "mt-0.5 h-3.5 w-3.5 shrink-0",
          alert.severity === "critical"
            ? "text-bear"
            : alert.severity === "warn"
              ? "text-delayed"
              : "text-flat",
        )}
      />
      <div className="min-w-0 flex-1">
        <p className="text-xs">
          <Link href={alert.href} className="font-semibold hover:text-primary">
            {alert.name}
          </Link>{" "}
          — {alert.title}
        </p>
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          {alert.detail}
        </p>
      </div>
      <button
        type="button"
        onClick={onAck}
        disabled={acking}
        /* The tooltip says "silenced until", not "dismiss". A dismiss that
           reads as a delete is how a safety feature becomes decorative. */
        title={
          alert.severity === "critical"
            ? "Silence for 12 hours — it returns if this is still true"
            : "Silence for 3 days — it returns if this is still true"
        }
        className="shrink-0 rounded p-1 text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
      >
        <BellOff className="h-3 w-3" />
      </button>
    </div>
  );
}
