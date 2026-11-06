"""Runtime configuration. All secrets come from the environment / .env only (never the DB, never logs)."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LIVE_CONFIRM_PHRASE = "I UNDERSTAND THIS WILL TRADE REAL MONEY"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- mode -----------------------------------------------------------------
    execution_mode: Literal["paper", "approval", "live"] = "paper"
    broker: Literal["paper", "oanda"] = "paper"

    # --- secrets (SecretStr: repr/str never reveal the value) ------------------
    webhook_secret: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("dev-only-change-me")
    oanda_api_key: SecretStr = SecretStr("")
    oanda_account_id: str = ""
    oanda_env: Literal["practice", "live"] = "practice"
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    fmp_api_key: SecretStr = SecretStr("")
    embed_api_key: SecretStr = SecretStr("")

    # --- storage ---------------------------------------------------------------
    database_url: str = "sqlite:///./data/app.db"
    data_dir: Path = Path("./data")
    raw_log_retention_days: int = 90

    # --- webhook ---------------------------------------------------------------
    freshness_sec: int = 60
    future_skew_sec: int = 10
    rate_limit_per_min: int = 60
    rate_limit_burst: int = 30
    trusted_proxy_ips: str = ""  # comma list; when behind cloudflared/caddy
    tv_ip_allowlist_enabled: bool = False
    tv_ips: str = "52.89.214.238,34.212.75.30,54.218.53.128,52.32.178.7"

    # --- account / risk (mirrors position-sizer, pre-trade-discipline-gate, drawdown-circuit-breaker,
    #     exposure-coach) -----------------------------------------------------------
    base_currency: str = "USD"
    account_size: Decimal = Decimal("2000")
    risk_per_trade_pct: Decimal = Decimal("0.5")
    max_daily_loss_pct: Decimal = Decimal("2.0")
    max_open_positions: int = 3
    max_exposure_pct: Decimal = Decimal("300")  # total notional / equity ceiling (leveraged CFD)
    revenge_block_min: int = 30
    max_entries_per_day: int = 6
    session_start_utc: str = "07:00"
    session_end_utc: str = "20:00"
    friday_no_entry_after_utc: str = "18:00"
    news_blackout_min: int = 30
    news_filter_enabled: bool = True
    max_price_deviation_pct: Decimal = Decimal("0.5")
    symbol_map: str = '{"XAUUSD": "XAU_USD"}'  # TradingView symbol -> broker symbol
    strategy_version: str = "v0.1"

    # --- paper broker ----------------------------------------------------------
    paper_slippage: Decimal = Decimal("0.10")  # USD per unit, adverse
    paper_spread: Decimal = Decimal("0.30")
    paper_fee_per_unit: Decimal = Decimal("0.05")
    paper_min_qty: Decimal = Decimal("0.01")
    paper_qty_step: Decimal = Decimal("0.01")
    paper_price_source: Literal["signal", "yfinance", "oanda"] = "signal"
    paper_yf_ticker: str = "GC=F"

    # --- broker resilience -------------------------------------------------------
    broker_timeout_sec: float = 10.0
    broker_max_retries: int = 4
    reconcile_interval_sec: int = 30
    price_stale_sec: int = 120

    # --- memory / learning -------------------------------------------------------
    embed_provider: Literal["ollama", "openai_compat", "hash"] = "ollama"
    embed_fallback: Literal["ollama", "none", "hash"] = "none"
    ollama_url: str = "http://localhost:11434"
    ollama_embed_model: str = "nomic-embed-text"
    embed_api_url: str = ""
    embed_api_model: str = "text-embedding-3-small"
    embed_dim: int = 768
    recall_top_k: int = 20
    caution_rule_enabled: bool = False
    caution_min_samples: int = 20
    caution_avg_r_threshold: Decimal = Decimal("-0.3")
    caution_action: Literal["halve", "approval"] = "halve"

    # --- dashboard ---------------------------------------------------------------
    dashboard_secure_cookies: bool = False
    login_max_attempts_per_15min: int = 10

    @property
    def symbol_mapping(self) -> dict[str, str]:
        return json.loads(self.symbol_map)

    def broker_symbol(self, tv_symbol: str) -> str:
        return self.symbol_mapping.get(tv_symbol.upper(), tv_symbol.upper())

    def risk_fingerprint(self) -> dict:
        """The risk/strategy config every trade is linked to (config_versions table)."""
        keys = ["strategy_version", "risk_per_trade_pct", "max_daily_loss_pct", "max_open_positions",
                "max_exposure_pct", "revenge_block_min", "max_entries_per_day", "session_start_utc",
                "session_end_utc", "friday_no_entry_after_utc", "news_blackout_min", "news_filter_enabled",
                "caution_rule_enabled", "caution_min_samples", "caution_avg_r_threshold", "caution_action",
                "execution_mode", "broker"]
        return {k: str(getattr(self, k)) for k in keys}

    def risk_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.risk_fingerprint(), sort_keys=True).encode()).hexdigest()[:16]

    # --- paths ---
    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def outbox_path(self) -> Path:
        return self.data_dir / "outbox.sqlite"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def paper_state_path(self) -> Path:
        return self.data_dir / "paper_broker.sqlite"

    @property
    def live_confirm_path(self) -> Path:
        return self.state_dir / "LIVE_CONFIRMED.json"


class LiveModeNotConfirmed(RuntimeError):
    pass


def assert_mode_allowed(s: Settings) -> None:
    """Live trading requires BOTH execution_mode=live in config AND a typed confirmation file
    written by `tradectl set-live` that matches the current config hash. Never auto-created."""
    live = s.execution_mode == "live" or (s.broker == "oanda" and s.oanda_env == "live")
    if not live:
        return
    if s.execution_mode != "live":
        raise LiveModeNotConfirmed("OANDA_ENV=live requires EXECUTION_MODE=live plus `tradectl set-live`.")
    p = s.live_confirm_path
    if not p.exists():
        raise LiveModeNotConfirmed("EXECUTION_MODE=live but no typed confirmation. Run `tradectl set-live`.")
    data = json.loads(p.read_text())
    if data.get("risk_hash") != s.risk_hash():
        raise LiveModeNotConfirmed("Config changed since live confirmation. Re-run `tradectl set-live`.")


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.data_dir, s.raw_dir, s.state_dir):
        Path(d).mkdir(parents=True, exist_ok=True)
    return s
