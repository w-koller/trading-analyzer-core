"use client";

import { AlertTriangle, XCircle } from "lucide-react";
import { useBackendStatus } from "@/lib/backend-status";
import { useHoldings } from "@/lib/holdings";
import { timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Backend / OpenD / Ollama status.
 *
 * Renders nothing when everything is fine. A permanent green bar teaches
 * people to ignore the space where warnings appear, so the space stays empty
 * until it has something to say.
 *
 * Lives in the app shell rather than on the dashboard: an outage that only
 * announced itself on one page left every other page showing stale numbers
 * with no indication they had stopped updating.
 */
export function HealthBanner() {
  const { health, online, unreachableError, lastOkAt } = useBackendStatus();
  const holdings = useHoldings();

  // Unreachable outranks everything: nothing else on screen can be trusted.
  if (!online) {
    return (
      <Banner tone="bad" icon={<XCircle className="h-4 w-4 shrink-0" />}>
        Backend unreachable
        {lastOkAt ? ` — last responded ${timeAgo(new Date(lastOkAt).toISOString())}` : ""}
        . Retrying…
        {unreachableError && (
          <span className="ml-1 font-normal opacity-70">({unreachableError})</span>
        )}
      </Banner>
    );
  }

  if (!health) return null;

  const problems: string[] = [];
  if (!health.opend?.connected) problems.push("OpenD disconnected");
  else if (health.opend.qot_logined === false) problems.push("OpenD quote session not logged in");
  if (!health.ollama?.reachable) problems.push("Ollama unreachable");
  else if (health.ollama.configured_model_present === false)
    problems.push(`model ${health.ollama_model} missing`);
  if (!health.db_exists) problems.push("database missing");
  // Holdings failing is a degraded annotation, not a broken pipeline.
  if (!holdings.isLoading && !holdings.available && holdings.reason)
    problems.push("holdings unavailable");

  // A paused rotation is deliberately NOT a problem — it is a valid state the
  // user chose, and warning about a chosen state is the permanent-green
  // antipattern in reverse. The scan controls surface it instead.
  if (problems.length === 0) return null;

  return (
    <Banner tone="warn" icon={<AlertTriangle className="h-4 w-4 shrink-0" />}>
      Degraded — {problems.join(" · ")}
    </Banner>
  );
}

function Banner({
  tone,
  icon,
  children,
}: {
  tone: "bad" | "warn";
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "mb-4 flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-medium",
        tone === "bad" && "border-bear/40 bg-bear-muted text-bear",
        tone === "warn" && "border-delayed/40 bg-delayed-muted text-delayed",
      )}
    >
      {icon}
      <span>{children}</span>
    </div>
  );
}
