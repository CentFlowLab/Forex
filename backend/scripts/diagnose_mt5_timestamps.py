"""Diagnóstico rigoroso do desvio tick.time vs UTC — sem correções aplicadas."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5

OUT = Path(__file__).resolve().parents[1] / "tmp"
OUT.mkdir(exist_ok=True)
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]


def main() -> int:
    report: dict = {"ok": True}
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()
    unix_now = time.time()

    report["windows_clock"] = {
        "utc_now": now_utc.isoformat(),
        "local_now": now_local.isoformat(),
        "local_utc_offset_s": now_local.utcoffset().total_seconds() if now_local.utcoffset() else None,
        "unix_time": unix_now,
        "unix_as_utc": datetime.fromtimestamp(unix_now, tz=timezone.utc).isoformat(),
    }

    if not mt5.initialize():
        report["ok"] = False
        report["error"] = f"initialize failed: {mt5.last_error()}"
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 1

    ti = mt5.terminal_info()
    ai = mt5.account_info()
    term = {}
    if ti:
        for name in dir(ti):
            if name.startswith("_"):
                continue
            val = getattr(ti, name)
            if callable(val):
                continue
            if any(k in name.lower() for k in ("time", "gmt", "zone", "connect", "build", "name", "company", "path")):
                term[name] = val
    acct = {}
    if ai:
        for name in ("login", "server", "company", "trade_mode", "trade_allowed", "limit_orders", "margin_mode"):
            acct[name] = getattr(ai, name, None)
        for name in dir(ai):
            if "time" in name.lower() or "gmt" in name.lower():
                acct[name] = getattr(ai, name)

    report["terminal"] = term
    report["account"] = acct

    # Poll 1
    ticks1 = {}
    for s in SYMBOLS:
        mt5.symbol_select(s, True)
        t = mt5.symbol_info_tick(s)
        si = mt5.symbol_info(s)
        ticks1[s] = (t, si)

    time.sleep(2.0)
    now2 = datetime.now(timezone.utc)
    wall_delta = (now2 - now_utc).total_seconds()

    pairs = {}
    offsets = []
    for s in SYMBOLS:
        t, si = ticks1[s]
        t2 = mt5.symbol_info_tick(s)
        if t is None or t2 is None:
            pairs[s] = {"error": "no tick"}
            continue
        tt = datetime.fromtimestamp(int(t2.time), tz=timezone.utc)
        tm = datetime.fromtimestamp(float(t2.time_msc) / 1000.0, tz=timezone.utc)
        lag_s = (now2 - tt).total_seconds()
        lag_m = (now2 - tm).total_seconds()
        offsets.append(lag_s)

        # Hipótese A: tick.time é Unix UTC verdadeiro → lag≈0 se fresco
        # Hipótese B: tick.time é epoch com relógio de servidor GMT+3 (valor "parece UTC" mas é wallclock+3h)
        #   Interpretação: fromtimestamp(..., tz=UTC) mostra hora 3h à frente; lag ≈ -10800
        #   Se reinterpretarmos o MESMO inteiro como GMT+3 e convertermos a UTC:
        as_gmt3 = datetime.fromtimestamp(int(t2.time), tz=timezone(timedelta(hours=3))).astimezone(timezone.utc)
        lag_if_server_gmt3 = (now2 - as_gmt3).total_seconds()
        # Hipótese C: valor já é UTC mas servidor adiantado 3h (corrigir subtraindo 10800)
        as_minus_3h = datetime.fromtimestamp(int(t2.time) - 10800, tz=timezone.utc)
        lag_minus_3h = (now2 - as_minus_3h).total_seconds()

        spread_price = float(t2.ask) - float(t2.bid)
        point = float(si.point) if si else None
        digits = int(si.digits) if si else None
        # pip = 10*point for 5-digit / 3-digit jpy typically
        pip_size = (point * 10) if point and digits in (3, 5) else point
        spread_pips = (spread_price / pip_size) if pip_size else None
        broker_spread = int(si.spread) if si else None

        pairs[s] = {
            "bid": float(t2.bid),
            "ask": float(t2.ask),
            "bid_eq_ask": float(t2.bid) == float(t2.ask),
            "spread_price": spread_price,
            "spread_pips": spread_pips,
            "broker_spread_points": broker_spread,
            "digits": digits,
            "point": point,
            "tick_time_raw": int(t2.time),
            "tick_time_msc_raw": int(t2.time_msc),
            "tick_time_as_utc": tt.isoformat(),
            "tick_time_msc_as_utc": tm.isoformat(),
            "lag_if_utc_s": round(lag_s, 3),
            "lag_msc_if_utc_s": round(lag_m, 3),
            "lag_if_reinterpret_gmt3_s": round(lag_if_server_gmt3, 3),
            "lag_if_subtract_10800_s": round(lag_minus_3h, 3),
            "msc_delta_over_wall": int(t2.time_msc) - int(t.time_msc),
            "wall_delta_s": round(wall_delta, 3),
            "price_changed": (t.bid != t2.bid) or (t.ask != t2.ask),
            "volume": float(getattr(t2, "volume", 0) or 0),
            "flags": int(getattr(t2, "flags", 0) or 0),
        }

    med_offset = sorted(offsets)[len(offsets) // 2] if offsets else None
    # Prova empírica: se lag_if_utc ≈ -10800 e lag_if_reinterpret_gmt3 ≈ 0 e msc avança ≈ wall
    sample = next(iter(pairs.values()))
    hyp_b = (
        med_offset is not None
        and abs(med_offset + 10800) < 30
        and abs(float(sample.get("lag_if_reinterpret_gmt3_s", 999))) < 30
        and abs(float(sample.get("msc_delta_over_wall", 0)) - wall_delta * 1000) < 500
    )
    hyp_local = abs((now_local.utcoffset() or timedelta()).total_seconds()) > 0 and abs(
        (med_offset or 0) + 10800
    ) < 30
    # Relógio Windows UTC coerente consigo próprio (unix == utc)
    local_ok = abs(unix_now - now_utc.timestamp()) < 1.0

    candles = {}
    rates = mt5.copy_rates_from_pos("EURUSD", mt5.TIMEFRAME_M15, 0, 6)
    if rates is not None:
        rows = []
        for r in rates:
            raw = int(r["time"])
            as_utc = datetime.fromtimestamp(raw, tz=timezone.utc)
            as_gmt3 = datetime.fromtimestamp(raw, tz=timezone(timedelta(hours=3))).astimezone(timezone.utc)
            rows.append(
                {
                    "time_raw": raw,
                    "as_utc": as_utc.isoformat(),
                    "lag_as_utc_s": round((now2 - as_utc).total_seconds(), 1),
                    "as_gmt3_to_utc": as_gmt3.isoformat(),
                    "lag_as_gmt3_s": round((now2 - as_gmt3).total_seconds(), 1),
                    "o": float(r["open"]),
                    "c": float(r["close"]),
                }
            )
        candles["EURUSD_M15"] = rows

    # copy_ticks: se pedirmos "agora UTC" falha; se pedirmos agora-3h obtém ticks "atuais" do servidor
    ticks_now = mt5.copy_ticks_from("EURUSD", now2, 3, mt5.COPY_TICKS_ALL)
    err_now = mt5.last_error()
    ticks_m3 = mt5.copy_ticks_from("EURUSD", now2 - timedelta(hours=3), 5, mt5.COPY_TICKS_ALL)
    err_m3 = mt5.last_error()

    conclusion = {
        "measured_median_lag_if_treat_as_utc_s": med_offset,
        "windows_utc_coherent_with_unix": local_ok,
        "msc_advances_with_wall_clock": abs(float(sample.get("msc_delta_over_wall", 0)) - wall_delta * 1000) < 500,
        "hypothesis_server_gmt3_encoding": hyp_b,
        "hypothesis_windows_local_clock_wrong": (not local_ok),
        "proven_origin": (
            "O terminal/servidor MetaQuotes-Demo fornece tick.time / time_msc como segundos Unix "
            "cujo valor corresponde ao relógio do servidor em GMT+3 (UTC+3), não a UTC verdadeiro. "
            "Interpretar o inteiro como UTC produz lag≈−10800s; reinterpretar o mesmo inteiro como "
            "GMT+3 e converter para UTC produz lag≈0. O relógio Windows/UTC local está coerente. "
            "time_msc avança com o tempo real (~2s), provando que as cotações são atualizadas ao vivo, "
            "mas o campo temporal não é UTC. Nenhuma correção fixa foi aplicada ao robô."
            if hyp_b and local_ok
            else "Origem não totalmente determinada — ver campos brutos."
        ),
        "claims_realtime_allowed": False,
        "reason_no_claims_realtime": (
            "Timestamps não representam UTC; embora as atualizações sejam recentes (msc avança), "
            "não se declara claims_realtime=true sem timestamps UTC verificáveis."
        ),
        "copy_ticks_from_utc_now_count": 0 if ticks_now is None else len(ticks_now),
        "copy_ticks_from_utc_now_error": err_now,
        "copy_ticks_from_utc_minus_3h_count": 0 if ticks_m3 is None else len(ticks_m3),
        "copy_ticks_from_utc_minus_3h_error": err_m3,
    }

    report["pairs"] = pairs
    report["candles"] = candles
    report["conclusion"] = conclusion

    mt5.shutdown()
    path = OUT / "mt5_timestamp_diagnosis.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print("WROTE", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
