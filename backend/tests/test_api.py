from __future__ import annotations


def test_health(client) -> None:
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["live_authorized"] is False


def test_dashboard(client) -> None:
    res = client.get("/api/dashboard")
    assert res.status_code == 200
    body = res.json()
    assert body["is_simulated"] is True
    assert "sintét" in body["disclaimer"].lower() or "sintet" in body["disclaimer"].lower()
    assert "account" in body


def test_market_snapshot_labeled(client) -> None:
    """Snapshot rotulado — sintético no seed, ou MT5/Dukascopy se o host os preferir."""
    res = client.get("/api/market/snapshot")
    assert res.status_code == 200
    body = res.json()
    label = str(body.get("label") or "").upper()
    assert label, "snapshot deve expor label de proveniência"
    bars = (body.get("bars") or {}).get("M15") or []
    # Com MT5 ativo a resolução pode apontar para fonte sem velas na BD de teste.
    if bars:
        if body.get("is_synthetic") is True:
            assert "SINTÉTICO" in label or "SINTETICO" in label
        else:
            assert "SINTÉTICO" not in label.replace("É", "E")
    else:
        assert body.get("data_source") or body.get("source_resolution")
        assert body.get("is_synthetic") in {True, False}


def test_settings_lists_providers(client) -> None:
    res = client.get("/api/settings")
    assert res.status_code == 200
    body = res.json()
    names = {p["name"] for p in body["providers"]}
    assert names == {"xai", "openai", "anthropic"}
    assert len(body["agents"]) == 8  # 6 principais + Bull/Bear de debate
    for p in body["providers"]:
        if not p["configured"]:
            assert p["status"] == "disabled"


def test_live_environment_blocked(client) -> None:
    res = client.put("/api/settings", json={"trading_environment": "LIVE"})
    assert res.status_code == 403
    detail = res.json()["detail"].lower()
    assert "desativada" in detail or "permanentemente" in detail


def test_kill_switch_endpoint(client) -> None:
    res = client.post("/api/kill-switch", params={"active": True})
    assert res.status_code == 200
    assert res.json()["kill_switch"] is True


def test_agent_update_validation(client) -> None:
    res = client.put(
        "/api/settings/agents/technical",
        json={"provider": "not-a-vendor", "model": "x", "enabled": True, "temperature": 0.2},
    )
    assert res.status_code == 400
