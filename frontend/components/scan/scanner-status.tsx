"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Pause, Play } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { api } from "@/lib/api";
import { timeUntil } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Whether the scanner is actually running, and a way to change it.
 *
 * This exists because scanning can sit paused indefinitely while the
 * dashboard shows theses that look current. Nothing said so, and the only way
 * to resume was curl — so "no fresh analysis for five hours" was invisible
 * from the product.
 *
 * Scans now fire on trading-session boundaries rather than a 60-second
 * rotation, so the countdown shown is the next SESSION scan — the next
 * full-watchlist pass. `next_run` is deliberately not used for it: that is
 * the gap-filler's next tick, a repair path that normally scans nothing, and
 * showing it would promise a refresh that does not happen.
 *
 * Deliberately not a HealthBanner condition: paused is a legitimate state the
 * user chose, and warning about a chosen state trains people to ignore
 * warnings. It belongs next to the scan controls, stated plainly.
 *
 * Colours are `sidebar-*` throughout, because that is where this renders and
 * the sidebar is dark purple in BOTH themes — the page-level tokens are tuned
 * for a card that flips with the theme and wash out against it.
 */
export function ScannerStatus({ className }: { className?: string }) {
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["scan-status"],
    queryFn: () => api.scanStatus(),
    refetchInterval: 30_000,
  });

  const toggle = useMutation({
    mutationFn: (next: "resume" | "pause") =>
      next === "resume" ? api.resumeScan() : api.pauseScan(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["scan-status"] }),
  });

  if (isLoading || !data) return null;

  const scanning = Boolean(data.scan_in_progress);
  const paused = Boolean(data.paused);
  const neverStarted = paused && data.rotation_state_source !== "persisted";
  const nextScan = data.next_session_scan ?? data.next_run;
  const sessionCount = (data.session_scans ?? data.premarket ?? []).length;

  return (
    <div className={cn("rounded-md border bg-muted/30 p-2.5", className)}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          {scanning ? (
            <Badge variant="primary" className="gap-1">
              <Loader2 className="h-3 w-3 animate-spin" />
              Scanning
            </Badge>
          ) : paused ? (
            <Badge variant="delayed">Paused</Badge>
          ) : (
            <Badge variant="bull" className="gap-1">
              <span className="h-1.5 w-1.5 rounded-full bg-bull animate-pulse-soft" />
              Auto-scanning
            </Badge>
          )}
        </div>

        <button
          type="button"
          disabled={toggle.isPending || scanning}
          onClick={() => toggle.mutate(paused ? "resume" : "pause")}
          className={cn(
            "inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors disabled:opacity-50",
            paused
              ? "border-sidebar-input-border bg-sidebar-input text-sidebar-foreground hover:bg-sidebar-accent"
              : "border-sidebar-input-border text-sidebar-muted hover:bg-sidebar-input hover:text-sidebar-foreground",
          )}
          title={scanning ? "A scan is running; wait for it to finish" : undefined}
        >
          {toggle.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : paused ? (
            <Play className="h-3 w-3" />
          ) : (
            <Pause className="h-3 w-3" />
          )}
          {paused ? "Resume" : "Pause"}
        </button>
      </div>

      <p className="mt-1.5 text-[11px] leading-snug text-sidebar-muted">
        {scanning
          ? "A scan is in progress. Scheduled sessions wait their turn."
          : paused
            ? neverStarted
              ? "Automatic scanning has never been turned on — theses only update when you run a scan manually."
              : "You paused automatic scanning. Theses will not refresh until you resume."
            : nextScan
              ? `Next full scan ${timeUntil(nextScan)}${
                  sessionCount ? ` · ${sessionCount} session scans a day` : ""
                }.`
              : "Scanning before each trading session."}
      </p>

      {toggle.isError && (
        <p className="mt-1.5 text-[11px] text-sidebar-danger">
          Could not change the scanner state — the backend may be unreachable.
        </p>
      )}
    </div>
  );
}
