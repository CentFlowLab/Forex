"""Contexto radar para OpenAI — origem MT5 e disclaimer factual (sem API paga)."""

from __future__ import annotations

from app.robot.radar import (
    RadarCandidate,
    RadarScan,
    _temporal_disclaimer,
    validate_scan_for_paid,
)


def _pair(symbol: str, *, source: str = "mt5", candle_source: str = "mt5") -> dict:
    return {
        "symbol": symbol,
        "display": symbol,
        "score": 1.0,
        "triage_reasons": [],
        "quote": {
            "mid": 1.1,
            "bid": 1.1,
            "ask": 1.1,
            "lag_seconds": 0.01,
            "update_age_s": 0.01,
            "source": source,
            "feed_state": "recente",
            "clock_anomaly": True,
            "claims_realtime": False,
            "data_class": "mt5_atualizacao_recente_relogio_inconsistente",
        },
        "candle_source": candle_source,
        "data_quality": {"data_ok": True, "executable": True},
        "executable": True,
    }


def test_temporal_disclaimer_mt5_not_dukascopy():
    pairs = [_pair(s) for s in ("EURUSD", "GBPUSD", "USDJPY")]
    text = _temporal_disclaimer(pairs)
    assert "MetaTrader 5" in text or "MT5" in text
    assert "Dukascopy bi5 histórico — não é tempo real" not in text
    assert "update_age" in text
    assert "anomalia" in text.lower() or "relógio" in text.lower()


def test_validate_scan_requires_mt5_quotes_and_candles():
    symbols = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
    good = [
        RadarCandidate(
            symbol=s,
            display=s,
            score=1.0,
            snapshot={
                "quote": _pair(s)["quote"],
                "candle_source": "mt5",
                "data_quality": {"data_ok": True, "executable": True},
                "candles_closed": {"M15": [], "H1": [], "H4": []},
                "candles_counts": {"M15": 40, "H1": 24, "H4": 16},
                "structure": {},
                "volatility": {},
                "trading_costs": {},
            },
        )
        for s in symbols
    ]
    scan = RadarScan(
        monitored=7,
        with_valid_data=7,
        candidates=good[:3],
        rejected=[],
        pairs=[],
        all_valid=good,
    )
    assert validate_scan_for_paid(scan)["ok"] is True

    bad = [
        RadarCandidate(
            symbol=s,
            display=s,
            score=1.0,
            snapshot={
                "quote": _pair(s, source="ultima_vela_m15_fechada")["quote"],
                "candle_source": "dukascopy",
                "data_quality": {"data_ok": True, "executable": False},
                "candles_closed": {"M15": [], "H1": [], "H4": []},
                "candles_counts": {"M15": 40, "H1": 24, "H4": 16},
                "structure": {},
                "volatility": {},
                "trading_costs": {},
            },
        )
        for s in symbols
    ]
    scan_bad = RadarScan(
        monitored=7,
        with_valid_data=7,
        candidates=bad[:3],
        rejected=[],
        pairs=[],
        all_valid=bad,
    )
    result = validate_scan_for_paid(scan_bad)
    assert result["ok"] is False
    assert "MT5" in result["reason"]


def test_authorization_label_no_pending_when_confirmed():
    from app.robot.openai_panel import authorization_label

    assert (
        authorization_label(
            {
                "confirmed": True,
                "active": True,
                "robot_running": True,
                "scheduling_active": True,
                "suspend_reason": "",
            }
        )
        == "OpenAI autónoma ativa"
    )
    assert (
        authorization_label({"confirmed": False, "active": False})
        == "A aguardar autorização"
    )
    assert "parado" in authorization_label(
        {
            "confirmed": True,
            "active": True,
            "robot_running": False,
            "scheduling_active": False,
            "suspend_reason": "",
        }
    ).lower()
