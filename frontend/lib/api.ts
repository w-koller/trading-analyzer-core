/**
 * Backend API client.
 *
 * The default is the RELATIVE path "/api", served by the route handler in
 * app/api/[...path]/route.ts, which proxies to the backend on localhost. That
 * relative-ness is load-bearing, in three separate ways:
 *
 *   1. Same origin means no CORS and no mixed-content block. Pointing a
 *      browser on an HTTPS page at http://192.168.x.x:8000 fails both of
 *      those tests before a packet leaves, which is exactly how the public
 *      deployment used to present as "Backend unreachable".
 *   2. The session cookie is only sent to the same origin. `credentials`
 *      below is "same-origin", so an ABSOLUTE url here silently stops
 *      authenticating and every call starts returning 401 with no obvious
 *      cause.
 *   3. There is no longer an API token in the browser to fall back on. It
 *      used to live in NEXT_PUBLIC_API_TOKEN, which meant it was compiled
 *      into a public bundle and readable by anyone who loaded the page.
 *
 * So: leave this relative unless you have read all three of those. It is
 * still overridable via NEXT_PUBLIC_API_URL, which is read at BUILD time —
 * changing it needs deploy/rebuild_frontend.sh, not a restart.
 */
export const API_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "/api";

/**
 * The PUBLIC health shape. Deliberately thin.
 *
 * /health is unauthenticated — the dashboard must be able to tell "backend
 * down" from "not signed in" before anyone signs in, and the watchdog probes
 * it with no credentials. So everything identifying was moved to
 * /health/detail: the account id, db path, OpenD and Ollama addresses, the
 * installed model list, and the last scan's per-ticker results.
 *
 * If you need a field that is not here, it is on HealthDetail and it needs a
 * session. Do not add it back to this type.
 */
export type Health = {
  status: string;
  db_exists: boolean;
  ollama_model: string;
  opend: { connected: boolean; qot_logined?: boolean };
  ollama: { reachable: boolean; configured_model_present?: boolean };
  llm_slots?: { active: number; capacity: number };
};

export type Indicators = {
  close: number;
  sma_fast: number | null;
  sma_slow: number | null;
  sma_trend: string;
  sma_cross: string;
  sma_gap_pct: number | null;
  macd: number | null;
  macd_signal: number | null;
  macd_hist: number | null;
  macd_state: string;
  macd_cross: string;
  bb_upper: number | null;
  bb_mid: number | null;
  bb_lower: number | null;
  bb_percent_b: number | null;
  bb_bandwidth: number | null;
  bb_state: string;
  warnings: string[];
};

export type Walls = {
  expiry: string;
  call_wall: number | null;
  call_wall_oi: number;
  call_wall_volume: number;
  put_wall: number | null;
  put_wall_oi: number;
  put_wall_volume: number;
  call_wall_distance_pct: number | null;
  put_wall_distance_pct: number | null;
  put_call_oi_ratio: number | null;
  put_call_volume_ratio: number | null;
  has_walls: boolean;
};

export type Setup = {
  id: number;
  /** Which scan cycle produced this. Used to track live scan progress. */
  scanner_run_id: number | null;
  code: string;
  market: string;
  created_at: string;
  data_as_of: string;
  is_delayed_data: boolean;
  trade_direction: "Bullish" | "Bearish" | "Neutral";
  conviction_score: number;
  reasoning: string;
  suggested_entry: number | null;
  suggested_stop: number | null;
  suggested_target: number | null;
  key_levels_notes: string | null;
  similar_setup_ids: number[];
  indicator_snapshot: {
    indicators: Indicators;
    walls: Walls | null;
    spot: number | null;
    session: string;
    bars_used: number;
    feature_version: number;
  };
  feature_vector: number[];
  feature_breakdown?: Record<string, number>;
  /**
   * How many theses are stored for this ticker in total.
   *
   * Present ONLY on a `latestPerCode` response, because only there does it
   * mean something: on an undeduped page the same ticker already appears
   * that many times, so a "39 theses" chip beside one of 39 identical cards
   * would be describing the page rather than the ticker.
   */
  thesis_count?: number;
};

export type Ticker = {
  code: string;
  name: string;
  market: string;
  enabled: number;
  last_synced_at: string | null;
};

export type Market = "US" | "HK" | "AU";
export const MARKETS: Market[] = ["US", "HK", "AU"];

/**
 * How a thesis list is ordered.
 *
 * "conviction" ranks by what the model rates highest — the default, because
 * the list answers "what is worth looking at". "recent" is a feed, and is
 * what anything time-windowed or progress-tracking needs.
 */
export type SetupSort = "conviction" | "recent";

export type NewsCategory = "all" | "shocks" | "themes" | "macro" | "watchlist";

export type NewsArticle = {
  id: number;
  title: string;
  url: string | null;
  summary: string | null;
  source_label: string;
  feed_key: string;
  category: "shocks" | "themes" | "macro";
  icon: string;
  published_at: string;
  /** True when the feed gave no usable date and fetch time was substituted. */
  published_estimated: boolean;
  age_seconds: number | null;
  codes: { code: string; name: string | null; match_basis: string }[];
  /** Other outlets carrying the same story, folded in at query time. */
  also_in: string[];
};

export type NewsResponse = {
  articles: NewsArticle[];
  count: number;
  collapsed: number;
  counts_by_category: Record<string, number>;
  generated_at: string;
};

export type FeedHealth = {
  key: string;
  label: string;
  category: "shocks" | "themes" | "macro";
  category_label: string;
  url: string;
  icon: string;
  per_ticker: boolean;
  enabled: boolean;
  last_status: string;
  last_success_at: string | null;
  last_attempt_at: string | null;
  last_error: string | null;
  consecutive_failures: number;
  articles_last_run: number;
};

export type ModelSettings = {
  active: string;
  source: "persisted" | "env_default";
  env_default: string;
  available: string[];
  reachable: boolean;
  error: string | null;
  scan_in_progress: boolean;
  warning: string | null;
};

export type Opportunity = {
  code: string;
  name: string;
  market: string;
  horizon: "short" | "medium";
  direction: "Bullish" | "Bearish";
  score: number;
  /** Every input behind the score. The weights are priors, so the ranking
   *  has to be inspectable rather than taken on trust. */
  components: Record<string, number>;
  entry: number | null;
  stop: number | null;
  target: number | null;
  risk_reward: number | null;
  /** Both legs of the ratio: a big R:R can mean a good trade, or a stop too
   *  tight to survive normal noise. The ratio alone cannot tell you which. */
  stop_distance_pct: number | null;
  target_distance_pct: number | null;
  spot: number | null;
  setup_id: number | null;
  conviction: number | null;
  thesis_created_at: string | null;
  agreeing: number;
  of_last: number;
  held: boolean;
  is_delayed_data: boolean;
  data_as_of: string | null;
  notes: string[];
  href: string;
};

export type OpportunitiesResponse = {
  horizons: { short: Opportunity[]; medium: Opportunity[] };
  counts: { short: number; medium: number };
  /** False until the scorecard has both enough samples AND enough distinct
   *  trading days. The UI must not imply a track record that does not exist. */
  calibrated: boolean;
  scored_samples: number;
  min_risk_reward: number;
  min_score: number;
};

export type ScorecardBucket = {
  horizon_days: number;
  direction: string;
  conviction_bucket: string;
  samples: number;
  distinct_days: number;
  hit_rate: number | null;
  mean_return_pct: number | null;
  target_first: number;
  stop_first: number;
  unresolved: number;
  sufficient: boolean;
};

/** What `POST /signals/scorecard/run` reports it did. */
export type ScoringRun = {
  considered: number;
  scored: number;
  rows: number;
  skipped: number;
};

export type ScorecardResponse = {
  buckets: ScorecardBucket[];
  total_samples: number;
  distinct_days: number;
  min_samples: number;
  min_distinct_days: number;
  calibrated: boolean;
  horizons: number[];
};

export type Mover = {
  code: string;
  name: string;
  market: string;
  last_price: number | null;
  prev_close: number | null;
  change_pct: number | null;
  /**
   * The three extended-hours sessions.
   *
   * All three are populated at once during regular trading, so they are a
   * record of the last three off-hours sessions rather than a "which
   * session is it now" signal — decide what to render by which values are
   * non-null, never by the clock.
   *
   * They do NOT share a base: `pre` and `overnight` are measured against the
   * previous close, `after` against today's close. Never present them as
   * differences from one another.
   *
   * A session that never traded is null on BOTH fields (the backend maps
   * OpenD's 0.0 sentinel away), so a rendered 0.00% always means genuinely
   * flat.
   */
  pre_change_pct: number | null;
  pre_price: number | null;
  after_change_pct: number | null;
  after_price: number | null;
  overnight_change_pct: number | null;
  overnight_price: number | null;
  volume: number | null;
  is_delayed_data: boolean;
  data_as_of: string;
};

export type MoversResponse = {
  movers: Mover[];
  count: number;
  /** Markets the account has no quote entitlement for, keyed to the reason. */
  skipped_markets: Record<string, string>;
};

export type Position = {
  code: string;
  name: string;
  market: string;
  qty: number | null;
  avg_cost: number | null;
  market_value: number | null;
  last_price: number | null;
  unrealized_pnl_pct: number | null;
  unrealized_pnl: number | null;
  currency: string;
  position_side: string;
};

export type PositionsResponse = {
  available: boolean;
  reason: string | null;
  positions: Position[];
  count: number;
};

export type SeriesPoint = { time: string; value: number | null };
export type CrossEvent = { time: string; type: string };

export type Kline = {
  time: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
};

/**
 * Chart data, or a plain statement that this account cannot have it.
 *
 * `available: false` is a 200, not an error — the account holds AU.CSL but has
 * no ASX quote entitlement, so there are no ASX bars at any time. A permanent
 * limitation rendered as "502 Bad Gateway" sends you debugging OpenD for
 * something OpenD is answering correctly. A real gateway fault still throws.
 */
export type KlinesResponse = {
  code: string;
  market: string;
  available: boolean;
  reason: string | null;
  is_delayed_data: boolean;
  /** Null when there are no bars — an entitlement gap has no "as of". */
  data_as_of: string | null;
  bars: Kline[];
  overlays: {
    sma_fast: SeriesPoint[];
    sma_slow: SeriesPoint[];
    sma_cross_events: CrossEvent[];
    bollinger: { upper: SeriesPoint[]; mid: SeriesPoint[]; lower: SeriesPoint[] };
    macd: {
      macd: SeriesPoint[];
      signal: SeriesPoint[];
      hist: SeriesPoint[];
      cross_events: CrossEvent[];
    };
  };
  min_rows_available: number;
  warnings: string[];
};


export type AlertSeverity = "critical" | "warn" | "info";

export type PositionAlert = {
  /** `rule:code:discriminator` — stable while the underlying FACT is
   *  unchanged, which is what makes acknowledgement stick across polls and
   *  re-fire when a new thesis or a deeper drawdown makes it a new fact. */
  id: string;
  rule: string;
  severity: AlertSeverity;
  code: string;
  name: string;
  title: string;
  detail: string;
  evidence: Record<string, unknown>;
  href: string;
  acknowledged: boolean;
  acknowledged_until: string | null;
  is_delayed_data?: boolean;
  data_as_of?: string | null;
};

export type AlertsResponse = {
  /** false when the trade session is down — holdings are unknown, which is
   *  NOT the same as "nothing is wrong". */
  available: boolean;
  reason: string | null;
  alerts: PositionAlert[];
  counts: Record<AlertSeverity, number>;
  acknowledged_count: number;
  truncated: number;
  generated_at: string;
};

export type EarningsPubType = "BEFORE" | "AFTER" | "REGULAR" | "UNKNOWN";

export type EarningsOutlook = {
  headline: string;
  what_to_watch: string[];
  news_summary: string | null;
  uncertainty: string | null;
  generated_at: string | null;
  model: string | null;
  sources: Record<string, unknown>;
};

export type EarningsEvent = {
  code: string;
  name: string;
  market: string;
  earnings_date: string;
  days_until: number;
  pub_type: EarningsPubType;
  period_text: string | null;
  eps_predict: number | null;
  eps_actual: number | null;
  revenue_predict: number | null;
  iv: number | null;
  iv_rank: number | null;
  iv_percentile: number | null;
  outlook: EarningsOutlook | null;
};

export type EarningsResponse = {
  events: EarningsEvent[];
  count: number;
  horizon_days: number;
  /** Markets this source permanently does not cover, keyed by market. */
  unsupported_markets: Record<string, string>;
  refreshed_at: string | null;
};

/**
 * A failed request, classified.
 *
 * The distinction matters to the UI: a timeout or a network error means the
 * backend is unreachable and retrying will help, while a 4xx means the
 * request itself was wrong and retrying is just noise. Without a `kind` the
 * caller can only guess from a string.
 */
export class ApiError extends Error {
  readonly kind: "timeout" | "network" | "http";
  readonly status?: number;
  readonly path: string;

  constructor(kind: "timeout" | "network" | "http", path: string, message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.path = path;
    this.status = status;
  }

  get unreachable() {
    return this.kind === "timeout" || this.kind === "network";
  }
}

/** Most calls should answer well inside this; a wedged backend must not hang forever. */
const DEFAULT_TIMEOUT_MS = 10_000;

/**
 * A fingerprint is `rule:code:discriminator` and the backend matches it with
 * a `:path` converter, so the separators must survive. encodeURIComponent
 * would escape the colons and the route would stop matching; only the
 * genuinely unsafe characters are escaped here.
 */
export function encodePath(value: string): string {
  return value.split("/").map(encodeURIComponent).join("/");
}

/**
 * Every request is credentialled and same-origin.
 *
 * There is deliberately no header helper any more. Authentication is the
 * `ta_session` cookie, which is HttpOnly — so JS cannot read it, cannot
 * accidentally log it, and an XSS cannot exfiltrate it. That is strictly
 * better than the bearer token this replaced, which had to be readable by
 * the very code an attacker would be running.
 *
 * "same-origin" is the fetch default, stated explicitly because it is the
 * thing that silently breaks if API_URL is ever made absolute.
 */
export const CREDENTIALS: RequestCredentials = "same-origin";

async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs: number | null = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      cache: "no-store",
      credentials: CREDENTIALS,
      // null disables the timeout entirely — see the scan endpoints below.
      signal: timeoutMs === null ? undefined : AbortSignal.timeout(timeoutMs),
      ...init,
    });
  } catch (e) {
    const isAbort = e instanceof DOMException && e.name === "TimeoutError";
    throw new ApiError(
      isAbort ? "timeout" : "network",
      path,
      isAbort
        ? `Timed out after ${(timeoutMs ?? 0) / 1000}s on ${path}`
        : `Could not reach the backend on ${path}`,
    );
  }
  if (!res.ok) {
    throw new ApiError("http", path, `${res.status} ${res.statusText} on ${path}`, res.status);
  }
  return res.json() as Promise<T>;
}

/**
 * One helper per HTTP verb, so no call site hand-rolls headers or JSON.
 *
 * `timeoutMs` defaults to DEFAULT_TIMEOUT_MS; passing `null` disables the
 * timeout entirely, which several endpoints below genuinely need — see the
 * note on runScan. It is a real parameter default rather than an
 * `=== undefined` check inside, so `null` still reaches request() unchanged.
 */
async function get<T>(path: string, timeoutMs: number | null = DEFAULT_TIMEOUT_MS): Promise<T> {
  return request<T>(path, {}, timeoutMs);
}

/** Shared by every verb that carries a JSON body. */
function jsonBody(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    // Distinguishes "no body" from "a body that is literally null": several
    // POSTs here are pure commands and must send nothing at all.
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

async function post<T>(
  path: string, body?: unknown, timeoutMs: number | null = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  return request<T>(path, jsonBody("POST", body), timeoutMs);
}

async function put<T>(
  path: string, body?: unknown, timeoutMs: number | null = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  return request<T>(path, jsonBody("PUT", body), timeoutMs);
}

async function patch<T>(
  path: string, body?: unknown, timeoutMs: number | null = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  return request<T>(path, jsonBody("PATCH", body), timeoutMs);
}

/** Named `del` because `delete` is a reserved word. */
async function del<T>(
  path: string, timeoutMs: number | null = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  return request<T>(path, { method: "DELETE" }, timeoutMs);
}

const qs = (params: Record<string, string | number | undefined | null>) => {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
};

export type AuthState = { authenticated: boolean; login_configured: boolean };

export const api = {
  health: () => get<Health>("/health"),

  /**
   * Session state.
   *
   * Always 200, even when signed out, for the same reason /health is: the
   * caller needs to tell "not signed in" apart from "backend unreachable",
   * and a 401 would be indistinguishable from the transport failure it is
   * trying to rule out.
   */
  authState: () => get<AuthState>("/auth/me"),
  login: (password: string, totp: string) =>
    post<{ ok: boolean; expires_at: string }>("/auth/login", { password, totp }),
  logout: () => post<{ ok: boolean }>("/auth/logout"),
  logoutAll: () => post<{ ok: boolean; ended: number }>("/auth/logout-all"),

  pushStatus: () =>
    get<{
      configured: boolean;
      public_key: string | null;
      subscriptions: number;
      min_severity: string;
    }>("/push/status"),
  pushSubscribe: (subscription: Record<string, unknown>) =>
    post<{ ok: boolean }>("/push/subscribe", subscription),
  pushUnsubscribe: (endpoint: string) =>
    post<{ ok: boolean; removed: boolean }>("/push/unsubscribe", { endpoint }),
  pushTest: () =>
    post<{ ok: boolean; sent: number; gone: number; failed: number }>("/push/test"),

  /**
   * List stored theses.
   *
   * An options object rather than positional arguments, and `sort` is
   * required, because the two orders are not interchangeable and the
   * difference is invisible at the call site otherwise. A caller that means
   * "recent" but inherits a conviction default gets a page that can contain
   * nothing from today — which then renders as an empty section rather than
   * an error. Making every caller say which it means is the cheap way to
   * stop that recurring.
   */
  opportunities: (market?: string) =>
    get<OpportunitiesResponse>(`/signals/opportunities${qs({ market })}`),
  /**
   * The whole scorecard in one call — every horizon, ungrouped.
   *
   * Deliberately unparameterised even though the endpoint accepts `horizon`.
   * A horizon only produces buckets once that many forward bars exist, so on
   * a young corpus most of them are legitimately empty; fetching per horizon
   * would fire five requests to learn that. One response also carries
   * `horizons`, which is what lets the UI tell "no data yet" apart from "this
   * horizon is not a thing".
   */
  scorecard: () => get<ScorecardResponse>("/signals/scorecard"),
  /**
   * Score theses whose future has now happened, rather than waiting for the
   * nightly job.
   *
   * Given a long timeout for the same reason `refreshEarnings` is: it walks
   * unscored setups and can reach OpenD on a kline cache miss, so the 10s
   * default would abort a call the backend then completes anyway.
   *
   * 409s when a scan holds the gateway. That is an ordinary "not now", not a
   * failure — the same rows stay unscored and the nightly job would pick them
   * up regardless, so the caller must render it as a wait rather than an
   * error.
   */
  runScoring: (limit?: number) =>
    post<ScoringRun>(`/signals/scorecard/run${qs({ limit })}`, undefined, 120_000),

  setups: (opts: {
    limit?: number;
    market?: string;
    minConviction?: number;
    sort: SetupSort;
    /** Restrict to one ticker — the per-ticker thesis history. */
    code?: string;
    /**
     * Collapse to one row per ticker, the newest thesis for each.
     *
     * Opt-in and OFF by default, and it must stay that way: three callers
     * structurally depend on seeing every row, and they fail in three
     * different silent ways if this is ever defaulted on. The dashboard feed
     * and `buildWatchlist`'s 36h window would under-fill; worst,
     * `scan-runner-dialog` counts rows carrying the running scan's id to
     * drive its progress bar, so one-row-per-ticker would cap it at the
     * watchlist size and pin it near-flat for the hour a pre-market scan
     * takes.
     */
    latestPerCode?: boolean;
  }) =>
    get<{
      setups: Setup[];
      count: number;
      sort: SetupSort;
      latest_per_code: boolean;
    }>(
      `/setups${qs({
        limit: opts.limit ?? 50,
        market: opts.market,
        min_conviction: opts.minConviction,
        sort: opts.sort,
        code: opts.code,
        // qs drops undefined; a bare `false` would serialise as "false",
        // which FastAPI parses as truthy-shaped and is needless noise.
        latest_per_code: opts.latestPerCode ? 1 : undefined,
      })}`,
    ),
  setup: (id: number) => get<Setup>(`/setups/${id}`),
  latestSetup: (code: string) => get<Setup>(`/setups/latest/${encodeURIComponent(code)}`),
  similar: (id: number) =>
    get<{ setup_id: number; similar: SimilarSetup[] }>(`/setups/${id}/similar`),
  watchlist: (market?: string) =>
    get<{ tickers: Ticker[]; count: number }>(`/watchlist${qs({ market })}`),
  movers: (market?: string) => get<MoversResponse>(`/watchlist/movers${qs({ market })}`),
  positions: (market?: string) => get<PositionsResponse>(`/positions${qs({ market })}`),
  klines: (code: string, days?: number) =>
    get<KlinesResponse>(`/market/${encodeURIComponent(code)}/klines${qs({ days })}`),
  scanStatus: () => get<ScanStatus>("/scan/status"),
  news: (opts: { category?: NewsCategory; code?: string; limit?: number; sinceHours?: number } = {}) =>
    get<NewsResponse>(
      `/news${qs({
        category: opts.category,
        code: opts.code,
        limit: opts.limit ?? 50,
        since_hours: opts.sinceHours,
      })}`,
    ),
  // Stored rows only — this never touches OpenD, so it keeps answering
  // through the hour a pre-market scan owns the gateway.
  alerts: () => get<AlertsResponse>("/alerts"),
  ackAlert: (fingerprint: string) =>
    post<{ expires_at: string }>(`/alerts/${encodePath(fingerprint)}/ack`),
  unackAlert: (fingerprint: string) =>
    del<{ removed: boolean }>(`/alerts/${encodePath(fingerprint)}/ack`),
  earnings: (opts: { days?: number; market?: string } = {}) =>
    get<EarningsResponse>(`/earnings${qs({ days: opts.days, market: opts.market })}`),
  // Two OpenD calls per market; 409s while a scan holds the gateway.
  refreshEarnings: () =>
    post<Record<string, unknown>>("/earnings/refresh", undefined, 120_000),
  // One model call, 60-120s. Exempt from the default timeout for the same
  // reason runScan is — see its note.
  generateOutlook: (code: string) =>
    post<EarningsOutlook & { code: string }>(
      `/earnings/${encodeURIComponent(code)}/outlook`,
      undefined,
      null,
    ),
  topStories: (limit = 9) => get<NewsResponse>(`/news/top${qs({ limit })}`),
  newsFeeds: () =>
    get<{ feeds: FeedHealth[]; count: number; failing: string[] }>("/news/feeds"),
  // 16 feeds over 4 workers, each up to 10s — well past the 10s default.
  refreshNews: () => post<Record<string, unknown>>("/news/refresh", undefined, 180_000),
  modelSettings: () => get<ModelSettings>("/settings/model"),
  setModel: (model: string) => put<ModelSettings>("/settings/model", { model }),
  resetModel: () => post<ModelSettings>("/settings/model/reset"),
  resumeScan: () => post<ScanStatus>("/scan/schedule/resume"),
  pauseScan: () => post<ScanStatus>("/scan/schedule/pause"),
  scanRuns: (limit = 5) => get<{ runs: ScanRun[]; count: number }>(`/scan/runs?limit=${limit}`),
  livez: () => get<{ status: string; pid: number; uptime_seconds: number }>("/livez", 5_000),
  // NOTE: the scan calls below pass `null` for the timeout deliberately.
  // POST /scan/run blocks for 60-120s PER TICKER and a full watchlist run
  // legitimately exceeds an hour (decisions #22). Applying the 10s default
  // here would abort the request while the backend kept working, which would
  // break the headline feature in the name of resilience.
  runScan: (codes: string[]) =>
    post<CycleResult>("/scan/run", { codes, sync_first: false }, null),
  /**
   * Scan the whole enabled watchlist. `max_tickers: null` is what turns the
   * scanner's per-cycle slice into a full pass; without it the backend
   * defaults to 3 tickers and the button silently under-delivers.
   *
   * This blocks for the entire run (60-120s per ticker, so potentially over
   * an hour). That is deliberate on the backend's side — see routers/scan.py.
   */
  runFullScan: (market?: string) =>
    post<CycleResult>(
      "/scan/run",
      { max_tickers: null, sync_first: false, market: market || undefined },
      null, // see the note on runScan — this one can run for over an hour
    ),
  // A full sync is rate-limited to 8 group reads per 30s, so ~60s is normal
  // and expected — not a hang. Given a generous ceiling rather than the default.
  syncWatchlist: () => post<Record<string, unknown>>("/watchlist/sync", undefined, 180_000),
  setEnabled: (code: string, enabled: boolean) =>
    patch<{ code: string; enabled: boolean }>(
      `/watchlist/${encodeURIComponent(code)}/enabled`,
      { enabled },
    ),

  /**
   * The rotation board for one window.
   *
   * `window` is required rather than defaulted for the same reason
   * `setups`' `sort` is: the four windows read different evidence and are
   * not interchangeable, so a caller that inherits a default gets a reading
   * about a timeframe it did not mean to ask about.
   */
  sectorRotation: (opts: {
    market?: string;
    window: SectorWindow;
    topN?: number;
    plateClass?: PlateClass;
  }) =>
    get<RotationBoard>(
      `/sectors/rotation${qs({
        market: opts.market,
        window: opts.window,
        top_n: opts.topN,
        plate_class: opts.plateClass,
      })}`,
    ),
  sectorPairs: (opts: { market?: string; window: SectorWindow; topN?: number }) =>
    get<RotationPairs>(
      `/sectors/pairs${qs({ market: opts.market, window: opts.window, top_n: opts.topN })}`,
    ),
  sector: (plateCode: string, window: SectorWindow) =>
    get<SectorDetail>(`/sectors/${encodePath(plateCode)}${qs({ window })}`),
  sectorUniverse: (market?: string) =>
    get<SectorUniverse>(`/sectors${qs({ market })}`),
  /**
   * Re-ingest and rescore now.
   *
   * No timeout: this makes ~300 quote calls plus a paced ETF pass, so the
   * 10s default would abort a call the backend then completes anyway — the
   * same reasoning as `runScan`.
   *
   * 409s when a scan holds the gateway, and that is a "not now" rather than
   * a failure: the kline call backfills, so the next scheduled run writes
   * exactly what this one would have. Render it as a wait.
   */
  refreshSectors: (market?: string) =>
    post<Record<string, unknown>>(`/sectors/refresh${qs({ market })}`, undefined, null),
  /**
   * The stored narrative for one sector. Read-only — it never generates on
   * demand, because a narrative costs GPU time and a page load must not be
   * able to spend it.
   */
  sectorNarrative: (plateCode: string, window: SectorWindow) =>
    get<SectorNarrative>(`/sectors/${encodePath(plateCode)}/narrative${qs({ window })}`),
  /**
   * Write narratives for the day's biggest movers now.
   *
   * No timeout, like `runScan`: three to six generations at 30-120s each.
   * Unlike `refreshSectors` this never 409s on a scan — it makes no market
   * data calls at all.
   */
  runSectorNarratives: (window: SectorWindow, topN?: number) =>
    post<{ written: number; considered: number; failures: string[] }>(
      `/sectors/narratives/run${qs({ window, top_n: topN })}`, undefined, null),
};

/* --- sector rotation ---------------------------------------------------- */

/** A window in SESSIONS, not calendar days: daily / weekly / monthly / quarterly. */
export type SectorWindow = 1 | 5 | 21 | 63;
export type PlateClass = "INDUSTRY" | "CONCEPT";

export type SectorScore = {
  plate_code: string;
  plate_name: string;
  plate_class: PlateClass;
  sector_group: string | null;
  as_of_date: string;
  window_days: number;
  score: number | null;
  /** The signed parts the score was summed from. Shipped because a ranking
   *  whose ranking cannot be inspected is a black box. */
  components: Record<string, number>;
  rel_return_pct: number | null;
  turnover_thrust: number | null;
  breadth: number | null;
  persistence: number | null;
  news_thrust: number | null;
  sessions_used: number;
  /** 0 means the rotating member refresh has not reached this plate — that is
   *  UNKNOWN, not "no constituents". Render it as such. */
  constituents: number | null;
  coverage: number;
  thin_session: boolean;
  sufficient: boolean;
};

export type RotationBoard = {
  available: boolean;
  reason: string | null;
  market: string;
  window_days: number;
  windows: number[];
  plate_class: PlateClass | null;
  as_of_date?: string;
  scored?: number;
  sufficient?: number;
  thin_session?: boolean;
  /** Thresholds come from the response, never a local constant — that rule
   *  belongs to the service, and a second copy here is a second thing to
   *  forget to change. */
  min_constituents: number;
  min_sessions: number;
  /** What the score's zero actually means. Not "the market". */
  baseline: string;
  inflow: SectorScore[];
  outflow: SectorScore[];
};

export type RotationPair = {
  from: { plate_code: string; plate_name: string; score: number; sufficient: boolean };
  to: { plate_code: string; plate_name: string; score: number; sufficient: boolean };
  link_basis: string;
  shared_members: number;
  jaccard: number;
  spread: number;
  both_sufficient: boolean;
};

export type RotationPairs = {
  available: boolean;
  reason: string | null;
  market?: string;
  window_days?: number;
  pairs: RotationPair[];
  note: string;
  /** How much of the ranked board has constituent data. Pairing needs BOTH
   *  sides, and member lists load a slice at a time, so this is what lets the
   *  UI say "still loading" rather than "nothing is rotating". */
  coverage?: { rows: number; with_members: number; share: number };
};

export type SectorEtfFlow = {
  code: string;
  label: string;
  sessions: number;
  net_flow: number;
  main_flow: number;
  institutional_share: number | null;
  units: {
    available: boolean;
    reason: string | null;
    sessions: number;
    min_sessions: number;
    unit_change?: number;
    estimated_flow?: number | null;
    note?: string;
  };
};

export type SectorMember = {
  code: string;
  name: string | null;
  on_watchlist: boolean;
  enabled: boolean;
};

export type SectorDetail = {
  available: boolean;
  reason: string | null;
  plate_code: string;
  plate_name?: string;
  plate_class?: PlateClass;
  sector_group?: string | null;
  market?: string;
  window_days?: number;
  windows?: number[];
  constituent_count?: number;
  score?: SectorScore | null;
  history?: { as_of_date: string; score: number | null; sufficient: number }[];
  members?: SectorMember[];
  watchlist_members?: SectorMember[];
  related?: {
    plate_code: string;
    plate_name: string;
    plate_class: PlateClass;
    shared_members: number;
    jaccard: number;
  }[];
  etf_flow?: {
    plate_code: string;
    days: number;
    etfs: SectorEtfFlow[];
    note: string;
  } | null;
  min_constituents?: number;
};

export type SectorUniverse = {
  available: boolean;
  reason: string | null;
  market: string;
  windows: number[];
  counts: Record<string, number>;
  members_unvisited: number;
  universe_age_days: number | null;
  universe_max_age_days: number;
  plates: {
    plate_code: string;
    plate_name: string;
    plate_class: PlateClass;
    sector_group: string | null;
    constituent_count: number;
  }[];
};

/**
 * A model-written note about one sector's move.
 *
 * Carries NO numeric field, deliberately and permanently. The rotation score
 * beside it is computed in Python; a model-authored number here would look
 * comparable when it is nothing of the kind. `confidence_label` is three
 * fixed words for exactly that reason — a 1-10 rating would get averaged and
 * plotted.
 *
 * `supporting_headlines` are validated server-side against the titles the
 * model was actually shown, so a citation here cannot name an article that
 * does not exist.
 */
export type SectorNarrative = {
  available: boolean;
  reason: string | null;
  plate_code: string;
  window_days: number;
  as_of_date?: string;
  headline?: string;
  candidate_driver?: string;
  supporting_headlines?: string[];
  contradicts?: string;
  confidence_label?: "news explains it" | "news is consistent" | "no news explains it";
  model?: string;
  sources?: Record<string, unknown>;
  generated_at?: string;
  disclaimer?: string;
};

export type SectorWindowLabel = { value: SectorWindow; label: string };

/** Labels for the four windows. In SESSIONS, so "1 week" is 5 trading days
 *  rather than 7 calendar ones — the difference matters on a holiday week. */
export const SECTOR_WINDOWS: SectorWindowLabel[] = [
  { value: 1, label: "1 day" },
  { value: 5, label: "1 week" },
  { value: 21, label: "1 month" },
  { value: 63, label: "1 quarter" },
];

export type SimilarSetup = {
  setup_id: number;
  code: string;
  created_at: string;
  trade_direction: string;
  conviction_score: number;
  similarity: number;
  outcome: { pnl_pct: number | null; exit_reason: string | null } | null;
};

export type ScanStatus = {
  running: boolean;
  paused: boolean;
  next_run: string | null;
  interval_seconds?: number;
  /**
   * "persisted" = the user chose this and it survived a restart;
   * "default"   = never chosen, so the paused-on-boot default applies.
   * Worth showing, because "paused because you paused it" and "paused because
   * nobody has ever turned it on" call for different responses.
   */
  rotation_state_source?: "persisted" | "default";
  scan_in_progress?: boolean;
  premarket_lead_minutes?: number;
  /**
   * When theses next refresh.
   *
   * This, not `next_run`, is the number a human wants. `next_run` is the
   * gap-filler's next tick — a repair path that normally scans nothing —
   * whereas this is the soonest full-watchlist scan.
   */
  next_session_scan?: string | null;
  /** One entry per (market, trading session): four for the US, three for
   *  HK, two for AU. `premarket` is the same list under its former name. */
  session_scans?: { id: string; next_run: string | null; paused: boolean }[];
  premarket?: { id: string; next_run: string | null; paused: boolean }[];
  last_result: unknown;
  last_premarket?: Record<string, unknown>;
  scorecard?: { running: boolean; next_run: string | null; last_result: unknown };
};

export type ScanRun = {
  id: number;
  started_at: string;
  finished_at: string | null;
  tickers_scanned: number;
  tickers_succeeded: number;
  tickers_failed: number;
  status: "running" | "completed" | "failed";
  error_summary: string | null;
};

export type CycleResult = {
  run_id: number;
  scanned: number;
  succeeded: number;
  failed: number;
  elapsed_seconds: number;
  results: { code: string; ok: boolean; setup_id: number | null; error: string | null }[];
};
