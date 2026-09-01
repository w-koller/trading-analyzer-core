"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Gauge, RefreshCw } from "lucide-react";
import { Empty, SectionCard } from "@/components/ui/section-card";
import { QueryErrorCard } from "@/components/query-error-card";
import { GlossaryTerm } from "@/components/glossary-term";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, api, type ScorecardBucket, type ScorecardResponse } from "@/lib/api";
import { DASH, pct } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * How past theses actually resolved, measured against the bars that followed
 * them.
 *
 * This is the one screen that can contradict the rest of the app, so the
 * whole component is built around not overstating what it knows. Three rules
 * carry that, and none of them is cosmetic:
 *
 *   1. A hit rate is NEVER rendered without its sample count AND its distinct
 *      day count beside it. Tickers scanned on the same day all share that
 *      day's market move, so a bucket of 96 samples drawn from 2 days is
 *      nearer 2 observations than 96 — which is exactly how the first scoring
 *      run managed to report every bucket positive, Bearish included.
 *   2. Buckets the backend marks `sufficient: false` are shown, but drained
 *      of emphasis: no directional colour, muted text, and the shortfall
 *      stated inline. Hiding them would make a building corpus look empty;
 *      styling them like findings would make noise look like a result.
 *   3. The thresholds come from the response (`min_samples`,
 *      `min_distinct_days`), never from constants here. They are the
 *      backend's rule and the UI has no business re-deriving it.
 */
export function ThesisScorecard() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["scorecard"],
    queryFn: () => api.scorecard(),
  });

  if (isLoading) {
    return (
      <div className="grid gap-3 lg:grid-cols-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-64" />
        ))}
      </div>
    );
  }

  // "Could not ask" is a different answer from "nothing has been scored yet",
  // and this page is the one place where confusing them would read as the
  // model having no track record at all.
  if (isError || !data) return <QueryErrorCard error={error} what="the scorecard" />;

  return (
    <div className="space-y-3">
      <Summary data={data} />
      <div className="grid gap-3 lg:grid-cols-2">
        {data.horizons.map((h) => (
          <HorizonCard key={h} horizon={h} data={data} />
        ))}
      </div>
      <Footnote />
    </div>
  );
}

/** The corpus-wide state, plus the manual re-score. */
function Summary({ data }: { data: ScorecardResponse }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState<{ text: string; failed: boolean } | null>(null);

  const score = useMutation({
    mutationFn: () => api.runScoring(),
    onMutate: () => setNote(null),
    onSuccess: (result) => {
      setNote({
        text:
          result.scored > 0
            ? `Scored ${result.scored} thesis${result.scored === 1 ? "" : "es"}.`
            : "Nothing new to score — every thesis whose horizon has passed is already measured.",
        failed: false,
      });
      queryClient.invalidateQueries({ queryKey: ["scorecard"] });
    },
    onError: (e) => {
      // A 409 means a scan holds the gateway. That is an ordinary "not now" —
      // the same rows stay unscored and the nightly job picks them up anyway —
      // so it reads as a wait, not as the failure an error colour would imply.
      // Verified live: the backend answers 409 "a scan is using the gateway".
      const busyScan = e instanceof ApiError && e.status === 409;
      setNote({
        text: busyScan
          ? "A scan is using the gateway right now. Scoring can wait for it — try again once the scan finishes."
          : `Scoring failed: ${e instanceof Error ? e.message : String(e)}`,
        failed: !busyScan,
      });
    },
  });

  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <p className="text-sm font-semibold">
            {data.calibrated
              ? "Measured against realised bars"
              : "Not yet calibrated"}
          </p>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-muted-foreground">
            {data.calibrated ? (
              <>
                At least one bucket has cleared {data.min_samples} samples across{" "}
                {data.min_distinct_days} separate trading days. Read every figure
                with its own counts — the buckets below it have not.
              </>
            ) : (
              <>
                A bucket counts as calibrated at {data.min_samples} samples across{" "}
                {data.min_distinct_days} separate trading days, and none has
                reached that. The corpus currently spans{" "}
                <strong className="font-semibold text-foreground">
                  {data.distinct_days}{" "}
                  <GlossaryTerm term="distinct_days">
                    {data.distinct_days === 1 ? "trading day" : "trading days"}
                  </GlossaryTerm>
                </strong>
                . Everything below is the record so far, not a track record.
              </>
            )}
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => score.mutate()}
          disabled={score.isPending}
          className="shrink-0"
        >
          <RefreshCw className={cn("h-3.5 w-3.5", score.isPending && "animate-spin")} />
          {score.isPending ? "Scoring…" : "Score now"}
        </Button>
      </div>
      {note && (
        <p className={cn("mt-2 text-xs", note.failed ? "text-bear" : "text-muted-foreground")}>
          {note.text}
        </p>
      )}
    </div>
  );
}

/** Direction first, then conviction, so a card reads top-down consistently. */
const DIRECTION_ORDER = ["Bullish", "Bearish", "Neutral"];
const BUCKET_ORDER = ["1-4", "5-6", "7-10"];

function sortKey(b: ScorecardBucket): number {
  const d = DIRECTION_ORDER.indexOf(b.direction);
  const c = BUCKET_ORDER.indexOf(b.conviction_bucket);
  return (d < 0 ? DIRECTION_ORDER.length : d) * 10 + (c < 0 ? BUCKET_ORDER.length : c);
}

function HorizonCard({ horizon, data }: { horizon: number; data: ScorecardResponse }) {
  const rows = useMemo(
    () =>
      data.buckets
        .filter((b) => b.horizon_days === horizon)
        .sort((a, b) => sortKey(a) - sortKey(b)),
    [data.buckets, horizon],
  );

  return (
    <SectionCard
      // Load-bearing, not tidying. A grid item is `min-width: auto`, so
      // anything inside declaring a min-width hands it straight to the
      // column: the card stops being able to shrink, the grid outgrows the
      // viewport, and the page scrolls sideways. Which then breaks something
      // that looks unrelated — the mobile browser widens the layout viewport
      // to fit the overflow, and the bottom tab bar is `fixed` against
      // exactly that, so it leaves the screen.
      className="min-w-0"
      title={horizon === 1 ? "1 trading day" : `${horizon} trading days`}
      description={`Where price sat ${horizon} bar${horizon === 1 ? "" : "s"} after each thesis.`}
      icon={<Gauge className="h-4 w-4" />}
      meta={
        rows.length > 0 ? (
          <span className="tabular text-[11px] text-muted-foreground">
            {rows.reduce((n, r) => n + r.samples, 0)} samples
          </span>
        ) : null
      }
    >
      {rows.length === 0 ? (
        // NOT the same as "no data". A horizon produces nothing until that
        // many bars exist after a thesis, so on a young corpus the long
        // horizons are legitimately unknowable and saying "no results" would
        // read as a bug or as a model that never gets scored.
        <Empty>
          No thesis is old enough yet — this horizon needs {horizon} trading days
          of bars after a thesis was written.
        </Empty>
      ) : (
        <Buckets rows={rows} data={data} />
      )}
    </SectionCard>
  );
}

/**
 * Everything the presentation rules decide about one bucket, worked out once.
 *
 * There are two layouts below — stacked rows on a phone, a table from `sm`
 * up — and the rules in this file's header are judgement, not formatting.
 * Deriving them per layout is how the phone quietly ends up colouring a
 * bucket the table greys out, so both layouts render from this and neither
 * gets to re-decide.
 */
function derive(row: ScorecardBucket, data: ScorecardResponse) {
  const solid = row.sufficient;
  const resolved = row.target_first + row.stop_first + row.unresolved;
  return {
    solid,
    // Rule 2: an unproven bucket keeps its numbers and loses its emphasis.
    // Directional colour is the strongest "this is a finding" signal on the
    // page, so it is the first thing withheld.
    directionClass: !solid
      ? "text-muted-foreground"
      : row.direction === "Bullish"
        ? "text-bull"
        : row.direction === "Bearish"
          ? "text-bear"
          : "text-foreground",
    // Rule 1: never rendered apart from the hit rate it qualifies.
    sample: `n=${row.samples}`,
    days: `${row.distinct_days}d`,
    shortfall: solid ? null : `below ${data.min_samples}/${data.min_distinct_days}d`,
    // Neutral carries a null hit rate by construction — it makes no
    // directional claim, and 0% here would invent a failed one.
    hit: row.hit_rate === null ? null : `${(row.hit_rate * 100).toFixed(0)}%`,
    ret: pct(row.mean_return_pct, 1),
    resolution: resolved === 0 ? null : `${row.target_first}/${row.stop_first}/${row.unresolved}`,
  };
}

const NEUTRAL_HIT_TITLE = "Neutral theses make no directional call";
const NO_LEVELS_TITLE = "No thesis in this bucket suggested both a stop and a target";

function Buckets({ rows, data }: { rows: ScorecardBucket[]; data: ScorecardResponse }) {
  return (
    <>
      {/* Phone: stacked rows, no horizontal scrolling anywhere.
          A five-column table needs ~480px and a phone is 412, so this used to
          be one `min-w-[30rem]` table inside an `overflow-x-auto` box. That
          box scrolls, but it does not stop its own MIN-CONTENT width
          propagating: the card is a grid item, grid items are `min-width:
          auto`, so the column itself grew to 480px and dragged the whole page
          past the viewport. Two things broke, and only one of them looked
          related — the mobile browser widens the layout viewport to fit
          overflowing content, and the bottom tab bar is `fixed inset-x-0
          bottom-0` against exactly that, so it stopped sitting on the screen
          and sank to the end of the document.
          Hence: don't make a phone render a table it cannot fit. */}
      <div className="space-y-1.5 sm:hidden">
        {rows.map((r) => (
          <StackedRow key={`${r.direction}-${r.conviction_bucket}`} row={r} data={data} />
        ))}
      </div>

      {/* sm and up: the table, in its own scroll container so a narrow tablet
          still scrolls the TABLE rather than the page. `min-w-0` on the card
          in HorizonCard is what keeps that promise. */}
      <div className="hidden overflow-x-auto sm:block">
        <table className="w-full min-w-[26rem] text-xs">
          <thead>
            <tr className="border-b border-border text-left text-[11px] text-muted-foreground">
              <th className="px-2 py-1.5 font-medium">Call</th>
              <th className="px-2 py-1.5 font-medium">Sample</th>
              <th className="px-2 py-1.5 text-right font-medium">
                <GlossaryTerm term="hit_rate">Hit rate</GlossaryTerm>
              </th>
              <th className="px-2 py-1.5 text-right font-medium">
                <GlossaryTerm term="forward_return">Return</GlossaryTerm>
              </th>
              <th className="px-2 py-1.5 text-right font-medium">
                <GlossaryTerm term="resolution">T/S/&mdash;</GlossaryTerm>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <TableRow key={`${r.direction}-${r.conviction_bucket}`} row={r} data={data} />
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

/** The phone layout: call and figures on one line, counts on the next. */
function StackedRow({ row, data }: { row: ScorecardBucket; data: ScorecardResponse }) {
  const v = derive(row, data);

  return (
    <div className="rounded-md bg-muted/40 px-2 py-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="min-w-0 truncate">
          <span className={cn("text-xs font-semibold", v.directionClass)}>{row.direction}</span>
          <span className="ml-1.5 text-xs text-muted-foreground">{row.conviction_bucket}</span>
        </span>
        <span
          className={cn(
            "tabular flex shrink-0 items-baseline gap-2.5 text-xs",
            !v.solid && "text-muted-foreground",
          )}
        >
          <span>
            <span className="text-[10px] text-muted-foreground">hit </span>
            {v.hit ?? <span className="text-muted-foreground" title={NEUTRAL_HIT_TITLE}>{DASH}</span>}
          </span>
          <span>{v.ret}</span>
        </span>
      </div>
      {/* Rule 1 again: the counts are on the row's own second line, never
          somewhere the reader has to go looking for them. */}
      <div className="mt-0.5 text-[10px] leading-tight text-muted-foreground">
        <span className="tabular">{v.sample}</span>
        {" · "}
        <GlossaryTerm term="distinct_days">
          <span className="tabular">{v.days}</span>
        </GlossaryTerm>
        {v.shortfall && <> · {v.shortfall}</>}
        {v.resolution && (
          <>
            {" · "}
            <GlossaryTerm term="resolution">
              <span className="tabular">{v.resolution}</span>
            </GlossaryTerm>
          </>
        )}
      </div>
    </div>
  );
}

function TableRow({ row, data }: { row: ScorecardBucket; data: ScorecardResponse }) {
  const v = derive(row, data);

  return (
    <tr className="border-b border-border/50 align-top last:border-0">
      <td className="px-2 py-2">
        <span className={cn("font-semibold", v.directionClass)}>{row.direction}</span>
        <span className="ml-1.5 text-muted-foreground">{row.conviction_bucket}</span>
      </td>

      <td className="px-2 py-2">
        <span className="tabular">{v.sample}</span>
        <span className="text-muted-foreground">
          {" · "}
          <GlossaryTerm term="distinct_days">
            <span className="tabular">{v.days}</span>
          </GlossaryTerm>
        </span>
        {v.shortfall && (
          <span className="block text-[10px] leading-tight text-muted-foreground">
            {v.shortfall}
          </span>
        )}
      </td>

      <td className={cn("tabular px-2 py-2 text-right", !v.solid && "text-muted-foreground")}>
        {v.hit ?? <span className="text-muted-foreground" title={NEUTRAL_HIT_TITLE}>{DASH}</span>}
      </td>

      <td className={cn("tabular px-2 py-2 text-right", !v.solid && "text-muted-foreground")}>
        {v.ret}
      </td>

      <td className="tabular px-2 py-2 text-right text-muted-foreground">
        {v.resolution ?? <span title={NO_LEVELS_TITLE}>{DASH}</span>}
      </td>
    </tr>
  );
}

function Footnote() {
  return (
    <p className="text-[11px] leading-relaxed text-muted-foreground">
      Scored against daily bars, not against trades — a thesis is measured on
      what price did afterwards whether or not anyone acted on it. One sample
      per ticker per trading day, taking that day&apos;s last thesis. A bar that
      touched both the stop and the target counts as the stop, because daily
      bars cannot order two intraday touches. This view covers every market and
      ignores the market filter above.
    </p>
  );
}
