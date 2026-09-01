"""
config.py — centralized settings, loaded from environment variables / .env.

Import `settings` (a module-level singleton) everywhere instead of
reading os.environ directly, so every setting has one documented
source of truth and a validated type.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchored to backend/ rather than the process CWD: systemd, uvicorn --reload
# and `python -m tests.x` all start from different directories, and a relative
# env_file silently loads nothing when the CWD isn't backend/.
BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Deployment mode ---
    #
    # This codebase now serves two deployments: the single-user self-hosted
    # box (Moomoo, SQLite, one owner) and the Informed Trader cloud offering
    # (Twelve Data, Postgres tenancy, many users).
    #
    # It defaults to self_hosted so that a box with no DEPLOYMENT_MODE line in
    # its .env behaves in every respect exactly as it did before this setting
    # existed. Every cloud-only field below is declared but unread under
    # self_hosted, so an absent value is never an error here.
    deployment_mode: Literal["self_hosted", "cloud"] = "self_hosted"

    # --- Cloud-only ---------------------------------------------------------
    # Read only when deployment_mode == "cloud". They are declared in core/ so
    # that there is ONE settings object rather than two that can drift, but
    # nothing in core/ consumes them.
    #
    # Empty strings rather than None: an unset credential must fail as
    # "not configured" at the point of use, not as an AttributeError at import.
    cloud_database_url: str = ""
    twelve_data_api_key: str = ""
    # Twelve Data's free tier allows 800 API credits per day. This is the
    # actual ceiling, not headroom: at roughly two credits per ticker-scan
    # across four session scans a day it is about 100 distinct tickers for the
    # whole platform, shared across every user, which is why the cloud provider
    # caches by ticker rather than by user.
    twelve_data_daily_credits: int = 800

    # --- Moomoo / OpenD ---
    opend_host: str = "127.0.0.1"
    opend_port: int = 11111

    # --- Moomoo trade context (READ-ONLY position queries) ---
    # These exist solely so the dashboard can show "you hold this". There is
    # no password/unlock setting here and there must never be one: unlocking
    # is what enables order placement, and this project never places orders.
    #
    # Every value below was confirmed against the live account, not guessed:
    #   trd_security_firm — must be FUTUAU for this account. A wrong firm does
    #     NOT error: FUTUINC/FUTUSECURITIES return RET_OK with only the
    #     SIMULATE account, so positions come back empty and look like "you
    #     hold nothing" rather than like a misconfiguration.
    #   trd_market — the context's `filter_trdmarket`, which filters which
    #     ACCOUNTS are listed, not which positions come back. 'AU' is not
    #     usable here: it fails with "the type of environment param is wrong".
    #     Per-market position filtering uses `position_market` instead.
    trd_env: str = "REAL"
    trd_market: str = "US"
    trd_security_firm: str = "FUTUAU"
    # Pin an account instead of auto-resolving the first REAL one from
    # get_acc_list(). Leave unset unless the account ever holds more than one.
    trd_acc_id: int | None = None

    # --- Ollama ---
    ollama_base_url: str = "http://192.168.68.49:11434/v1"
    ollama_model: str = "deepseek-r1:32b"

    # --- Database ---
    trading_db_path: str = str(BACKEND_DIR / "data" / "trading.db")

    # --- Scanner ---
    # Legacy. This drove the 60-second continuous rotation, which was
    # replaced by session-boundary scans plus a gap-filler; nothing reads it
    # now except `start_scheduler`'s explicit override, which the tests use
    # to keep the gap-filler from firing. Left declared so an existing
    # SCAN_INTERVAL_SECONDS in .env is not a surprise, and because
    # `extra="ignore"` would otherwise silently swallow it either way.
    scan_interval_seconds: int = 60

    # How often the gap-filler looks for tickers the session scan missed.
    # It normally finds none and does nothing.
    gap_filler_minutes: int = 30

    # How long before each trading session the full-watchlist scan starts.
    # A full US watchlist is ~48 tickers at 27-90s each, so this is a start
    # offset, not a guarantee it finishes before the session opens.
    #
    # Read through `session_lead_minutes`. The old name is kept as the
    # storage field so an existing PREMARKET_LEAD_MINUTES in .env keeps
    # working — renaming the env var would silently revert a tuned value to
    # the default, which is the kind of change nobody notices.
    premarket_lead_minutes: int = 45

    @property
    def session_lead_minutes(self) -> int:
        """Minutes before a session opens that its scan starts."""
        return self.premarket_lead_minutes

    # --- News ---
    # Feeds themselves live in services/news_feeds.py, not here: 16 entries
    # each with a category, label and icon do not fit str env fields, and
    # pydantic-settings JSON-decodes complex types before validators run
    # (see the cors_origins note below).
    #
    # SEC EDGAR 403s without a User-Agent carrying contact details. Probed
    # working as: trading-analyzer/1.0 (personal research; <email>).
    news_contact_email: str = ""
    news_refresh_minutes: int = 15
    news_retention_days: int = 30
    # Per-ticker feeds are rotated rather than fetched for all 45 tickers
    # every cycle — 45 x 4/hour would be 180 requests an hour to one host.
    news_ticker_batch: int = 10
    # Comma-separated feed keys to skip. A str + property for the same reason
    # as cors_origins.
    news_feeds_disabled: str = ""

    # --- App ---
    log_level: str = "INFO"

    # Browser origins allowed to call this API. Comma-separated, and a str
    # rather than list[str] deliberately: pydantic-settings JSON-decodes
    # complex types from the environment BEFORE field validators run, so a
    # plain comma-separated CORS_ORIGINS in .env raises SettingsError rather
    # than parsing. Read it through `cors_origin_list`.
    #
    # The LAN origin matters: the frontend is served from the LXC's LAN
    # address when browsed from another machine, and a request from
    # http://192.168.68.107:3000 is cross-origin even though it is the same
    # host as the backend — different port is enough.
    cors_origins: str = (
        "http://localhost:3000,http://127.0.0.1:3000,http://192.168.68.107:3000"
    )

    # Address uvicorn binds. 127.0.0.1 since the same-origin proxy landed:
    # the browser now reaches the API at /api on the Next.js origin, so
    # nothing outside this box has any reason to connect to :8000 directly.
    # This narrows the surface that used to be open to the whole LAN.
    #
    # NOTE: the systemd unit's --host flag is what actually binds; this value
    # is not read by uvicorn. Change both or neither.
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # Shared secret required in the X-API-Key header. Empty disables the
    # check entirely (and logs a warning) rather than locking the owner out
    # of their own dashboard over a missing .env line.
    #
    # This is NO LONGER shipped to the browser. The frontend used to inline it
    # via NEXT_PUBLIC_API_TOKEN, which published it to anyone who could load
    # the page; the session cookie replaced that. The header now exists only
    # for non-browser callers on localhost — scripts/benchmark_models.py, curl
    # and the like. See app/auth.py and services/auth_service.py.
    api_token: str = ""

    # --- Position alerts ---
    #
    # Thresholds live here rather than as literals in alerts.py so they can be
    # tuned against a real account without a code change. They are deliberately
    # blunt: there is no volatility model in this project, and scaling a
    # drawdown threshold by some invented per-ticker factor would be a computed
    # number pretending to be risk management (rule #1's spirit).
    alerts_drawdown_warn_pct: float = -8.0
    alerts_drawdown_critical_pct: float = -15.0
    alerts_earnings_warn_days: int = 3
    # A stop from a two-week-old thesis is not a level anyone is trading, so
    # the thesis-derived rules ignore setups older than this.
    alerts_setup_stale_days: int = 7
    alerts_shock_window_hours: int = 24
    alerts_contradiction_min_conviction: int = 7
    # Roughly the account's position count. Past that it stops being a glance.
    alerts_max_rendered: int = 6
    # Acks expire, so a fact that is still true tomorrow is raised again.
    # Critical expires sooner because it matters more, not less.
    alerts_ack_ttl_hours: float = 72.0
    alerts_ack_ttl_critical_hours: float = 12.0

    # --- Authentication ---
    #
    # Credentials live in .env rather than in trading.db on purpose: the DB is
    # snapshotted hourly by the backup timer, and a password hash does not
    # belong in fourteen days of rolling backups. Generate both with
    # `python scripts/set_password.py`.
    #
    # Empty auth_password_hash means login is IMPOSSIBLE, not open — the
    # opposite of api_token's empty-means-off rule, and deliberately so. That
    # rule exists because locking the owner out of a LAN-only box over a
    # missing .env line was the worse failure; on an internet-facing login
    # endpoint, failing open is not a trade-off worth making.
    auth_password_hash: str = ""
    auth_totp_secret: str = ""

    auth_cookie_name: str = "ta_session"
    auth_session_days: float = 30.0
    # Secure cookies require HTTPS. Only turn this off to debug over plain
    # HTTP on the LAN, and turn it back on — it is what stops the session
    # cookie being sent in cleartext.
    auth_cookie_secure: bool = True

    # Per-IP lockout. Deliberately modest numbers: this guards one human's
    # single account, so there is no legitimate reason to fail five times.
    auth_max_login_failures: int = 5
    auth_login_window_minutes: float = 15.0

    # --- Web Push ---
    #
    # Generate with `python scripts/generate_vapid.py`. The PUBLIC key is meant
    # to be public and is the one value that legitimately belongs in a
    # NEXT_PUBLIC_* variable — unlike the API token that used to live there.
    # vapid_subject must be a mailto: or https: URL identifying the sender;
    # push services reject a JWT without one.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = ""

    # How often the push job re-derives alerts. Shorter than the trade
    # gateway's 45s position cache would just miss the cache every time.
    push_check_minutes: float = 5.0
    # 'info' alerts are context, not events worth waking a phone for.
    push_min_severity: str = "warn"
    # Consecutive non-410 failures before an endpoint is dropped. A 404/410 is
    # handled separately and deletes immediately — that is the push service
    # saying the subscription is gone, not a transient error.
    push_max_failures: int = 5
    push_retention_days: int = 30

    @property
    def cors_origin_list(self) -> list[str]:
        """Allowed browser origins, trailing slashes stripped."""
        return [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]

    @property
    def news_feeds_disabled_list(self) -> list[str]:
        return [k.strip() for k in self.news_feeds_disabled.split(",") if k.strip()]

    @property
    def news_user_agent(self) -> str:
        """SEC EDGAR requires a declared UA with contact info, or it 403s."""
        contact = self.news_contact_email.strip()
        who = f"personal research; {contact}" if contact else "personal research"
        return f"trading-analyzer/1.0 ({who})"


settings = Settings()
