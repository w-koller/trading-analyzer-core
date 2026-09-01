"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowDownRight, ArrowUpRight, Eye } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Empty, RowSkeletons, SectionCard } from "@/components/ui/section-card";
import { TickerRow } from "@/components/dashboard/ticker-row";
import { DelayedPill, extendedHoursNote } from "@/components/market/indicators";
import { StaleLabel, StaleWhenOffline } from "@/components/data-freshness";
import { api, type Mover, type Setup } from "@/lib/api";
import { parseIso, pct } from "@/lib/format";

const TOP_N = 5;
const MOVERS_REFETCH_MS = 60_000;
// Flag staleness at 2x the refresh interval: one missed refetch is a blip,
// two means it is not coming back on its own.
const MOVERS_STALE_MS = MOVERS_REFETCH_MS * 2;
/**
 * A thesis has to be at least mildly interesting to displace a big mover.
 * Most theses land on 5/10 Neutral, which is the model saying "nothing here"
 * — promoting those would fill the list with shrugs.
 */
const WATCH_MIN_CONVICTION = 6;
/** Rows fetched for "Watch today" before the 36h + conviction filter. Part
 *  of the query's cache identity — see the note on `queryKey` below. */
const WATCH_SETUPS_LIMIT = 40;

/**
 * The three headline lists.
 *
 * All of them read from one movers query plus one setups query, so switching
 * market or refreshing costs two requests total rather than six.
 */
export function MoversSections({ market }: { market: string }) {
  const moversQuery = useQuery({
    queryKey: ["movers", market],
    queryFn: () => api.movers(market || undefined),
    refetchInterval: MOVERS_REFETCH_MS,
  });

  const setupsQuery = useQuery({
    // Keyed "recent" because that is what it fetches. buildWatchlist filters
    // to the last 36 hours *after* this returns, then ranks by conviction
    // within that window — so a conviction-ordered fetch would hand it the
    // all-time top 40, which can contain nothing from today and would empty
    // "Watch today" silently.
    //
    // WATCH_SETUPS_LIMIT is in the key because the dashboard's own "Recent
    // theses" query is also ["setups", "recent", market] at sort=recent and
    // asks for 12. Without the limit these share one cache entry and one of
    // the two silently gets the other's page size — this list would be
    // filtering a 12-row page down to 36h and conviction >= 6, and simply
    // show fewer rows. Same prefix, so the invalidateQueries(["setups"])
    // calls in scan-runner-dialog and the ticker page still reach it.
    queryKey: ["setups", "recent", market, WATCH_SETUPS_LIMIT],
    queryFn: () =>
      api.setups({
        limit: WATCH_SETUPS_LIMIT,
        market: market || undefined,
        sort: "recent",
      }),
  });

  const movers = (moversQuery.data?.movers ?? []).filter((m) => m.change_pct !== null);
  const ranked = [...movers].sort((a, b) => (b.change_pct ?? 0) - (a.change_pct ?? 0));
  const gainers = ranked.filter((m) => (m.change_pct ?? 0) > 0).slice(0, TOP_N);
  const losers = ranked
    .filter((m) => (m.change_pct ?? 0) < 0)
    .slice(-TOP_N)
    .reverse();

  const watch = buildWatchlist(setupsQuery.data?.setups ?? [], movers);
  const skipped = Object.keys(moversQuery.data?.skipped_markets ?? {});
  const anyDelayed = movers.some((m) => m.is_delayed_data);
  const asOf = movers[0]?.data_as_of ?? null;

  return (
    <StaleWhenOffline className="grid gap-4 lg:grid-cols-3">
      <SectionCard
        title="Watch today"
        description="Highest-conviction theses, topped up with the day's biggest moves."
        icon={<Eye className="h-4 w-4" />}
        accent="text-primary"
      >
        {setupsQuery.isLoading || moversQuery.isLoading ? (
          <RowSkeletons n={TOP_N} />
        ) : watch.length === 0 ? (
          <Empty>No theses yet — run a scan to populate this.</Empty>
        ) : (
          <div className="space-y-0.5">
            {watch.map((w, i) => (
              <TickerRow
                key={w.code}
                rank={i + 1}
                code={w.code}
                name={w.name}
                price={w.price}
                changePct={w.changePct}
                note={w.note}
              />
            ))}
          </div>
        )}
      </SectionCard>

      <MoverList
        title="Top gainers"
        description="Largest gains against the previous close."
        icon={<ArrowUpRight className="h-4 w-4" />}
        accent="text-bull"
        movers={gainers}
        isLoading={moversQuery.isLoading}
        emptyMessage={
          moversQuery.isError ? "Quotes unavailable." : "Nothing green in this market."
        }
        meta={
          <div className="flex items-center gap-1.5">
            <StaleLabel
              updatedAt={moversQuery.dataUpdatedAt}
              staleAfterMs={MOVERS_STALE_MS}
            />
            <DelayedPill isDelayed={anyDelayed} asOf={asOf} />
          </div>
        }
      />

      <MoverList
        title="Top losers"
        description="Largest falls against the previous close."
        icon={<ArrowDownRight className="h-4 w-4" />}
        accent="text-bear"
        movers={losers}
        isLoading={moversQuery.isLoading}
        emptyMessage={
          moversQuery.isError ? "Quotes unavailable." : "Nothing red in this market."
        }
        // Only the losers card carries this, because one unavailable market
        // is one fact and reporting it on two cards reads as two problems.
        meta={
          skipped.length > 0 ? (
            <Badge variant="outline" title={Object.values(moversQuery.data?.skipped_markets ?? {}).join("; ")}>
              {skipped.join(", ")} unavailable
            </Badge>
          ) : undefined
        }
      />
    </StaleWhenOffline>
  );
}

/**
 * One ranked list of movers in a SectionCard.
 *
 * The gainers and losers cards differ only in their heading, their accent
 * colour, which slice of the sorted list they get, and what sits in the card's
 * meta slot — so everything else, including the three-way
 * loading/empty/populated branch, lives here once.
 *
 * `meta` stays a caller-supplied node rather than a set of flags: the two
 * cards genuinely carry different things there (a freshness label and a
 * delayed-data pill on one, a skipped-markets badge on the other), and
 * parameterising that would be a worse abstraction than passing the node.
 */
function MoverList({
  title,
  description,
  icon,
  accent,
  movers,
  isLoading,
  emptyMessage,
  meta,
}: {
  title: string;
  description: string;
  icon: React.ReactNode;
  /** A text-colour token class, e.g. "text-bull". */
  accent: string;
  /** Already sorted and sliced by the caller — this renders, it does not rank. */
  movers: Mover[];
  isLoading: boolean;
  /** Shown when `movers` is empty. Distinguishes "no data" from "nothing moved". */
  emptyMessage: string;
  meta?: React.ReactNode;
}) {
  return (
    <SectionCard
      title={title}
      description={description}
      icon={icon}
      accent={accent}
      meta={meta}
    >
      {isLoading ? (
        <RowSkeletons n={TOP_N} />
      ) : movers.length === 0 ? (
        <Empty>{emptyMessage}</Empty>
      ) : (
        <div className="space-y-0.5">
          {movers.map((m, i) => (
            <TickerRow
              key={m.code}
              rank={i + 1}
              code={m.code}
              name={m.name}
              price={m.last_price}
              changePct={m.change_pct}
              note={extendedHoursNote(m)}
            />
          ))}
        </div>
      )}
    </SectionCard>
  );
}



type WatchItem = {
  code: string;
  name: string;
  price: number | null;
  changePct: number | null;
  note: React.ReactNode;
};

/**
 * "Worth a look today" — a frontend heuristic, not a backend verdict.
 *
 * There is no stored notion of "interesting", so this composes the two
 * signals that exist: a recent high-conviction thesis is the strongest claim
 * the system can make, and after those run out, an unusually large move is
 * the next best reason to look at something. Ranked by conviction first,
 * then by the size of the move, so an opinion always outranks mere movement.
 */
function buildWatchlist(setups: Setup[], movers: Mover[]): WatchItem[] {
  const priceOf = new Map(movers.map((m) => [m.code, m]));
  const dayOld = Date.now() - 36 * 60 * 60 * 1000;

  const seen = new Set<string>();
  const items: WatchItem[] = [];

  const recent = setups
    .filter((s) => {
      const t = parseIso(s.created_at);
      return !Number.isNaN(t) && t >= dayOld;
    })
    .filter((s) => s.conviction_score >= WATCH_MIN_CONVICTION)
    .sort((a, b) => b.conviction_score - a.conviction_score);

  for (const s of recent) {
    if (seen.has(s.code)) continue;
    seen.add(s.code);
    const m = priceOf.get(s.code);
    items.push({
      code: s.code,
      name: m?.name ?? "",
      price: m?.last_price ?? s.indicator_snapshot?.spot ?? null,
      changePct: m?.change_pct ?? null,
      note: (
        <span>
          <span
            className={
              s.trade_direction === "Bullish"
                ? "text-bull"
                : s.trade_direction === "Bearish"
                  ? "text-bear"
                  : ""
            }
          >
            {s.trade_direction}
          </span>{" "}
          · conviction {s.conviction_score}/10
        </span>
      ),
    });
    if (items.length >= TOP_N) return items;
  }

  const byMove = [...movers]
    .filter((m) => m.change_pct !== null && !seen.has(m.code))
    .sort((a, b) => Math.abs(b.change_pct ?? 0) - Math.abs(a.change_pct ?? 0));

  for (const m of byMove) {
    items.push({
      code: m.code,
      name: m.name,
      price: m.last_price,
      changePct: m.change_pct,
      note: <span className="text-muted-foreground">Unusual move · no thesis yet</span>,
    });
    if (items.length >= TOP_N) break;
  }

  return items;
}
