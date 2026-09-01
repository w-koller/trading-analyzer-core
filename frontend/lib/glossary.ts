/**
 * Plain-English definitions for the technical terms the dashboard shows.
 *
 * Static content on purpose — these describe what the backend computes and
 * change only when the indicators themselves do, so there is nothing for an
 * endpoint to serve. Definitions are written for someone who knows what a
 * stock is and not much more, and say what the number *means for a decision*
 * rather than restating the formula.
 */

export type GlossaryEntry = {
  term: string;
  short: string;
  detail?: string;
};

export const GLOSSARY: Record<string, GlossaryEntry> = {
  sma: {
    term: "SMA (Simple Moving Average)",
    short:
      "The average closing price over the last N days, redrawn each day. It smooths out daily noise so the underlying direction is visible.",
    detail:
      "This dashboard uses a fast 50-day and a slow 200-day SMA. Price above both is generally read as an uptrend.",
  },
  sma_cross: {
    term: "SMA cross",
    short:
      "The moment the 50-day average crosses through the 200-day one. Traders treat it as a change of medium-term trend.",
    detail:
      "Crossing upward is a 'golden cross' (bullish), downward is a 'death cross' (bearish). It is a lagging signal — it confirms a move that has already begun.",
  },
  golden_cross: {
    term: "Golden cross",
    short:
      "The 50-day average crossing up through the 200-day average — a conventional bullish trend signal.",
    detail: "Lagging by construction: it describes a move already underway.",
  },
  death_cross: {
    term: "Death cross",
    short:
      "The 50-day average crossing down through the 200-day average — a conventional bearish trend signal.",
    detail: "Lagging by construction: it describes a move already underway.",
  },
  sma_gap: {
    term: "SMA gap",
    short:
      "How far apart the fast and slow averages are, as a percentage. A widening gap means the trend is accelerating; a narrowing one means it is losing steam.",
  },
  macd: {
    term: "MACD",
    short:
      "The difference between two exponential moving averages (12-day minus 26-day). It measures momentum — whether a move is speeding up or slowing down.",
    detail:
      "Above zero means shorter-term prices are running ahead of longer-term ones.",
  },
  macd_signal: {
    term: "MACD signal line",
    short:
      "A 9-day average of the MACD itself. MACD crossing above it is read as bullish momentum, crossing below as bearish.",
  },
  macd_hist: {
    term: "MACD histogram",
    short:
      "MACD minus its signal line. The bars shrinking toward zero is often the earliest hint that a move is running out of energy.",
  },
  bollinger: {
    term: "Bollinger Bands",
    short:
      "A 20-day average with bands two standard deviations above and below. The bands widen when the market is volatile and squeeze when it is calm.",
    detail:
      "Price touching a band is not itself a buy or sell signal — in a strong trend price can ride the upper band for weeks.",
  },
  percent_b: {
    term: "%B",
    short:
      "Where price sits between the Bollinger bands: 0 is the lower band, 1 is the upper. Above 1 or below 0 means price has closed outside the bands entirely.",
  },
  bandwidth: {
    term: "Bandwidth",
    short:
      "How wide the Bollinger bands are relative to their middle. A historically low value (a 'squeeze') often precedes a large move, without saying which way.",
  },
  option_wall: {
    term: "Option wall",
    short:
      "A strike price with an unusually large number of outstanding option contracts. Such levels often act as short-term support or resistance.",
    detail:
      "Ranked here on open interest plus volume — open interest is published once a day and can be a session stale, while volume is same-day flow.",
  },
  call_wall: {
    term: "Call wall",
    short:
      "The strike above the current price with the heaviest call positioning — often behaves like a ceiling into expiry.",
  },
  put_wall: {
    term: "Put wall",
    short:
      "The strike below the current price with the heaviest put positioning — often behaves like a floor into expiry.",
  },
  put_call_ratio: {
    term: "Put/call ratio",
    short:
      "Puts divided by calls. Above 1 means more downside positioning than upside, which is usually read as bearish (or as hedging).",
  },
  conviction_score: {
    term: "Conviction score",
    short:
      "The model's own 1-10 confidence in its thesis, given the indicators and how similar past setups actually turned out.",
    detail:
      "It is an opinion, not a probability. The Track record tab shows whether higher conviction has actually meant a better hit rate so far.",
  },
  hit_rate: {
    term: "Hit rate",
    short:
      "How often a thesis pointed the right way — the share of Bullish calls the price rose after, and Bearish calls it fell after.",
    detail:
      "Measured against the bars that came after the thesis, not against trades anyone made. Neutral theses have no hit rate: they make no directional claim, so scoring them either way would invent one.",
  },
  forward_return: {
    term: "Mean forward return",
    short:
      "The average price change over the horizon, counted from the thesis onward. Positive for every direction just means the market rose.",
    detail:
      "Read it alongside the hit rate, not instead of it. A Bearish bucket can show a positive mean return and a low hit rate at once — that is the model being wrong in a rising market, not a good result.",
  },
  resolution: {
    term: "Resolution",
    short:
      "Which of the thesis's own levels the price reached first within the horizon: its target, its stop, or neither.",
    detail:
      "A daily bar that touched both counts as the stop, because daily bars cannot order two intraday touches and assuming the kind order would flatter the result. Theses that suggested no stop or target are not counted here at all.",
  },
  distinct_days: {
    term: "Distinct days",
    short:
      "How many separate trading days a bucket's samples come from. This matters more than the sample count.",
    detail:
      "Tickers scanned on the same day all share that day's market move, so 96 samples drawn from 2 days is closer to 2 observations than 96. A bucket is only treated as calibrated once it has breadth in time as well as in count.",
  },
  similar_setups: {
    term: "Similar setups",
    short:
      "Past setups whose indicator profile most closely matched this one, together with what actually happened to them. These are fed to the model before it writes a thesis.",
  },
  feature_vector: {
    term: "Feature vector",
    short:
      "The setup reduced to a fixed list of scaled numbers, so past setups can be compared against it mathematically.",
  },
  is_delayed_data: {
    term: "Delayed data",
    short:
      "This quote is not live. ASX and HK feeds reach this tool roughly 15 minutes late, so the price shown is where the stock was, not where it is.",
  },
  suggested_entry: {
    term: "Suggested entry",
    short:
      "Where the model would want to be filled, which is not always the current price — a setup can be worth waiting for a pullback to, or worth taking only once a level has broken. An entry shown as at market means the model read the setup as worth acting on at the last close. Advisory only — nothing here places or manages an order.",
  },
  suggested_stop: {
    term: "Suggested stop",
    short:
      "Where the model would abandon the idea. Advisory only — nothing here places or manages an order.",
  },
  suggested_target: {
    term: "Suggested target",
    short:
      "Where the model would take profit. Advisory only — nothing here places or manages an order.",
  },
  risk_reward: {
    term: "Risk / reward",
    short:
      "How far the target is versus how far the stop, from the same entry. Bigger is not automatically better — a very large ratio usually means the stop sits so close that ordinary noise would trigger it.",
  },
  // The three extended-hours sessions get their own entries because they are
  // NOT measured from the same starting price — verified across all 48 US
  // tickers. Saying so is the whole point: side by side they look
  // comparable, and subtracting one from another is meaningless.
  pre_market_change: {
    term: "Pre-market change",
    short:
      "How far the stock moved before the opening bell, measured from the previous session's close.",
  },
  after_hours_change: {
    term: "After-hours change",
    short:
      "How far the stock moved after the closing bell, measured from today's closing price — not from yesterday's.",
  },
  overnight_change: {
    term: "Overnight change",
    short:
      "How far the stock moved in the overnight session, measured from the previous session's close. A large overnight gap usually means news landed while the market was shut.",
  },
  // `session` is deliberately NOT surfaced by any <GlossaryTerm>. Rendering
  // it would mean deciding which session the user is in, and that means a
  // clock — see market_hours.session_of, whose HK midday break is unmodelled
  // and which CLAUDE.md says must not gain a conditional consumer. The
  // extended-hours UI keys off which values exist, never off the time. Left
  // defined because the setup card prints the stored string as raw text.
  session: {
    term: "Session",
    short:
      "Which part of the trading day the exchange was in when this was captured: pre-market, open, post-market or closed.",
  },
};


/* --- sector rotation ---------------------------------------------------- */

GLOSSARY.rotation_score = {
  term: "Rotation score",
  short: "How unusual a sector's move was, against every other sector the same day.",
  detail:
    "Computed in Python from the sector's own price and dollar volume — never by the model. " +
    "It combines the sector's return relative to the median sector, its volume against its own " +
    "normal, how consistently the move happened across sessions, and whether it is speeding up. " +
    "Runs from about -1 to +1. It is a dispersion measure, not a dollar amount: it says a sector " +
    "moved unusually, not how much money changed hands.",
};

GLOSSARY.rotation_baseline = {
  term: "the median sector",
  short: "The zero point: the middle sector's move that day, equal-weighted.",
  detail:
    "Every score is measured against the MIDDLE sector rather than against zero or against an " +
    "index. That is deliberate. On a day the whole market rises, an absolute measure would report " +
    "money flowing into all 145 industries at once — which is one market observation wearing 145 " +
    "hats. Measuring against the median sector means a positive score always signals a sector " +
    "outrunning its peers. It is not a cap-weighted market benchmark.",
};

GLOSSARY.turnover_thrust = {
  term: "Volume thrust",
  short: "This sector's dollar volume against its own recent normal.",
  detail:
    "Compared only to itself, never to other sectors: Semiconductors turns over about $117bn a " +
    "day and a niche sector $200m, so a cross-sector volume comparison would just rank sectors by " +
    "size forever. Signed by the direction the sector actually moved, since heavy volume is " +
    "evidence of conviction whichever way it went.",
};

GLOSSARY.sector_breadth = {
  term: "Breadth",
  short: "How many names inside the sector rose versus fell.",
  detail:
    "Advancers minus decliners over the total. A sector can rise on one huge constituent while " +
    "most of its names fall — breadth is what separates that from a broad move. Only available " +
    "for the single-session window, because it comes from a live snapshot rather than history.",
};

GLOSSARY.relative_return = {
  term: "vs median",
  short: "The sector's return minus the middle sector's return over the same window.",
  detail:
    "A sector up 1% on a day the median sector rose 3% is DOWN 2% on this measure, and that is " +
    "the honest reading — capital moved away from it relative to everywhere else it could have gone.",
};

GLOSSARY.persistence = {
  term: "Persistence",
  short: "Whether the move happened across the window's sessions, or on one of them.",
  detail:
    "Counts how many sessions in the window moved the same way the window as a whole did, " +
    "then scales that to run from -1 to +1. +1 means every session agreed. 0 means half did. " +
    "A NEGATIVE value is the interesting one: it means most sessions went the other way and a " +
    "single outsized day carried the whole move — which is a spike, not a reallocation. " +
    "Separating those two is the entire point of the component, since only one of them tends " +
    "to continue. Sessions flagged as a possible index rebase are left out of the count " +
    "entirely, so a bookkeeping jump cannot masquerade as a run of agreement.",
};

GLOSSARY.acceleration = {
  term: "Acceleration",
  short: "Whether the move is speeding up or fading, against the sector's own previous window.",
  detail:
    "Compares this window's move against the SAME sector's immediately preceding window of " +
    "equal length — the last five sessions against the five before them, say. Positive means " +
    "the move is gathering pace; negative means it is decaying even if the window is still " +
    "positive overall. It is measured backwards against an equal span rather than against a " +
    "longer window, which is what makes it defined even for the longest timeframe, where " +
    "there is no next window up to compare with.",
};

GLOSSARY.actual_move = {
  term: "Actual move vs median",
  short: "The real percentage move, before it is scaled into a score.",
  detail:
    "This and 'Return vs median' above are the same measurement at two stages, which is why " +
    "both are shown. This is the raw figure — how many percent the sector moved relative to " +
    "the middle sector over the window. 'Return vs median' is that same number compressed " +
    "into the -1 to +1 range the score is built from, scaled by how widely sectors moved " +
    "apart that day. So a +2% move counts for a great deal on a quiet day and rather less on " +
    "a volatile one, and this row is where you see what actually happened before that " +
    "adjustment.",
};

GLOSSARY.main_in_flow = {
  term: "Block-sized flow",
  short: "Net money in large orders, from the ETF that tracks this sector.",
  detail:
    "Moomoo splits order flow by size; this is the large-order half, which is a reasonable read " +
    "on institutional participation. It is NOT reported block trades, NOT 13F holdings and NOT " +
    "fund creations — it is the net of large buy and sell orders in the sector's tracking ETF. " +
    "Only about two dozen of the 262 sectors have a suitable ETF, which is why this is reported " +
    "beside the rotation score and never folded into it.",
};

GLOSSARY.rotation_pair = {
  term: "Rotation pair",
  short: "Two related sectors that moved in opposite directions over the same window.",
  detail:
    "A correlation, not a traced flow. Nothing available — not this broker's data, not even 13F " +
    "filings — can link a dollar leaving one sector to a dollar arriving in another, which is why " +
    "these are shown as a ranked list rather than a flow diagram. The pair is only shown when the " +
    "two sectors genuinely share constituents, so it is not merely two ends of the ranking.",
};

GLOSSARY.sector_constituents = {
  term: "constituents",
  short: "How many listed companies the sector contains.",
  detail:
    "Fetched a slice at a time because the data source is rate limited, so a sector may show no " +
    "count for the first few days. That means UNKNOWN, not empty — a sector without a count is " +
    "marked unconfirmed rather than being given a confident score.",
};

export function glossaryEntry(key: string): GlossaryEntry | undefined {
  return GLOSSARY[key];
}
