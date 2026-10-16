"""Investigação OOS de seguimento de tendência H1. Apenas leitura, sem ordens nem IA.

Executar na pasta backend:
    python -m app.robot.trend_following_research

Modelo: EMA50/200, breakout Donchian20, stop 2*ATR14, trailing 3*ATR14.
Entradas na abertura seguinte, com spread e slippage conservadores.
Sem operações sobrepostas no mesmo par. Sem ajuste de parâmetros no teste.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from app.market.mt5_history import fetch_symbol_history
from app.robot.universe import FOREX_28_SYMBOLS
from app.config import get_settings

OUTPUT = Path(__file__).resolve().parents[2] / "data" / "backtests" / "trend_following_oos.json"


def ema(values, length):
    out = [None] * len(values)
    if len(values) < length:
        return out
    seed = sum(values[:length]) / length
    out[length - 1] = seed
    k = 2.0 / (length + 1.0)
    for i in range(length, len(values)):
        seed += k * (values[i] - seed)
        out[i] = seed
    return out


def atr14(high, low, close):
    tr = [high[0] - low[0]]
    for i in range(1, len(close)):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1])))
    out = [None] * len(close)
    if len(close) < 14:
        return out
    a = sum(tr[:14]) / 14
    out[13] = a
    for i in range(14, len(close)):
        a = (a * 13 + tr[i]) / 14
        out[i] = a
    return out


def simulate(bars, *, pip_size, spread_pips, slip_pips):
    """Opera apenas sobre velas H1 fechadas. Saídas intrabar pessimistas."""
    c = [float(b["c"]) for b in bars]
    h = [float(b["h"]) for b in bars]
    l = [float(b["l"]) for b in bars]
    o = [float(b["o"]) for b in bars]
    e50, e200, a14 = ema(c, 50), ema(c, 200), atr14(h, l, c)
    trades = []
    i = 201
    while i < len(c) - 1:
        if a14[i] is None or e200[i] is None:
            i += 1
            continue
        up = e50[i] > e200[i] and c[i] > max(h[i-20:i])
        down = e50[i] < e200[i] and c[i] < min(l[i-20:i])
        if not (up or down):
            i += 1
            continue
        side = 1 if up else -1
        entry_i = i + 1
        # Custos aplicados na entrada e saída; sem fills perfeitos garantidos.
        entry_mid = o[entry_i]
        stop_distance = 2 * a14[i]
        stop = entry_mid - side * stop_distance
        best = entry_mid
        exit_i = None
        exit_mid = None
        reason = "end"
        for j in range(entry_i, min(entry_i + 240, len(c))):
            # Stop existente no início da vela; gap executado ao preço pior.
            if side == 1 and l[j] <= stop:
                exit_mid = min(stop, o[j])
                exit_i, reason = j, "stop"
                break
            if side == -1 and h[j] >= stop:
                exit_mid = max(stop, o[j])
                exit_i, reason = j, "stop"
                break
            best = max(best, h[j]) if side == 1 else min(best, l[j])
            # Trailing só se torna efetivo na vela seguinte.
            stop = max(stop, best - 3*a14[i]) if side == 1 else min(stop, best + 3*a14[i])
        if exit_i is None:
            exit_i = min(entry_i + 239, len(c) - 1)
            exit_mid = c[exit_i]
            reason = "timeout"
        gross_pips = side * (exit_mid - entry_mid) / pip_size
        net_pips = gross_pips - spread_pips - 2*slip_pips
        trades.append({"entry_bar": entry_i, "exit_bar": exit_i, "side": "BUY" if up else "SELL",
                       "gross_pips": round(gross_pips, 3), "net_pips": round(net_pips, 3),
                       "r_net": round(net_pips / (stop_distance / pip_size), 5), "exit": reason})
        i = exit_i + 1
    return trades


def stats(trades):
    pnl = [t["r_net"] for t in trades]
    wins = sum(x for x in pnl if x > 0)
    losses = -sum(x for x in pnl if x < 0)
    eq = peak = dd = 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": len(pnl), "net_r": round(sum(pnl), 3),
            "profit_factor": round(wins / losses, 3) if losses else None,
            "win_rate": round(sum(x > 0 for x in pnl) / len(pnl), 4) if pnl else None,
            "max_drawdown_r": round(dd, 3)}


def main():
    settings = get_settings()
    slip = max(0.0, float(settings.sim_slippage_pips))
    results = []
    for symbol in FOREX_28_SYMBOLS:
        print("A analisar", symbol, flush=True)
        try:
            hist = fetch_symbol_history(symbol, force=False)
            series = hist.by_tf.get("H1")
            if series is None or series.status != "ok" or len(series.bars) < 900:
                results.append({"symbol": symbol, "ok": False, "reason": "histórico H1 insuficiente"})
                continue
            bars = sorted(series.bars, key=lambda b: str(b.get("t", b.get("time", ""))))
            info = hist.symbol_info or {}
            # Evitar spread zero: fallback conservador se não houver bid/ask.
            from app.risk.instruments import get_instrument
            pip_size = float(get_instrument(symbol).pip_size)
            spread = 2.0
            if info.get("bid") and info.get("ask"):
                spread = max(1.0, (float(info["ask"]) - float(info["bid"])) / pip_size)
            n = len(bars)
            cuts = [0, int(n*0.6), int(n*0.8), n]
            # Recalcular indicadores com contexto anterior em cada período, mas
            # descartar entradas anteriores ao início do período.
            parts = {}
            for label, lo, hi in zip(("train", "validation", "test"), cuts[:-1], cuts[1:]):
                start = max(0, lo - 250)
                part = simulate(bars[start:hi], pip_size=pip_size, spread_pips=spread, slip_pips=slip)
                part = [t for t in part if t["entry_bar"] + start >= lo]
                parts[label] = stats(part)
            results.append({"symbol": symbol, "ok": True, "spread_pips_est": round(spread, 3),
                            "periods": parts})
        except Exception as exc:
            results.append({"symbol": symbol, "ok": False, "reason": str(exc)})
    report = {"created_utc": datetime.now(timezone.utc).isoformat(),
              "strategy": "EMA50/200 + Donchian20 H1 + ATR stop/trail",
              "status": "research_only_no_orders",
              "limits": ["Custos aproximados com spread atual, não histórico",
                         "Sem swaps/comissões overnight adicionais",
                         "Sinais H1, fills OHLC aproximados; sem ticks",
                         "Risco de gap/slippage real superior",
                         "Resultados por par não representam carteira com capital partilhado"],
              "results": results}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("RELATORIO:", OUTPUT)
    for label in ("train", "validation", "test"):
        r = [x["periods"][label] for x in results if x.get("ok")]
        print(label.upper(), "pares=", len(r), "trades=", sum(x["n"] for x in r),
              "net_R=", round(sum(x["net_r"] for x in r), 2),
              "pares_positivos=", sum(x["net_r"] > 0 for x in r))
    print("Sem ordens MT5. Sem chamadas pagas de IA.")


if __name__ == "__main__":
    main()
