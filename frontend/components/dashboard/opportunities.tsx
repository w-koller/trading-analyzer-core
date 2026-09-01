"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Clock, Telescope } from "lucide-react";
import { Empty, RowSkeletons, SectionCard } from "@/components/ui/section-card";
import { TickerRow } from "@/components/dashboard/ticker-row";
import { GlossaryTerm } from "@/components/glossary-term";
import { api, type Opportunity } from "@/lib/api";
import { num, timeAgo } from "@/lib/format";

const REFETCH_MS = 120_000;

/**
 * Ranked opportunities, both horizons.
 *
 * PULL, never PUSH — see services/signals.py. This section is something the
 * user chooses to look at. It never raises a notification, never becomes an
 * alert, and never carries a badge, which is what keeps `alerts.py`'s
 * held-positions-only scoping intact instead of quietly making an exception
 * to it.
 *
 * The whole section disappears when neither horizon has a candidate. Same
 * reasoning as PositionAlerts: an always-present empty state becomes
 * furniture, and furniture is invisible exactly when it changes.
 */
export function Opportunities({ market }: { market: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["signals", "opportunities", market],
    queryFn: () => api.opportunities(market || undefined),
    refetchInterval: REFETCH_MS,
  });

  // A failure here is not worth a red card on the market board: this is a
  // ranking of data shown elsewhere on the same page, not a safety surface.
  if (isError) return null;

  if (isLoading) {
    return (
      <div className="grid gap-3 lg:grid-cols-2">
        <SectionCard title="Short term" description="Loading…" icon={<Clock className="h-4 w-4" />}>
          <RowSkeletons n={3} />
        </SectionCard>
        <SectionCard title="Medium term" description="Loading…" icon={<Telescope className="h-4 w-4" />}>
          <RowSkeletons n={3} />
        </SectionCard>
      </div>
    );
  }

  const short = data?.horizons.short ?? [];
  const medium = data?.horizons.medium ?? [];
  if (short.length === 0 && medium.length === 0) return null;

  return (
    <section>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold">Opportunities</h2>
        <Calibration data={data} />
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <SectionCard
          title="Short term"
          description="Hold a day or two — momentum and positioning."
          icon={<Clock className="h-4 w-4" />}
        >
          <Horizon rows={short} />
        </SectionCard>
        <SectionCard
          title="Medium term"
          description="Weeks to months — trend structure."
          icon={<Telescope className="h-4 w-4" />}
        >
          <Horizon rows={medium} />
        </SectionCard>
      </div>

      <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
        A ranking of stored theses, computed in Python — not a forecast, and
        nothing here places or manages an order. Levels come from each thesis&apos;s
        own entry, stop and target; a candidate is dropped when those imply a
        risk/reward below {data?.min_risk_reward ?? 1}. A thesis that named no
        entry falls back to the nearest support or resistance the price would
        pull back to.
      </p>
    </section>
  );
}

/**
 * States the calibration plainly rather than letting the ranking imply a
 * track record it does not have. Until the scorecard has both enough samples
 * and enough distinct trading days, it says so.
 *
 * It is a link because this sentence is exactly where the reader asks
 * "unfitted against WHAT?" — and the scorecard is the answer. Leaving it as
 * dead text meant the one screen that could substantiate or contradict this
 * ranking had no route into it from the page making the claim.
 */
function Calibration({ data }: { data: { calibrated: boolean; scored_samples: number } | undefined }) {
  if (!data) return null;
  return (
    <Link
      href="/setups?view=scorecard"
      className="text-[11px] text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-foreground"
    >
      {data.calibrated ? (
        <>Weights checked against {data.scored_samples} scored theses</>
      ) : (
        <>
          Uncalibrated — weights are priors, not fitted to outcomes
          {data.scored_samples > 0 ? ` (${data.scored_samples} scored so far)` : ""}
        </>
      )}
    </Link>
  );
}

function Horizon({ rows }: { rows: Opportunity[] }) {
  if (rows.length === 0) return <Empty>Nothing clears the bar right now.</Empty>;
  return (
    <div className="space-y-0.5">
      {rows.map((o, i) => (
        <TickerRow
          key={o.code}
          code={o.code}
          name={o.name}
          rank={i + 1}
          price={o.spot}
          note={<Note o={o} />}
        />
      ))}
    </div>
  );
}

function Note({ o }: { o: Opportunity }) {
  return (
    <span className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
      <span className={o.direction === "Bullish" ? "text-bull" : "text-bear"}>
        {o.direction}
      </span>
      {/* Entry / stop / target together, because a level on its own is not
          actionable and inviting someone to act on one would be worse than
          showing none. */}
      <span className="tabular">
        in {num(o.entry)} · stop {num(o.stop)} · tgt {num(o.target)}
      </span>
      {o.risk_reward !== null && (
        <GlossaryTerm term="risk_reward" className="no-underline">
          <span className="tabular">
            {o.risk_reward.toFixed(1)}:1
            {/* The two legs, so a large ratio cannot read as unambiguously
                good when it really means a very tight stop. */}
            {o.stop_distance_pct !== null && (
              <span className="text-muted-foreground">
                {" "}
                (−{o.stop_distance_pct}% / +{o.target_distance_pct}%)
              </span>
            )}
          </span>
        </GlossaryTerm>
      )}
      <span>{o.agreeing}/{o.of_last} agree</span>
      {o.thesis_created_at && <span>· {timeAgo(o.thesis_created_at)}</span>}
      {o.notes
        .filter((n) => n.startsWith("tight stop"))
        .map((n) => (
          <span key={n} className="text-delayed">
            {n}
          </span>
        ))}
    </span>
  );
}
