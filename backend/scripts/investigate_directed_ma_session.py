"""Investigação direcionada: MA crossover + tendência por sessão + filtros H1/vol/hora.

- Dados Dukascopy reais apenas
- H1 só com velas fechadas (sem look-ahead)
- Hipóteses limitadas a priori
- Walk-forward em 3 janelas; aprovação só se >=2 janelas passarem critérios existentes
- Não promove para o robô; não chama OpenAI
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.adaptive.costs import apply_entry_price, estimate_round_trip_costs
from app.adaptive.regimes import classify_regime
from app.adaptive.scoring import reject_for_overfitting, score_candidate
from app.adaptive.strategies import build_strategy
from app.adaptive.strategies.session_trend import set_bar_hour
from app.adaptive.validation import chronological_split
from app.backtest.metrics import compute_metrics
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.market.history_inventory import DEFAULT_INVESTIGATION_END, DEFAULT_INVESTIGATION_START
from app.market.indicators import atr, sma
from app.market.labels import DUKASCOPY_SOURCE
from app.models.market import Candle
from app.risk.instruments import get_instrument

MIN_SCORE = 0.5
MIN_PF_VAL = 1.05
MIN_PF_OOS = 1.0
MIN_TRADES_VAL = 5
LOTS = 0.10


@dataclass(frozen=True)
class Hypothesis:
    id: str
    base_key: str
    params: dict[str, Any]
    use_h1_trend: bool = False
    use_vol_filter: bool = False
    hour_start: int | None = None  # filtro horário extra (UTC); None = só o da estratégia
    hour_end: int | None = None
    note: str = ""


# Hipóteses FIXAS a priori — não acrescentar após ver resultados.
HYPOTHESES: list[Hypothesis] = [
    Hypothesis("MA_base_20_50", "ma_crossover", {"fast": 20, "slow": 50}, note="original default"),
    Hypothesis("MA_near_5_50", "ma_crossover", {"fast": 5, "slow": 50}, note="near-miss histórico"),
    Hypothesis("MA_20_50_H1", "ma_crossover", {"fast": 20, "slow": 50}, use_h1_trend=True, note="default+H1"),
    Hypothesis("MA_5_50_H1", "ma_crossover", {"fast": 5, "slow": 50}, use_h1_trend=True, note="near-miss+H1"),
    Hypothesis(
        "MA_20_50_H1_vol",
        "ma_crossover",
        {"fast": 20, "slow": 50},
        use_h1_trend=True,
        use_vol_filter=True,
        note="default+H1+vol",
    ),
    Hypothesis(
        "MA_20_50_sess_7_17",
        "ma_crossover",
        {"fast": 20, "slow": 50},
        hour_start=7,
        hour_end=17,
        note="default+hora Londres/NY",
    ),
    Hypothesis(
        "ST_base_20_60",
        "session_trend",
        {"fast": 20, "slow": 60, "session_start_utc": 7, "session_end_utc": 17, "allow_all_hours": 0},
        note="original default",
    ),
    Hypothesis(
        "ST_10_40",
        "session_trend",
        {"fast": 10, "slow": 40, "session_start_utc": 7, "session_end_utc": 17, "allow_all_hours": 0},
        note="variante sessão mais rápida",
    ),
    Hypothesis(
        "ST_20_60_H1",
        "session_trend",
        {"fast": 20, "slow": 60, "session_start_utc": 7, "session_end_utc": 17, "allow_all_hours": 0},
        use_h1_trend=True,
        note="default+H1",
    ),
    Hypothesis(
        "ST_10_40_H1_vol",
        "session_trend",
        {"fast": 10, "slow": 40, "session_start_utc": 7, "session_end_utc": 17, "allow_all_hours": 0},
        use_h1_trend=True,
        use_vol_filter=True,
        note="rápida+H1+vol",
    ),
]


@dataclass
class WindowResult:
    window: str
    trades_val: int
    pf_val: float
    pf_oos: float
    net_pnl_oos: float
    max_dd_oos: float
    pf_train: float
    trades_oos: int
    score: float
    passed: bool
    reason: str


@dataclass
class HypothesisReport:
    hypothesis_id: str
    base_key: str
    note: str
    params: dict[str, Any]
    filters: dict[str, Any]
    windows: list[WindowResult] = field(default_factory=list)
    windows_passed: int = 0
    decision: str = "rejeitada"
    reason: str = ""


def verify_costs() -> dict[str, Any]:
    settings = get_settings()
    spec = get_instrument("EURUSD")
    costs = estimate_round_trip_costs("EURUSD", LOTS)
    pip_val = spec.pip_value_account(LOTS, settings.account_currency)
    expected_spread = settings.sim_spread_pips * pip_val
    expected_slip = settings.sim_slippage_pips * 2.0 * pip_val
    expected_comm = settings.sim_commission_per_lot * LOTS
    expected_total = expected_spread + expected_slip + expected_comm
    ok = (
        abs(pip_val - 1.0) < 1e-9
        and abs(costs.spread_cash - expected_spread) < 1e-9
        and abs(costs.total - expected_total) < 1e-9
        and abs(costs.total - 2.5) < 1e-9
    )
    return {
        "lots": LOTS,
        "pip_size": spec.pip_size,
        "pip_value_per_lot_usd": pip_val,
        "spread_pips": costs.spread_pips,
        "slippage_pips": costs.slippage_pips,
        "commission_per_lot_setting": settings.sim_commission_per_lot,
        "commission_cash": costs.commission,
        "spread_cash": costs.spread_cash,
        "slippage_cash": costs.slippage_cash,
        "total_round_trip": costs.total,
        "expected_total": expected_total,
        "costs_consistent": ok,
        "note": (
            "EURUSD: 1 pip = $10 / lote standard → $1 / 0.1 lote. "
            "Spread 1.2 pip = $1.2; slip 0.3×2 = $0.6; comissão $7×0.1 = $0.7; total $2.5."
        ),
    }


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def load_bars(db, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
    return list(
        db.scalars(
            select(Candle)
            .where(
                Candle.symbol == "EURUSD",
                Candle.timeframe == timeframe,
                Candle.source == DUKASCOPY_SOURCE,
                Candle.is_synthetic == False,  # noqa: E712
                Candle.ts >= start,
                Candle.ts <= end,
            )
            .order_by(Candle.ts.asc())
        )
    )


def build_closed_h1_index(m15: list[Candle], h1: list[Candle]) -> list[int | None]:
    """Para cada M15[i], índice do último H1 já fechado antes do open de M15[i]."""
    idx: list[int | None] = []
    j = -1
    for c in m15:
        ts = _aware(c.ts)
        while j + 1 < len(h1) and _aware(h1[j + 1].ts) + timedelta(hours=1) <= ts:
            j += 1
        idx.append(j if j >= 0 else None)
    return idx


def _hour_ok(ts: datetime, start_h: int, end_h: int) -> bool:
    h = _aware(ts).hour
    if start_h <= end_h:
        return start_h <= h < end_h
    return h >= start_h or h < end_h


def simulate_filtered(
    m15: list[Candle],
    h1: list[Candle],
    h1_idx: list[int | None],
    hyp: Hypothesis,
    *,
    split_label: str = "",
) -> dict[str, Any]:
    """Simulação com filtros; sinal na barra i, execução no open i+1; H1 só fechado."""
    if len(m15) < 80:
        return compute_metrics([], get_settings().starting_equity).as_dict()

    settings = get_settings()
    spec = get_instrument("EURUSD")
    costs_tpl = estimate_round_trip_costs("EURUSD", LOTS)
    strategy = build_strategy(hyp.base_key, dict(hyp.params))
    complexity = float(strategy.spec.complexity)

    opens = [float(c.open) for c in m15]
    highs = [float(c.high) for c in m15]
    lows = [float(c.low) for c in m15]
    closes = [float(c.close) for c in m15]
    h1_closes = [float(c.close) for c in h1]
    h1_highs = [float(c.high) for c in h1]
    h1_lows = [float(c.low) for c in h1]

    lookback = 120
    trades_pnl: list[float] = []
    total_costs = 0.0
    position: dict[str, Any] | None = None

    for i in range(50, len(m15) - 1):
        lo = max(0, i + 1 - lookback)
        c_win = closes[lo : i + 1]
        h_win = highs[lo : i + 1]
        l_win = lows[lo : i + 1]
        o_win = opens[lo : i + 1]

        if position is not None:
            bar = m15[i + 1]
            hit = None
            if position["side"] == "BUY":
                if float(bar.low) <= position["stop"]:
                    hit = position["stop"]
                elif float(bar.high) >= position["take"]:
                    hit = position["take"]
            else:
                if float(bar.high) >= position["stop"]:
                    hit = position["stop"]
                elif float(bar.low) <= position["take"]:
                    hit = position["take"]
            if hit is not None:
                pnl_raw = spec.pnl(position["side"], position["entry"], hit, LOTS, settings.account_currency)
                trades_pnl.append(pnl_raw - position["costs"])
                total_costs += position["costs"]
                position = None

        if position is not None:
            continue

        set_bar_hour(strategy, getattr(m15[i], "ts", None))
        sig = strategy.signal(c_win, h_win, l_win, o_win)
        if sig == "HOLD":
            continue

        # Filtro horário extra (para MA sem sessão nativa).
        if hyp.hour_start is not None and hyp.hour_end is not None:
            if not _hour_ok(m15[i].ts, hyp.hour_start, hyp.hour_end):
                continue

        # Filtro tendência H1 — apenas vela H1 fechada.
        hi = h1_idx[i]
        if hyp.use_h1_trend:
            if hi is None or hi < 55:
                continue
            h1_win = h1_closes[: hi + 1]
            h1_sma = sma(h1_win, 50)
            if h1_sma is None:
                continue
            last_h1 = h1_win[-1]
            if sig == "BUY" and not (last_h1 > h1_sma):
                continue
            if sig == "SELL" and not (last_h1 < h1_sma):
                continue

        # Filtro volatilidade: ATR M15 atual vs mediana das últimas 50 ATR.
        if hyp.use_vol_filter:
            if len(c_win) < 40:
                continue
            atrs: list[float] = []
            for k in range(20, len(c_win)):
                a = atr(h_win[: k + 1], l_win[: k + 1], c_win[: k + 1], 14)
                if a is not None:
                    atrs.append(a)
            a_now = atr(h_win, l_win, c_win, 14)
            if a_now is None or len(atrs) < 10:
                continue
            med = sorted(atrs)[len(atrs) // 2]
            if med <= 0:
                continue
            ratio = a_now / med
            if ratio < 0.7 or ratio > 1.6:
                continue

        next_open = opens[i + 1]
        entry = apply_entry_price(sig, next_open, costs_tpl.spread_pips, costs_tpl.slippage_pips, "EURUSD")
        stop_pips, take_pips = 15.0, 25.0
        if sig == "BUY":
            stop = entry - spec.price_from_pips(stop_pips)
            take = entry + spec.price_from_pips(take_pips)
        else:
            stop = entry + spec.price_from_pips(stop_pips)
            take = entry - spec.price_from_pips(take_pips)
        position = {
            "side": sig,
            "entry": entry,
            "stop": stop,
            "take": take,
            "costs": costs_tpl.total,
        }

    metrics = compute_metrics(trades_pnl, settings.starting_equity).as_dict()
    metrics["total_costs"] = round(total_costs, 4)
    metrics["_complexity"] = complexity
    metrics["_split"] = split_label
    return metrics


def evaluate_hypothesis_on_window(
    m15: list[Candle],
    h1: list[Candle],
    hyp: Hypothesis,
    window_name: str,
) -> WindowResult:
    h1_idx = build_closed_h1_index(m15, h1)
    split = chronological_split(m15)
    # Alinhar H1 idx aos subconjuntos: reindexar por fatias das listas m15
    # chronological_split devolve listas de candles — reconstruir índices por ts.
    def _slice_idx(part: list[Candle]) -> tuple[list[Candle], list[Candle], list[int | None]]:
        if not part:
            return [], [], []
        t0, t1 = _aware(part[0].ts), _aware(part[-1].ts)
        m = [c for c in m15 if t0 <= _aware(c.ts) <= t1]
        # h1: incluir histórico anterior para SMA (lookback) sem look-ahead no futuro
        h_end = t1
        h = [c for c in h1 if _aware(c.ts) <= h_end]
        # Precisamos H1 antes de t0 para SMA — já em h se h1 global começa mais cedo
        idx = build_closed_h1_index(m, h)
        return m, h, idx

    # Avaliar treino/val/teste com H1 disponível até ao fim de cada split
    # (sem usar H1 futuro relativamente a cada barra — índice fechado cuida disso).
    train_m, train_h, _ = _slice_idx(split.train)
    # Para SMA H1 no início do treino, usar todo h1 até fim do treino + histórico
    train_h = [c for c in h1 if _aware(c.ts) + timedelta(hours=1) <= _aware(split.train[-1].ts) + timedelta(hours=1)]
    # Simplificar: usar h1 completo até fim da janela (barras futuras H1 após m15[i] não entram pelo índice)
    window_h1 = [c for c in h1 if _aware(c.ts) <= _aware(m15[-1].ts)]

    def _run(part: list[Candle], label: str) -> dict[str, Any]:
        if len(part) < 80:
            return compute_metrics([], get_settings().starting_equity).as_dict() | {"_complexity": 1.0}
        # H1 até ao fim da part + histórico anterior da janela
        local_h1 = [c for c in window_h1 if _aware(c.ts) <= _aware(part[-1].ts)]
        # Incluir H1 anteriores ao início da part (para SMA)
        earlier = [c for c in h1 if _aware(c.ts) < _aware(part[0].ts)]
        # Unir sem duplicar
        seen = set()
        merged: list[Candle] = []
        for c in earlier + local_h1:
            key = _aware(c.ts)
            if key in seen:
                continue
            seen.add(key)
            merged.append(c)
        merged.sort(key=lambda x: _aware(x.ts))
        idx = build_closed_h1_index(part, merged)
        return simulate_filtered(part, merged, idx, hyp, split_label=label)

    # Nota: chronological_split já separa; cada part é simulada só com as suas barras M15.
    tr = _run(split.train, "train")
    va = _run(split.validation, "validation")
    te = _run(split.test, "test")

    overfit, overfit_reason = reject_for_overfitting(tr, va)
    strategy = build_strategy(hyp.base_key, dict(hyp.params))
    scores = score_candidate(
        train_metrics=tr,
        validation_metrics=va,
        test_metrics=te,
        complexity=float(strategy.spec.complexity) + (0.2 if hyp.use_h1_trend else 0) + (0.1 if hyp.use_vol_filter else 0),
        experiment_count=1,
        regime_match_ratio=0.5,
    )
    score = float(scores["total"])
    pf_v = float(va.get("profit_factor") or 0)
    pf_t = float(te.get("profit_factor") or 0)
    n_v = int(va.get("trades") or 0)
    reasons: list[str] = []
    passed = True
    if overfit:
        passed = False
        reasons.append(overfit_reason or "sobreajuste")
    if pf_v < MIN_PF_VAL:
        passed = False
        reasons.append(f"PF_val={pf_v:.3f}<{MIN_PF_VAL}")
    if pf_t < MIN_PF_OOS:
        passed = False
        reasons.append(f"PF_oos={pf_t:.3f}<{MIN_PF_OOS}")
    if n_v < MIN_TRADES_VAL:
        passed = False
        reasons.append(f"ops_val={n_v}<{MIN_TRADES_VAL}")
    if score < MIN_SCORE:
        passed = False
        reasons.append(f"score={score:.2f}<{MIN_SCORE}")
    if passed:
        reasons.append("passou critérios nesta janela")

    return WindowResult(
        window=window_name,
        trades_val=n_v,
        pf_val=round(pf_v, 4),
        pf_oos=round(pf_t, 4),
        net_pnl_oos=round(float(te.get("net_pnl") or 0), 2),
        max_dd_oos=round(float(te.get("max_drawdown_pct") or 0), 2),
        pf_train=round(float(tr.get("profit_factor") or 0), 4),
        trades_oos=int(te.get("trades") or 0),
        score=round(score, 4),
        passed=passed,
        reason="; ".join(reasons),
    )


def windows(start: datetime, end: datetime) -> list[tuple[str, datetime, datetime]]:
    total = (end - start).total_seconds()
    third = total / 3.0
    a = start + timedelta(seconds=third)
    b = start + timedelta(seconds=2 * third)
    return [
        ("A_inicial", start, a),
        ("B_media", a, b),
        ("C_recente", b, end),
    ]


def main() -> int:
    init_db()
    assert len(HYPOTHESES) <= 10, "limite de hipóteses excedido"
    db = SessionLocal()
    try:
        cost_audit = verify_costs()
        print("=== AUDITORIA DE CUSTOS ===")
        print(json.dumps(cost_audit, indent=2, ensure_ascii=False))
        if not cost_audit["costs_consistent"]:
            print("AVISO: inconsistência de custos — a investigação continua sem alterar custos.")

        start, end = DEFAULT_INVESTIGATION_START, DEFAULT_INVESTIGATION_END
        n_m15 = db.scalar(
            select(func.count(Candle.id)).where(
                Candle.symbol == "EURUSD",
                Candle.timeframe == "M15",
                Candle.source == DUKASCOPY_SOURCE,
                Candle.is_synthetic == False,  # noqa: E712
            )
        )
        n_h1 = db.scalar(
            select(func.count(Candle.id)).where(
                Candle.symbol == "EURUSD",
                Candle.timeframe == "H1",
                Candle.source == DUKASCOPY_SOURCE,
                Candle.is_synthetic == False,  # noqa: E712
            )
        )
        print(f"Dados Dukascopy: M15={n_m15} H1={n_h1} período {start.date()}→{end.date()}")
        print(f"Hipóteses pré-definidas: {len(HYPOTHESES)}")

        reports: list[HypothesisReport] = []
        for hyp in HYPOTHESES:
            rep = HypothesisReport(
                hypothesis_id=hyp.id,
                base_key=hyp.base_key,
                note=hyp.note,
                params=dict(hyp.params),
                filters={
                    "h1_trend": hyp.use_h1_trend,
                    "vol_filter": hyp.use_vol_filter,
                    "hour": [hyp.hour_start, hyp.hour_end],
                },
            )
            print(f"\n### {hyp.id} ({hyp.note})")
            for wname, w0, w1 in windows(start, end):
                m15 = load_bars(db, "M15", w0, w1)
                # H1 com histórico prévio para SMA (90 dias antes)
                h1_start = w0 - timedelta(days=90)
                h1 = load_bars(db, "H1", h1_start, w1)
                print(f"  {wname}: M15={len(m15)} H1={len(h1)}")
                if len(m15) < 200 or len(h1) < 80:
                    wr = WindowResult(
                        window=wname,
                        trades_val=0,
                        pf_val=0,
                        pf_oos=0,
                        net_pnl_oos=0,
                        max_dd_oos=0,
                        pf_train=0,
                        trades_oos=0,
                        score=0,
                        passed=False,
                        reason="dados insuficientes na janela",
                    )
                else:
                    wr = evaluate_hypothesis_on_window(m15, h1, hyp, wname)
                rep.windows.append(wr)
                flag = "PASS" if wr.passed else "FAIL"
                print(
                    f"    [{flag}] ops_val={wr.trades_val} PF_val={wr.pf_val} PF_oos={wr.pf_oos} "
                    f"PnL={wr.net_pnl_oos} DD={wr.max_dd_oos}% | {wr.reason[:100]}"
                )
            rep.windows_passed = sum(1 for w in rep.windows if w.passed)
            if rep.windows_passed >= 2:
                # Critérios de promoção existentes + estabilidade multi-janela.
                # NÃO aprovar no robô automaticamente — apenas marcar elegível.
                rep.decision = "rejeitada"
                rep.reason = (
                    f"Passou em {rep.windows_passed}/3 janelas, mas promoção ao robô "
                    "exige revisão humana; investigação não altera AdaptiveStrategyState."
                )
                # User asked Approved or Rejected — if passes 2 windows with criteria, could say
                # "candidata estável" but NOT approve for trading. Keep rejeitada for robot.
                # Actually user wants "Aprovada ou rejeitada" for the investigation criteria.
                # I'll use: aprovada_investigacao only if >=2 windows — but NOT wire to robot.
                rep.decision = "aprovada_investigacao"
                rep.reason = (
                    f"Critérios + estabilidade em {rep.windows_passed}/3 janelas. "
                    "NÃO promovida ao robô (sem alteração de estado)."
                )
            else:
                rep.decision = "rejeitada"
                fails = [f"{w.window}:{w.reason}" for w in rep.windows if not w.passed]
                rep.reason = f"Só {rep.windows_passed}/3 janelas; " + " || ".join(fails[:3])
            reports.append(rep)

        # Tabela final
        print("\n=== TABELA FINAL ===")
        table = []
        for rep in reports:
            # Agregar: média ponderada das janelas / melhor janela para números representativos
            # Usar soma de ops e médias de PF nas janelas que têm ops
            ops = sum(w.trades_val for w in rep.windows)
            # Mostrar por janela no campo dedicado; métricas agregadas = média das 3
            pf_vals = [w.pf_val for w in rep.windows]
            pf_oos = [w.pf_oos for w in rep.windows]
            pnls = [w.net_pnl_oos for w in rep.windows]
            dds = [w.max_dd_oos for w in rep.windows]
            by_w = {
                w.window: {
                    "ops": w.trades_val,
                    "PF_val": w.pf_val,
                    "PF_oos": w.pf_oos,
                    "PnL": w.net_pnl_oos,
                    "DD": w.max_dd_oos,
                    "passou": w.passed,
                }
                for w in rep.windows
            }
            row = {
                "Hipótese": rep.hypothesis_id,
                "Base": rep.base_key,
                "Nota": rep.note,
                "Operações (soma val)": ops,
                "PF validação (méd)": round(sum(pf_vals) / max(len(pf_vals), 1), 4),
                "PF teste (méd)": round(sum(pf_oos) / max(len(pf_oos), 1), 4),
                "Lucro líquido teste (soma)": round(sum(pnls), 2),
                "Drawdown máx. (máx janelas)": round(max(dds) if dds else 0, 2),
                "Janelas": by_w,
                "Janelas_ok": rep.windows_passed,
                "Decisão": rep.decision,
                "Motivo": rep.reason,
            }
            table.append(row)
            print(
                f"| {row['Hipótese']} | ops={row['Operações (soma val)']} | "
                f"PFv={row['PF validação (méd)']} | PFt={row['PF teste (méd)']} | "
                f"PnL={row['Lucro líquido teste (soma)']} | DD={row['Drawdown máx. (máx janelas)']} | "
                f"{row['Janelas_ok']}/3 | {row['Decisão']} |"
            )

        approved_inv = [r for r in reports if r.decision == "aprovada_investigacao"]
        conclusion = (
            "Nenhuma hipótese atingiu critérios em ≥2 janelas walk-forward. "
            "Robô permanece em AGUARDAR. Sem mais otimização sobre os mesmos dados."
            if not approved_inv
            else (
                f"{len(approved_inv)} hipótese(s) estável(is) na investigação, "
                "mas NÃO promovida(s) ao estado approved do robô. "
                "Robô permanece em AGUARDAR até promoção formal."
            )
        )
        print("\nCONCLUSÃO:", conclusion)

        out = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ai_enabled": False,
            "live_trading": False,
            "costs_audit": cost_audit,
            "criteria": {
                "min_score": MIN_SCORE,
                "min_pf_val": MIN_PF_VAL,
                "min_pf_oos": MIN_PF_OOS,
                "min_trades_val": MIN_TRADES_VAL,
                "min_windows": 2,
            },
            "hypotheses_predeclared": [asdict(h) for h in HYPOTHESES],
            "reports": [
                {
                    **{k: getattr(rep, k) for k in ("hypothesis_id", "base_key", "note", "params", "filters", "windows_passed", "decision", "reason")},
                    "windows": [asdict(w) for w in rep.windows],
                }
                for rep in reports
            ],
            "table": table,
            "robot_approved_count": 0,
            "conclusion": conclusion,
        }
        path = Path(__file__).resolve().parents[1] / "data" / "directed_ma_session_investigation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print("JSON:", path)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
