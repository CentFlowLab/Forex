"""Testa os mesmos endpoints que o painel Robot (sem OpenAI pago, sem ordens reais)."""

from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:8000/api"


def main() -> int:
    out: dict = {}
    with httpx.Client(timeout=90.0) as c:
        out["stop"] = c.post(f"{BASE}/robot/stop").status_code
        r = c.post(f"{BASE}/robot/start")
        out["start_code"] = r.status_code
        s = r.json()
        out["start"] = {
            "running": s.get("running"),
            "radar": f"{s.get('radar', {}).get('with_valid_data')}/{s.get('radar', {}).get('monitored')}",
            "primary_source": (s.get("radar") or {}).get("primary_source"),
            "quote_source": (s.get("quote") or {}).get("source"),
            "feed_state": (s.get("quote") or {}).get("feed_state"),
            "claims_realtime": (s.get("quote") or {}).get("claims_realtime"),
            "clock_anomaly": (s.get("quote") or {}).get("clock_anomaly"),
            "age": (s.get("quote") or {}).get("age_seconds"),
            "mid": (s.get("quote") or {}).get("mid"),
            "block": s.get("block_reason") or "",
            "mt5_connected": (s.get("mt5") or {}).get("terminal_connected"),
            "duk_blocks": (s.get("dukascopy_complementary") or {}).get("blocks_robot"),
            "session_pnl": (s.get("capital") or {}).get("session_realized_pnl"),
            "baseline_pnl": (s.get("capital") or {}).get("baseline_realized_pnl"),
            "trades_n": len(s.get("trades") or []),
            "baseline_trades_n": len(s.get("baseline_trades") or []),
            "ai_mode": (s.get("ai") or {}).get("mode"),
            "ai_context_source": (s.get("ai") or {}).get("context_source"),
            "decision": s.get("decision"),
            "pairs": [
                {
                    "symbol": p.get("symbol"),
                    "source": p.get("source"),
                    "state": p.get("state"),
                    "feed_state": p.get("feed_state"),
                    "data_ok": p.get("data_ok"),
                }
                for p in ((s.get("radar") or {}).get("pairs") or [])
            ],
        }
        st = c.get(f"{BASE}/robot/status").json()
        out["status"] = {
            "running": st.get("running"),
            "radar": f"{st.get('radar', {}).get('with_valid_data')}/{st.get('radar', {}).get('monitored')}",
            "quote_source": (st.get("quote") or {}).get("source"),
            "block": st.get("block_reason") or "",
            "security": st.get("security"),
        }
        cd = c.get(f"{BASE}/market/candles", params={"timeframe": "M15", "limit": 40}).json()
        candles = cd.get("candles") or []
        out["candles"] = {
            "source": cd.get("source"),
            "label": (cd.get("label") or "")[:100],
            "n": len(candles),
            "note": cd.get("note"),
            "last_close": candles[-1].get("close") if candles else None,
        }
        ok = (
            out["start"].get("running") is True
            and out["start"].get("radar") == "7/7"
            and out["start"].get("quote_source") == "mt5"
            and out["start"].get("duk_blocks") is False
            and out["candles"].get("source") == "mt5"
            and out["candles"].get("n", 0) >= 10
            and (out["status"].get("security") or {}).get("live_trading_locked") is True
            and (out["status"].get("security") or {}).get("auto_paid_ai") is False
        )
        out["acceptance_ok"] = ok
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0 if out.get("acceptance_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
