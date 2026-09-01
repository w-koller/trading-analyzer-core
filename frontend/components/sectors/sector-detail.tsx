"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { GlossaryTerm } from "@/components/glossary-term";
import { SectorNarrative } from "@/components/sectors/sector-narrative";
import { api, type SectorWindow } from "@/lib/api";
import { bareTicker, compactNum } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * One sector in full: its score components, its recent history, which of the
 * user's own names sit inside it, and any signed ETF flow.
 *
 * The watchlist join is what makes a market-wide macro view actionable on a
 * 50-ticker account — "capital is leaving semis" matters rather differently
 * when you hold three of them.
 */
export function SectorDetail({
  plateCode,
  window,
  onClose,
}: {
  plateCode: string;
  window: SectorWindow;
  onClose: () => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["sectors", "detail", plateCode, window],
    queryFn: () => api.sector(plateCode, window),
  });

  if (isLoading) return <Skeleton className="h-64 w-full" />;
  if (!data?.available) {
    return (
      <Card className="border-dashed p-6 text-center text-xs text-muted-foreground">
        {data?.reason ?? "Unknown sector."}
      </Card>
    );
  }

  const score = data.score ?? null;
  const held = data.watchlist_members ?? [];
  const history = data.history ?? [];

  return (
    <Card className="min-w-0 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold">{data.plate_name}</h3>
          <p className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
            <Badge variant="outline">{data.plate_class}</Badge>
            {/* 0 means the rotating member fetch has not reached this plate.
                That is UNKNOWN, not "no constituents", and saying so is the
                difference between an honest gap and a wrong number. */}
            <span>
              {data.constituent_count
                ? `${data.constituent_count} constituents`
                : "constituents not yet counted"}
            </span>
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close sector detail"
          className="shrink-0 rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {score ? (
        <>
          <div className="mt-3 flex items-baseline gap-2">
            <span
              className={cn(
                "text-2xl font-semibold tabular",
                !score.sufficient
                  ? "text-muted-foreground"
                  : (score.score ?? 0) > 0
                    ? "text-bull"
                    : "text-bear",
              )}
            >
              {(score.score ?? 0) > 0 ? "+" : ""}
              {(score.score ?? 0).toFixed(2)}
            </span>
            <span className="text-[11px] text-muted-foreground">
              <GlossaryTerm term="rotation_score">rotation score</GlossaryTerm> ·{" "}
              {score.sessions_used} sessions
              {!score.sufficient && " · unconfirmed"}
            </span>
          </div>

          <Components score={score} />
          {history.length > 1 && <Sparkline history={history} />}
        </>
      ) : (
        <p className="mt-3 text-xs text-muted-foreground">
          Not scored for this window yet.
        </p>
      )}

      {/* Below the measured components and above the constituent lists: it
          interprets the numbers above it, and must not be the first thing
          read. Renders nothing when no narrative has been written. */}
      <SectorNarrative plateCode={plateCode} window={window} />

      {data.etf_flow && <EtfFlow flow={data.etf_flow} />}

      <Section title={`Your names in this sector (${held.length})`}>
        {held.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            None of your watchlist tickers are in this sector.
          </p>
        ) : (
          <div className="flex flex-wrap gap-1">
            {held.map((m) => (
              <Link
                key={m.code}
                href={`/ticker/${encodeURIComponent(m.code)}`}
                className="rounded-md border px-1.5 py-0.5 text-[11px] font-medium transition-colors hover:bg-muted"
              >
                {bareTicker(m.code)}
              </Link>
            ))}
          </div>
        )}
      </Section>

      {(data.related?.length ?? 0) > 0 && (
        <Section title="Related sectors">
          <div className="flex flex-wrap gap-1">
            {data.related!.map((r) => (
              <span
                key={r.plate_code}
                className="rounded-md border px-1.5 py-0.5 text-[11px] text-muted-foreground"
                title={`${r.shared_members} shared constituents`}
              >
                {r.plate_name}
              </span>
            ))}
          </div>
        </Section>
      )}
    </Card>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-3 border-t pt-2">
      <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {title}
      </p>
      {children}
    </div>
  );
}

/**
 * The parts the score was summed from.
 *
 * Rendered because the weights are priors with nothing fitted to outcomes: a
 * ranking whose ranking cannot be inspected is a black box. Stacked rows
 * rather than a table, so it survives a 320px phone.
 */
function Components({ score }: { score: { components: Record<string, number>; rel_return_pct: number | null } }) {
  const LABELS: Record<string, { label: string; term?: string }> = {
    rel_return: { label: "Return vs median", term: "relative_return" },
    turnover_thrust: { label: "Volume thrust", term: "turnover_thrust" },
    breadth: { label: "Breadth", term: "sector_breadth" },
    persistence: { label: "Persistence", term: "persistence" },
    acceleration: { label: "Acceleration", term: "acceleration" },
  };
  const entries = Object.entries(score.components ?? {});
  if (entries.length === 0) return null;
  return (
    <dl className="mt-2 space-y-1">
      {entries.map(([k, v]) => {
        const meta = LABELS[k] ?? { label: k };
        return (
          <div key={k} className="flex items-baseline justify-between gap-2 text-[11px]">
            <dt className="text-muted-foreground">
              {meta.term ? (
                <GlossaryTerm term={meta.term}>{meta.label}</GlossaryTerm>
              ) : (
                meta.label
              )}
            </dt>
            <dd className={cn("tabular", v > 0 ? "text-bull" : v < 0 ? "text-bear" : "text-flat")}>
              {v > 0 ? "+" : ""}
              {v.toFixed(2)}
            </dd>
          </div>
        );
      })}
      {score.rel_return_pct !== null && (
        <div className="flex items-baseline justify-between gap-2 border-t pt-1 text-[11px]">
          <dt className="text-muted-foreground">
            <GlossaryTerm term="actual_move">Actual move vs median</GlossaryTerm>
          </dt>
          <dd className="tabular">
            {score.rel_return_pct > 0 ? "+" : ""}
            {score.rel_return_pct.toFixed(2)}%
          </dd>
        </div>
      )}
    </dl>
  );
}

/**
 * Score history, on a FIXED [-1, +1] domain.
 *
 * Never autoscaled — the same argument the conviction sparkline already
 * makes: an autoscaled axis renders a wobble between -0.05 and +0.05 as a
 * dramatic reversal. currentColor throughout, so it inherits the theme
 * without parsing the bare-HSL tokens by hand.
 */
function Sparkline({ history }: { history: { as_of_date: string; score: number | null }[] }) {
  const pts = history.filter((h) => h.score !== null);
  if (pts.length < 2) return null;
  const W = 220;
  const H = 32;
  const x = (i: number) => (i / (pts.length - 1)) * W;
  const y = (v: number) => H / 2 - (Math.max(-1, Math.min(1, v)) * H) / 2;
  const d = pts.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.score!).toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1].score!;
  return (
    <div className="mt-3">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className={cn("h-8 w-full", last > 0 ? "text-bull" : "text-bear")}
        role="img"
        aria-label={`Rotation score over the last ${pts.length} readings, currently ${last.toFixed(2)}`}
        preserveAspectRatio="none"
      >
        <line x1="0" y1={H / 2} x2={W} y2={H / 2} className="text-border" stroke="currentColor" strokeWidth="1" strokeDasharray="2 3" />
        <path d={d} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      </svg>
      <p className="mt-0.5 text-[10px] text-muted-foreground">
        Last {pts.length} readings, fixed −1 to +1 scale
      </p>
    </div>
  );
}

/** Signed institutional flow, where an ETF tracks this sector. */
function EtfFlow({
  flow,
}: {
  flow: NonNullable<import("@/lib/api").SectorDetail["etf_flow"]>;
}) {
  return (
    <Section title="Institutional flow">
      <div className="space-y-1.5">
        {flow.etfs.map((e) => (
          <div key={e.code} className="text-[11px]">
            <div className="flex items-baseline justify-between gap-2">
              <span className="min-w-0 truncate text-muted-foreground">
                {bareTicker(e.code)} · {e.label}
              </span>
              <span
                className={cn("shrink-0 tabular", e.main_flow > 0 ? "text-bull" : "text-bear")}
              >
                {e.main_flow > 0 ? "+" : "−"}
                {compactNum(Math.abs(e.main_flow))}
              </span>
            </div>
            <p className="text-[10px] text-muted-foreground">
              <GlossaryTerm term="main_in_flow">block-sized</GlossaryTerm> over {e.sessions}{" "}
              sessions
              {e.institutional_share !== null &&
                ` · ${(e.institutional_share * 100).toFixed(0)}% of gross activity`}
              {!e.units.available && ` · ${e.units.reason}`}
              {e.units.available &&
                e.units.estimated_flow != null &&
                ` · shares outstanding ${e.units.unit_change! > 0 ? "+" : "−"}${compactNum(
                  Math.abs(e.units.estimated_flow),
                )}`}
            </p>
          </div>
        ))}
      </div>
      <p className="mt-1.5 text-[10px] leading-relaxed text-muted-foreground">{flow.note}</p>
    </Section>
  );
}
