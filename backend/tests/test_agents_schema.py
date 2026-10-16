from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.agents.base import _extract_json
from app.schemas.agents import CoordinatorOutput, FundamentalOutput, TechnicalOutput


def test_technical_schema_requires_rationale() -> None:
    with pytest.raises(ValidationError):
        TechnicalOutput(timestamp_utc=datetime.now(timezone.utc).isoformat())


def test_technical_schema_accepts_valid_payload() -> None:
    out = TechnicalOutput(
        rationale="RSI is mid-range on supplied data.",
        timestamp_utc="2024-01-01T00:00:00+00:00",
        rsi_14=51.2,
        volatility_regime="normal",
    )
    assert out.recommendation == "HOLD"


def test_fundamental_defaults_do_not_invent_news() -> None:
    out = FundamentalOutput(
        rationale="No calendar source configured.",
        timestamp_utc="2024-01-01T00:00:00+00:00",
        insufficient_information=True,
        news_available=False,
        calendar_available=False,
    )
    assert out.news_available is False
    assert "fabricad" in out.disclaimer.lower()


def test_coordinator_hold_default() -> None:
    out = CoordinatorOutput(
        rationale="Conflicting reports.",
        timestamp_utc="2024-01-01T00:00:00+00:00",
        final_recommendation="HOLD",
    )
    assert out.final_recommendation == "HOLD"


def test_extract_json_from_fenced_block() -> None:
    data = _extract_json('```json\n{"bias": "NEUTRAL"}\n```')
    assert data["bias"] == "NEUTRAL"
