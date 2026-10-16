"""Database bootstrap: account, agent configs, synthetic candles."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.prompts import ALL_CONFIGURABLE_ROLES, CORE_AGENT_ROLES, DISPLAY_NAMES
from app.config import get_settings
from app.market.data_store import seed_synthetic_if_empty
from app.models.account import Account
from app.models.agent import AgentConfig


def seed_all(db: Session) -> dict[str, int]:
    settings = get_settings()
    created_account = 0
    if db.scalar(select(Account.id).limit(1)) is None:
        db.add(
            Account(
                name="SIMULATED",
                currency=settings.account_currency,
                environment=settings.trading_environment,
                starting_equity=settings.starting_equity,
                balance=settings.starting_equity,
                equity=settings.starting_equity,
                peak_equity=settings.starting_equity,
                is_simulated=True,
            )
        )
        created_account = 1

    created_agents = 0
    # Seis agentes do painel + papéis de debate Bull/Bear (internos).
    for role in ALL_CONFIGURABLE_ROLES:
        title = DISPLAY_NAMES[role]
        row = db.get(AgentConfig, role)
        if row is None:
            provider, model = settings.agent_assignment(role)
            db.add(
                AgentConfig(
                    role=role,
                    display_name=title,
                    provider=provider,
                    model=model,
                    enabled=True,
                    temperature=settings.ai_temperature,
                )
            )
            created_agents += 1
        elif row.display_name != title:
            row.display_name = title
    db.commit()
    candles = seed_synthetic_if_empty(db)
    return {
        "account": created_account,
        "agents": created_agents,
        "core_roles": len(CORE_AGENT_ROLES),
        "candles": candles,
    }
