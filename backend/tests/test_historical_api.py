from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.market.labels import DUKASCOPY_SOURCE
from app.models.market import Candle


def test_sources_endpoint(client) -> None:
    res = client.get("/api/market/sources")
    assert res.status_code == 200
    body = res.json()
    assert body["live_locked"] is True
    names = {s["name"] for s in body["sources"]}
    assert "dukascopy" in names
    assert "synthetic" in names


def test_live_permanently_blocked(client) -> None:
    res = client.put("/api/settings", json={"trading_environment": "LIVE"})
    assert res.status_code == 403
    detail = res.json()["detail"].lower()
    assert "permanentemente" in detail or "desativada" in detail


def test_candles_dukascopy_empty_message(client) -> None:
    res = client.get("/api/market/candles?timeframe=M15&source=dukascopy")
    assert res.status_code == 200
    body = res.json()
    assert body["is_synthetic"] is False
    assert body["candles"] == []
    assert "Descarregue" in (body.get("message") or "") or "descarreg" in (body.get("message") or "").lower()


def test_replay_create_requires_data(client) -> None:
    res = client.post(
        "/api/replay",
        json={
            "start": "2025-10-13T00:00:00Z",
            "end": "2025-10-14T00:00:00Z",
            "timeframe": "M15",
            "speed": 1,
            "ai_every_n_bars": 12,
            "execute_trades": False,
        },
    )
    assert res.status_code == 400


def test_replay_flow_with_seeded_data(client, db) -> None:
    start = datetime(2025, 10, 13, 8, 0, tzinfo=timezone.utc)
    price = 1.08
    for i in range(40):
        ts = start + timedelta(minutes=15 * i)
        c = price + 0.00005
        db.add(
            Candle(
                symbol="EURUSD",
                timeframe="M15",
                ts=ts,
                open=price,
                high=max(price, c) + 0.0001,
                low=min(price, c) - 0.0001,
                close=c,
                volume=10,
                source=DUKASCOPY_SOURCE,
                is_synthetic=False,
            )
        )
        price = c
    db.commit()
    res = client.post(
        "/api/replay",
        json={
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": (start + timedelta(hours=10)).isoformat().replace("+00:00", "Z"),
            "timeframe": "M15",
            "speed": 20,
            "ai_every_n_bars": 50,
            "execute_trades": False,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["session"]["is_synthetic"] is False
    assert body["session"]["source"] == "dukascopy"
    sid = body["session"]["id"]
    play = client.post(f"/api/replay/{sid}/play")
    assert play.status_code == 200
    pause = client.post(f"/api/replay/{sid}/pause")
    assert pause.status_code == 200
    report = client.get(f"/api/replay/{sid}/report")
    assert report.status_code == 200
    assert "HISTÓRICOS REAIS" in report.json()["label"] or "HISTORICOS REAIS" in report.json()["label"].upper()
