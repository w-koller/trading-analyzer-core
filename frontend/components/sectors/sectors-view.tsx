"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PillGroup } from "@/components/ui/pill-group";
import { RotationBoard } from "@/components/sectors/rotation-board";
import { RotationPairs } from "@/components/sectors/rotation-pairs";
import { SectorDetail } from "@/components/sectors/sector-detail";
import { api, SECTOR_WINDOWS, type PlateClass, type SectorWindow } from "@/lib/api";

const CLASSES: { value: PlateClass | "ALL"; label: string }[] = [
  { value: "ALL", label: "All" },
  { value: "INDUSTRY", label: "Industries" },
  { value: "CONCEPT", label: "Themes" },
];

/**
 * The full sector rotation view.
 *
 * Two flat axes rather than a hierarchy: INDUSTRY is Moomoo's own
 * sub-industry taxonomy ("Semiconductors", "Software - Infrastructure") and
 * CONCEPT is the cross-cutting theme list ("AI Chip", "AI application
 * software"). The second is what actually answers "is money leaving AI
 * hardware for AI software", and it does not nest inside the first, so
 * inventing a tree over them would be inventing a relationship the source
 * does not publish.
 *
 * The window and the selected sector live in component state rather than the
 * URL: unlike `?market=` and `?view=`, they are a reading position inside one
 * page rather than a destination worth linking to, and putting four more
 * params in the URL would make the back button behave surprisingly.
 */
export function SectorsView({ market }: { market: string }) {
  const [window, setWindow] = useState<SectorWindow>(5);
  const [plateClass, setPlateClass] = useState<PlateClass | "ALL">("ALL");
  const [selected, setSelected] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const universe = useQuery({
    queryKey: ["sectors", "universe", market],
    queryFn: () => api.sectorUniverse(market || undefined),
  });

  const refresh = useMutation({
    mutationFn: () => api.refreshSectors(market || undefined),
    onSuccess: () => {
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["sectors"] });
    },
    onError: (e) => {
      // A 409 is a "not now", not a failure: the kline call backfills, so the
      // next scheduled run writes exactly what this one would have.
      const msg = e instanceof Error ? e.message : String(e);
      setError(
        msg.includes("409") || msg.toLowerCase().includes("scan is using")
          ? "A scan is using the market gateway. The nightly refresh will pick this up — nothing is lost by waiting."
          : `Refresh failed: ${msg}`,
      );
    },
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <PillGroup
          options={SECTOR_WINDOWS}
          value={window}
          onChange={setWindow}
          ariaLabel="Rotation window"
        />
        <PillGroup
          options={CLASSES}
          value={plateClass}
          onChange={setPlateClass}
          ariaLabel="Sector type"
        />
        <Button
          variant="outline"
          size="sm"
          className="ml-auto"
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
        >
          <RefreshCw className={refresh.isPending ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} />
          {refresh.isPending ? "Refreshing…" : "Refresh"}
        </Button>
      </div>

      {refresh.isPending && (
        <p className="rounded-md border bg-muted/50 px-3 py-2 text-xs text-muted-foreground">
          Fetching bars for every sector and rescoring. This takes about five
          minutes — the calls are paced to stay inside the data provider&apos;s rate
          limits.
        </p>
      )}
      {error && (
        <p className="rounded-md border border-delayed/40 bg-delayed-muted px-3 py-2 text-xs text-delayed">
          {error}
        </p>
      )}

      {universe.data?.available && universe.data.members_unvisited > 0 && (
        <p className="rounded-md border bg-muted/50 px-3 py-2 text-[11px] text-muted-foreground">
          Constituent lists are still loading:{" "}
          <span className="tabular">
            {universe.data.counts.total - universe.data.members_unvisited} of{" "}
            {universe.data.counts.total}
          </span>{" "}
          sectors counted. Sectors without a count are shown but marked unconfirmed,
          and they cannot be paired until both sides are known.
        </p>
      )}

      <div className="grid gap-3 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div className="min-w-0 space-y-3">
          <RotationBoard
            market={market}
            window={window}
            plateClass={plateClass === "ALL" ? null : plateClass}
            selected={selected}
            onSelect={(code) => setSelected((cur) => (cur === code ? null : code))}
          />
        </div>
        <div className="min-w-0 space-y-3">
          {selected && (
            <SectorDetail
              plateCode={selected}
              window={window}
              onClose={() => setSelected(null)}
            />
          )}
          <RotationPairs market={market} window={window} onSelect={setSelected} />
        </div>
      </div>
    </div>
  );
}
