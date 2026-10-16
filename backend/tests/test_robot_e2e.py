"""E2E: motor IA simulado + harness de teste. Sem OpenAI paga. LIVE bloqueado."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.brokers.factory import get_broker
from app.brokers.simulated import SimulatedBroker
from app.config import get_settings
from app.database import Base
from app.market.quote_feed import get_quote_feed
from app.market.quote_types import ValidatedTick
from app.models import account as _acc  # noqa: F401
from app.models import adaptive as _a  # noqa: F401
from app.models import autonomy as _u  # noqa: F401
from app.models import robot as _r  # noqa: F401
from app.models import trading as _t  # noqa: F401
from app.models.adaptive import AdaptiveStrategyState
from app.models.robot import AIDecisionLog
from app.models.trading import ClosedTrade, Position
from app.robot.ai_engine import analyze_simulated, estimate_single_analysis_usd
from app.robot.ai_schema import AITradeProposal
from app.robot.ai_validate import validate_ai_proposal
from app.robot.service import RobotService, RobotTestHarness
from app.services.seed import seed_all


@pytest.fixture()
def rdb() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = S()
    seed_all(s)
    yield s
    try:
        get_broker(s).clear_replay_quote()
    except Exception:
        pass
    try:
        get_robot = RobotService()
        get_robot._stop.set()
        get_robot._thread = None
    except Exception:
        pass
    s.close()


def _inject(mid: float, ts: datetime | None = None) -> None:
    now = ts or datetime.now(timezone.utc)
    get_quote_feed().inject_tick_for_tests(
        ValidatedTick(
            ts=now,
            bid=mid - 0.00006,
            ask=mid + 0.00006,
            mid=mid,
            spread_pips=1.2,
            channel="dukascopy_bi5",
            mid_only=False,
        )
    )


def test_e2e_synthetic_open_close_balance(rdb: Session) -> None:
    settings = get_settings()
    assert settings.live_trading_locked is True
    get_broker(rdb).clear_replay_quote()

    for row in list(rdb.scalars(select(AdaptiveStrategyState))):
        if row.status == "approved":
            row.status = "experimental"
    rdb.commit()

    robot = RobotService()
    robot._interval_seconds = 3600

    broker = get_broker(rdb)
    assert isinstance(broker, SimulatedBroker)
    broker.connect()
    start_equity = broker.account().equity

    mid = 1.10000
    now = datetime.now(timezone.utc)
    broker.set_replay_quote(mid, ts=now, is_synthetic=True, source="synthetic_e2e_test")
    _inject(mid, now)

    started = robot.start(rdb)
    assert started["security"]["live_trading_locked"] is True
    assert started["security"]["auto_paid_ai"] is False
    robot.stop(rdb)

    opened = robot.tick(
        rdb,
        test=RobotTestHarness(force_signal="BUY", allow_synthetic=True, label="estratégia_teste_isolada"),
    )
    assert opened.get("ok") is True
    assert opened.get("action") == "BUY"
    assert opened.get("is_test") is True
    assert str(opened.get("client_order_id", "")).startswith("TEST-")

    positions = list(rdb.scalars(select(Position)))
    assert len(positions) == 1
    pos = positions[0]
    assert pos.is_simulated is True
    take = float(pos.take_profit)

    _inject(take + 0.00020)
    broker.set_replay_quote(take + 0.00020, ts=datetime.now(timezone.utc), is_synthetic=True, source="synthetic_e2e_test")
    closed = robot.tick(
        rdb,
        test=RobotTestHarness(force_signal="HOLD", allow_synthetic=True),
    )
    assert closed.get("action") == "closed"
    assert len(list(rdb.scalars(select(Position)))) == 0

    trades = list(rdb.scalars(select(ClosedTrade)))
    assert len(trades) >= 1
    trade = trades[0]
    assert trade.is_simulated is True

    end_equity = get_broker(rdb).account().equity
    assert end_equity != start_equity or abs(trade.pnl) > 0 or trade.commission > 0

    stopped = robot.stop(rdb)
    assert stopped["running"] is False
    assert stopped["decision_pt"] == "AGUARDAR"


def test_ai_simulated_cycle_logs_decision(rdb: Session) -> None:
    """Percurso completo com IA simulada — sem custo OpenAI, sem exigir 7 estratégias."""
    robot = RobotService()
    robot._interval_seconds = 3600
    _inject(1.12136)
    broker = get_broker(rdb)
    assert isinstance(broker, SimulatedBroker)
    broker.connect()
    broker.set_replay_quote(1.12136, ts=datetime.now(timezone.utc), is_synthetic=False, source="bi5")

    before_logs = len(list(rdb.scalars(select(AIDecisionLog))))
    out = robot.tick(rdb)
    assert out.get("mode") == "simulated" or out.get("blocked") is True or out.get("action") in {
        "BUY",
        "SELL",
        "HOLD",
        "manage",
        "closed",
    }
    # Sem OpenAI: ciclo automático nunca marca modo openai
    st = robot.status(rdb)
    assert st["security"]["auto_paid_ai"] is False
    assert st["ai"]["mode"] in {"simulated", "openai"}
    # Se cotação válida, deve existir log de decisão
    if not out.get("blocked"):
        assert len(list(rdb.scalars(select(AIDecisionLog)))) >= before_logs + 1
        assert st["decision_pt"] in {"COMPRAR", "VENDER", "AGUARDAR"}
        assert st["last_justification"] or st["last_message"]


def test_ai_validate_rejects_inconsistent_buy(rdb: Session) -> None:
    ctx = {
        "quote": {
            "mid": 1.1,
            "spread_pips": 1.2,
            "block_entries": False,
            "claims_realtime": False,
            "is_synthetic": False,
        }
    }
    bad = AITradeProposal(
        decision="BUY",
        justification="teste",
        favoring_scenario="x",
        invalidating_scenario="y",
        entry_price=1.1,
        stop_loss=1.11,  # inválido para BUY
        take_profit=1.12,
        time_horizon="M15",
        risks="r",
        confidence=0.5,
    )
    v = validate_ai_proposal(rdb, bad, ctx, allow_delayed=True)
    assert v.approved is False


def test_ai_simulated_holds_on_stale_quote() -> None:
    prop = analyze_simulated(
        {
            "quote": {
                "mid": 1.1,
                "lag_seconds": 4000,
                "block_entries": False,
                "claims_realtime": False,
                "data_class": "historico_horario_atrasado",
                "status_label": "Com atraso",
            },
            "structure": {},
            "trading_costs": {"total_round_trip_usd": 2.5},
            "news_note": "Sem notícias.",
        }
    )
    assert prop.decision == "HOLD"


def test_estimate_single_analysis_no_api_call() -> None:
    est = estimate_single_analysis_usd()
    assert est["max_calls"] == 1
    assert est["estimated_cost_usd"] > 0
    assert "disclaimer" in est


def test_feed_bi5_not_claimed_realtime() -> None:
    from app.market.quote_types import QuoteFeedSnapshot

    snap = QuoteFeedSnapshot(
        connected=True,
        status="com_atraso",
        status_label="Com atraso",
        channel="dukascopy_bi5",
        source_label="bi5",
        mid=1.1,
        lag_seconds=1500,
        block_entries=False,
        data_class="historico_horario_atrasado",
        claims_realtime=False,
    )
    d = snap.as_dict()
    assert d["claims_realtime"] is False
    assert d["data_class"] == "historico_horario_atrasado"


def test_robot_api_start_stop_and_estimate(client) -> None:
    r = client.get("/api/robot/status")
    assert r.status_code == 200
    body = r.json()
    assert body["security"]["live_trading_locked"] is True
    assert body["security"]["auto_paid_ai"] is False

    est = client.get("/api/robot/ai/estimate")
    assert est.status_code == 200
    assert est.json()["max_calls"] == 1
    assert est.json()["estimated_cost_usd"] > 0

    start = client.post("/api/robot/start")
    assert start.status_code == 200
    assert start.json()["security"]["auto_paid_ai"] is False

    # Manual OpenAI sem token → 400
    manual = client.post("/api/robot/ai/manual", json={"confirmed": True})
    assert manual.status_code == 400

    stop = client.post("/api/robot/stop")
    assert stop.status_code == 200
    assert stop.json()["running"] is False
