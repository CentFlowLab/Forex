"""Application settings loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BACKEND_DIR / "data"

TradingEnvironment = Literal["BACKTEST", "PAPER", "LIVE"]
AIProviderName = Literal["xai", "openai", "anthropic"]
AgentRole = Literal[
    "technical",
    "trend",
    "fundamental",
    "strategy",
    "risk_challenger",
    "coordinator",
    "bull_researcher",
    "bear_researcher",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "GROK FOREX AI"
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    frontend_origin: str = "http://127.0.0.1:5173"
    database_url: str = "sqlite:///./data/grokforex.db"
    log_level: str = "INFO"

    trading_environment: TradingEnvironment = "PAPER"
    live_trading_enabled: bool = False
    live_manual_authorization: str = ""
    # Hard lock for this development phase — LIVE cannot be enabled.
    live_trading_locked: bool = True

    ai_daily_budget_usd: float = 5.0
    ai_session_budget_usd: float = 2.0
    max_debate_rounds: int = 2
    max_analysis_duration_seconds: float = 180.0
    ai_max_retries: int = 3
    default_analysis_workflow: str = "tradingagents"
    dukascopy_max_days: int = 14
    # Descarregamentos longos do Motor Adaptativo (incremental, com confirmação).
    dukascopy_adaptive_max_days: int = 400
    dukascopy_hour_delay_seconds: float = 0.25
    dukascopy_hour_retries: int = 3

    # Ciclo autónomo (local, sem IA paga).
    autonomy_default_interval_minutes: int = 180
    autonomy_default_max_combos: int = 6


    # Controlo de custos — predefinição segura: IA DESLIGADA.
    ai_enabled: bool = False
    ai_require_manual_approval: bool = True
    ai_max_calls_per_analysis: int = 12
    ai_max_concurrent_analyses: int = 1
    ai_max_cost_per_analysis_usd: float = 0.50
    ai_block_unknown_price_models: bool = True
    # Replay/backtest/avaliação NÃO podem chamar IA sem autorização explícita.
    ai_allow_replay_without_auth: bool = False
    # Robô: análises OpenAI autónomas (observação — sem execução).
    robot_openai_interval_seconds: float = 900.0  # 15 min
    robot_openai_daily_budget_usd: float = 0.10
    # Segunda chamada paga para "reparar" qualidade — desligada por defeito.
    # Só com True explícito (autorização manual separada); nunca no ciclo autónomo normal.
    openai_allow_quality_repair_call: bool = False

    # Feed automático Dukascopy (TCP anónimo + fallback bi5 horário).
    # Não exige conta de corretora nem chave API. Não dispara análises OpenAI.
    dukascopy_quote_feed_enabled: bool = True
    dukascopy_tcp_enabled: bool = True
    dukascopy_tcp_host: str = "datafeed.dukascopy.com"
    dukascopy_tcp_port: int = 9999
    dukascopy_bi5_poll_seconds: int = 60
    dukascopy_bi5_lookback_hours: int = 8
    # Limiares de estado da cotação (segundos de atraso medido).
    quote_fresh_seconds: float = 45.0
    quote_delayed_seconds: float = 7200.0

    max_risk_per_trade_pct: float = 0.25
    max_daily_loss_pct: float = 2.0
    max_concurrent_positions: int = 1
    max_spread_pips: float = 2.0
    # Magic number / comentário das ordens do robô no MT5 DEMO.
    mt5_demo_magic: int = 26041501
    mt5_demo_comment: str = "GROK_FX"
    max_slippage_pips: float = 1.5
    kill_switch: bool = False
    stale_data_seconds: int = 30
    starting_equity: float = 10_000.0
    account_currency: str = "USD"

    sim_spread_pips: float = 1.2
    sim_commission_per_lot: float = 7.0
    sim_execution_delay_ms: int = 150
    sim_slippage_pips: float = 0.3

    xai_api_key: str = ""
    xai_api_base: str = "https://api.x.ai/v1"
    xai_default_model: str = "grok-4-latest"

    openai_api_key: str = ""
    openai_api_base: str = "https://api.openai.com/v1"
    openai_default_model: str = "gpt-4o-mini"

    anthropic_api_key: str = ""
    anthropic_api_base: str = "https://api.anthropic.com"
    anthropic_default_model: str = "claude-sonnet-4-20250514"

    agent_technical_model: str = ""
    agent_trend_model: str = ""
    agent_fundamental_model: str = ""
    agent_strategy_model: str = ""
    agent_risk_challenger_model: str = ""
    agent_coordinator_model: str = ""
    agent_bull_researcher_model: str = ""
    agent_bear_researcher_model: str = ""

    ai_request_timeout_seconds: float = 45.0
    ai_max_tokens: int = 1800
    ai_temperature: float = 0.2

    ig_api_key: str = ""
    ig_username: str = ""
    ig_password: str = ""
    ig_acc_type: str = "DEMO"
    ig_api_base: str = ""

    mt5_login: str = ""
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_path: str = ""
    # Feed de mercado MT5 (cotações apenas — nunca order_send).
    mt5_quote_feed_enabled: bool = True
    mt5_poll_seconds: float = 2.0

    news_api_key: str = ""
    news_api_base: str = ""

    @field_validator("trading_environment", mode="before")
    @classmethod
    def _normalize_env(cls, value: str) -> str:
        return str(value).upper()

    @property
    def live_authorized(self) -> bool:
        if self.live_trading_locked:
            return False
        return (
            self.live_trading_enabled
            and self.live_manual_authorization == "I_AUTHORIZE_LIVE_TRADING"
        )

    def provider_configured(self, provider: str) -> bool:
        key = {
            "xai": self.xai_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }.get(provider, "")
        return bool(key and key.strip())

    def agent_assignment(self, role: str) -> tuple[str, str]:
        raw = {
            "technical": self.agent_technical_model,
            "trend": self.agent_trend_model,
            "fundamental": self.agent_fundamental_model,
            "strategy": self.agent_strategy_model,
            "risk_challenger": self.agent_risk_challenger_model,
            "coordinator": self.agent_coordinator_model,
            "bull_researcher": self.agent_bull_researcher_model,
            "bear_researcher": self.agent_bear_researcher_model,
        }.get(role, "")
        if raw and ":" in raw:
            provider, model = raw.split(":", 1)
            return provider.strip().lower(), model.strip()
        if self.provider_configured("xai"):
            return "xai", self.xai_default_model
        if self.provider_configured("openai"):
            return "openai", self.openai_default_model
        if self.provider_configured("anthropic"):
            return "anthropic", self.anthropic_default_model
        return "openai", self.openai_default_model


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "uploads").mkdir(exist_ok=True)
    (DATA_DIR / "synthetic").mkdir(exist_ok=True)
    (DATA_DIR / "runtime").mkdir(exist_ok=True)
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
