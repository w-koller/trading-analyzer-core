"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Radar } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { api, MARKETS, type CycleResult, type ScanRun } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Runs a scan across the whole enabled watchlist.
 *
 * The request blocks for the entire run — deliberately, on the backend's
 * side, so failures surface instead of vanishing into a background job. At
 * 60-120s of local inference per ticker a full US watchlist is well over an
 * hour, so the UI's job is to say so plainly before starting and then show
 * that something is still happening.
 *
 * Progress is derived from setups appearing in the database, NOT from
 * `scanner_runs`: those counters are written once, by finish_scanner_run, so
 * they read 0 for the entire run and only become correct after it is over.
 * Each ticker's setup is inserted as it completes, so counting setups that
 * carry the run's id is the only signal that actually moves.
 */
export function ScanRunnerDialog({ compact = false }: { compact?: boolean }) {
  const [open, setOpen] = React.useState(false);
  const [market, setMarket] = React.useState<string>("US");
  const [runId, setRunId] = React.useState<number | null>(null);
  const [result, setResult] = React.useState<CycleResult | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const queryClient = useQueryClient();

  const { data: watchlist } = useQuery({
    queryKey: ["watchlist", market],
    queryFn: () => api.watchlist(market || undefined),
    enabled: open,
  });

  const target = (watchlist?.tickers ?? []).filter((t) => t.enabled).length;

  const scan = useMutation({
    mutationFn: () => api.runFullScan(market || undefined),
    onMutate: () => {
      setResult(null);
      setError(null);
      setRunId(null);
      // The run row is written before the first ticker is touched, so the
      // newest run id is available almost immediately after kickoff.
      window.setTimeout(async () => {
        try {
          const runs = await api.scanRuns(1);
          const latest: ScanRun | undefined = runs.runs?.[0];
          if (latest?.status === "running") setRunId(latest.id);
        } catch {
          /* progress is best-effort; the blocking call is the real signal */
        }
      }, 1500);
    },
    onSuccess: (data) => {
      setResult(data);
      queryClient.invalidateQueries({ queryKey: ["setups"] });
      queryClient.invalidateQueries({ queryKey: ["movers"] });
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  });

  const { data: progress } = useQuery({
    queryKey: ["scan-progress", runId],
    // Must be "recent": progress is inferred by counting setups carrying
    // this run's id (decisions #22), and freshly-written rows are the whole
    // signal. Conviction-ordered, they would drop out of the top 200 as soon
    // as the corpus grew past it and the counter would sit at 0 for the hour
    // an unattended pre-market scan takes.
    queryFn: () => api.setups({ limit: 200, market: market || undefined, sort: "recent" }),
    enabled: scan.isPending && runId !== null,
    refetchInterval: 5_000,
  });

  const done = runId
    ? (progress?.setups ?? []).filter((s) => s.scanner_run_id === runId).length
    : 0;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Closing mid-run would abandon a request the user cannot restart
        // cheaply — an hour of inference is not a reopenable tab.
        if (!next && scan.isPending) return;
        setOpen(next);
      }}
    >
      <DialogTrigger asChild>
        {compact ? (
          <Button variant="ghost" size="icon" aria-label="Run a scan">
            <Radar className="h-4 w-4" />
          </Button>
        ) : (
          <Button className="w-full" size="sm">
            <Radar className="h-4 w-4" />
            Scan watchlist
          </Button>
        )}
      </DialogTrigger>

      <DialogContent hideClose={scan.isPending}>
        <DialogHeader>
          <DialogTitle>Scan the full watchlist</DialogTitle>
          <DialogDescription>
            Every enabled ticker gets a fresh thesis from the local model.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div>
            <p className="mb-1.5 text-xs font-medium text-muted-foreground">Market</p>
            <div className="flex flex-wrap gap-1.5">
              {[{ value: "", label: "All markets" }, ...MARKETS.map((m) => ({ value: m, label: m }))].map(
                (opt) => (
                  <button
                    key={opt.value || "all"}
                    disabled={scan.isPending}
                    onClick={() => setMarket(opt.value)}
                    className={cn(
                      "rounded-md border px-3 py-1.5 text-xs font-semibold transition-colors disabled:opacity-50",
                      market === opt.value
                        ? "border-primary bg-primary text-primary-foreground"
                        : "border-border text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {opt.label}
                  </button>
                ),
              )}
            </div>
          </div>

          <div className="rounded-md border border-delayed/40 bg-delayed-muted p-3">
            <p className="text-xs font-semibold text-delayed">This takes a long time</p>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              One thesis costs 60-120 seconds of local inference.{" "}
              {target > 0 ? (
                <>
                  <strong className="text-foreground">{target} enabled ticker{target === 1 ? "" : "s"}</strong>{" "}
                  means roughly{" "}
                  <strong className="text-foreground">
                    {Math.round((target * 90) / 60)} minutes
                  </strong>
                  .
                </>
              ) : (
                "The whole watchlist can run well over an hour."
              )}{" "}
              Keep this dialog open — closing it abandons the run.
            </p>
          </div>

          {scan.isPending && (
            <div className="rounded-md border bg-muted/40 p-3">
              <div className="flex items-center gap-2 text-xs font-medium">
                <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
                Scanning…
                {runId !== null && (
                  <span className="tabular text-muted-foreground">
                    {done} / {target || "?"} complete
                  </span>
                )}
              </div>
              {target > 0 && runId !== null && (
                <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-border">
                  <div
                    className="h-full rounded-full bg-primary transition-all duration-500"
                    style={{ width: `${Math.min(100, (done / target) * 100)}%` }}
                  />
                </div>
              )}
              <p className="mt-2 text-[11px] text-muted-foreground">
                Progress is best-effort — it counts theses as they are stored.
              </p>
            </div>
          )}

          {result && (
            <div className="rounded-md border bg-muted/40 p-3 text-xs">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge variant="bull">{result.succeeded} succeeded</Badge>
                {result.failed > 0 && <Badge variant="bear">{result.failed} failed</Badge>}
                <Badge variant="outline">{Math.round(result.elapsed_seconds)}s</Badge>
              </div>
              {result.failed > 0 && (
                <ul className="mt-2 space-y-0.5 text-muted-foreground">
                  {result.results
                    .filter((r) => !r.ok)
                    .slice(0, 5)
                    .map((r) => (
                      <li key={r.code}>
                        <span className="font-medium text-foreground">{r.code}</span>: {r.error}
                      </li>
                    ))}
                </ul>
              )}
            </div>
          )}

          {error && (
            <p className="rounded-md border border-bear/40 bg-bear-muted p-3 text-xs text-bear">
              {error}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            size="sm"
            disabled={scan.isPending}
            onClick={() => setOpen(false)}
          >
            {result ? "Close" : "Cancel"}
          </Button>
          <Button size="sm" disabled={scan.isPending || target === 0} onClick={() => scan.mutate()}>
            {scan.isPending ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                Running…
              </>
            ) : (
              `Scan ${target || ""} ticker${target === 1 ? "" : "s"}`
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
