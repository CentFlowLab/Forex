"""Radar multimeda local — triagem leve sem OpenAI e sem estratégias fixas."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.robot.mt5_symbol_discovery import discover_forex_universe, symbols_for_radar
from app.robot.pair_refresh import get_pair_refresh_tracker
from app.robot.pair_snapshot import build_pair_snapshot
from app.robot.universe import (
    FOREX_28_SYMBOLS,
    MIN_PAID_MT5_PAIRS,
    OPENAI_TOP_CANDIDATES,
    pair_meta,
)

# Limiar de atraso para considerar candidato a análise profunda (histórico OK).
_MAX_LAG_CANDIDATE = 6 * 3600.0
_TOP_N = OPENAI_TOP_CANDIDATES  # 5 candidatos detalhados para a OpenAI


@dataclass
class RadarCandidate:
    symbol: str
    display: str
    score: float
    reasons: list[str] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        q = self.snapshot.get("quote") or {}
        s = self.snapshot.get("structure") or {}
        v = self.snapshot.get("volatility") or {}
        c = self.snapshot.get("trading_costs") or {}
        dq = self.snapshot.get("data_quality") or {}
        # Contexto compacto para a IA — sem todas as velas brutas (só contagens + OHLC resumido).
        candles = self.snapshot.get("candles_closed") or {}
        m15 = candles.get("M15") or []
        clock_anom = bool(q.get("clock_anomaly"))
        # Com anomalia de relógio, tick_time_lag_s (~±3h) confunde o modelo como "atraso".
        # A frescura operacional é update_age_s / feed_state.
        quote_ai: dict[str, Any] = {
            "mid": q.get("mid"),
            "bid": q.get("bid"),
            "ask": q.get("ask"),
            "update_age_s": q.get("update_age_s"),
            "spread_pips": q.get("spread_pips"),
            "data_class": q.get("data_class"),
            "claims_realtime": q.get("claims_realtime"),
            "block_entries": q.get("block_entries"),
            "is_synthetic": q.get("is_synthetic"),
            "channel": q.get("channel"),
            "source": q.get("source"),
            "feed_state": q.get("feed_state"),
            "clock_anomaly": clock_anom,
            "ts": q.get("ts"),
        }
        if not clock_anom:
            quote_ai["lag_seconds"] = q.get("lag_seconds")
            quote_ai["tick_time_lag_s"] = q.get("tick_time_lag_s")
            quote_ai["tick_msc_lag_s"] = q.get("tick_msc_lag_s")
        else:
            quote_ai["lag_note"] = (
                "clock_anomaly=true: ignorar tick.time como UTC; "
                "usar update_age_s e feed_state para frescura."
            )
        vol_ai = {
            "atr14_m15": v.get("atr14_m15"),
            "trend_slope": v.get("trend_slope"),
            "atr_ratio": v.get("atr_ratio"),
            "adx_proxy": v.get("adx_proxy"),
            "bars_used": v.get("bars_used"),
            "local_regime_hint": v.get("local_regime_hint") or v.get("regime"),
            "local_regime_hint_pt": v.get("local_regime_hint_pt") or v.get("regime_label_pt"),
            "local_regime_note": (
                "Heurística ATR/SMA local — NÃO é recomendação nem veto. "
                "Avalia tendência/volatilidade pelos preços e estrutura; "
                "não copies este rótulo como conclusão final."
            ),
        }
        recent_m15 = [
            {
                "c": x.get("c"),
                "h": x.get("h"),
                "l": x.get("l"),
                "o": x.get("o"),
            }
            for x in m15[-12:]
        ]
        return {
            "symbol": self.symbol,
            "display": self.display,
            "score": round(self.score, 3),
            "triage_reasons": self.reasons,
            "quote": quote_ai,
            "candle_source": self.snapshot.get("candle_source"),
            "structure": s,
            "volatility": vol_ai,
            "trading_costs": {
                "total_round_trip_usd": c.get("total"),
                "spread_pips": c.get("spread_pips"),
            },
            "candles_counts": self.snapshot.get("candles_counts"),
            "recent_m15_closes": [x["c"] for x in m15[-12:]],
            "recent_m15_ohlc": recent_m15,
            "data_quality": dq,
            "executable": bool(dq.get("executable")),
        }


@dataclass
class RadarScan:
    monitored: int
    with_valid_data: int
    candidates: list[RadarCandidate]
    rejected: list[dict[str, Any]]
    pairs: list[dict[str, Any]]
    all_valid: list[RadarCandidate] = field(default_factory=list)
    directional: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "monitored": self.monitored,
            "with_valid_data": self.with_valid_data,
            "candidates": [c.summary() for c in self.candidates],
            "all_valid": [c.summary() for c in self.all_valid],
            "rejected": self.rejected,
            "pairs": self.pairs,
            "directional": {
                "top_opportunities": (self.directional or {}).get("top_opportunities") or [],
                "top_buys": (self.directional or {}).get("top_buys") or [],
                "top_sells": (self.directional or {}).get("top_sells") or [],
                "actionable_count": (self.directional or {}).get("actionable_count"),
                "ranking_metric": (self.directional or {}).get("ranking_metric"),
                "generated_at": (self.directional or {}).get("generated_at"),
                "disclaimer": (self.directional or {}).get("disclaimer"),
            }
            if self.directional
            else {},
            "note": (
                "Triagem local + motor direcional quantitativo — não é ordem. "
                "Dados sintéticos nunca são oportunidades reais."
            ),
        }

    def compact_for_ai(self) -> dict[str, Any]:
        """Contexto OpenAI: top-5 detalhado + síntese dos restantes (poupar tokens)."""
        top = list(self.candidates[:OPENAI_TOP_CANDIDATES])
        top_syms = {c.symbol for c in top}
        pairs_payload = [c.summary() for c in top]
        rest_brief = []
        for c in self.all_valid:
            if c.symbol in top_syms:
                continue
            q = c.snapshot.get("quote") or {}
            v = c.snapshot.get("volatility") or {}
            s = c.snapshot.get("structure") or {}
            costs = c.snapshot.get("trading_costs") or {}
            rest_brief.append(
                {
                    "symbol": c.symbol,
                    "display": c.display,
                    "score": round(c.score, 3),
                    "mid": q.get("mid"),
                    "spread_pips": q.get("spread_pips"),
                    "feed_state": q.get("feed_state"),
                    "update_age_s": q.get("update_age_s"),
                    "atr14_m15": v.get("atr14_m15"),
                    "trend_slope": v.get("trend_slope"),
                    "local_regime_hint": v.get("local_regime_hint") or v.get("regime"),
                    "above_sma20": s.get("above_sma20"),
                    "above_sma50": s.get("above_sma50"),
                    "round_trip_usd": costs.get("total"),
                    "triage_reasons": c.reasons[:3],
                }
            )
        rest_brief.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
        from app.robot.directional_engine import opportunities_for_openai

        dir_top = opportunities_for_openai(self.directional) if self.directional else []
        return {
            "monitored": self.monitored,
            "with_valid_data": self.with_valid_data,
            "universe_size_target": 28,
            "pairs_for_comparison": pairs_payload,
            "other_pairs_brief": rest_brief[:23],
            "local_priority_hint": [
                {"symbol": c.symbol, "score": round(c.score, 3)} for c in top
            ],
            "directional_opportunities": dir_top,
            "directional_note": (
                "directional_opportunities: ranking quantitativo local por expectativa líquida "
                "após custos (P(up/down/flat) calibradas em histórico de velas fechadas). "
                "NÃO inventes nem alters estas probabilidades. Usa-as como evidência; "
                "podes rejeitar se riscos/contrários forem superiores. "
                "Mesmo em HOLD, indica a direção mais provável do melhor candidato."
            ),
            "data_quality": _data_quality_for_ai(pairs_payload),
            "temporal_disclaimer": _temporal_disclaimer(pairs_payload),
            "rejected_brief": [
                {"symbol": r["symbol"], "reason": r["reason"]} for r in self.rejected[:15]
            ],
            "instruction": (
                "Avaliação independente multimeda. Compara BUY e SELL nas "
                "directional_opportunities (até 5, ordenadas por EV líquido). "
                "Também avalia pairs_for_comparison. "
                "local_priority_hint é ranking de atenção/custo — NÃO é ordem nem sinal; "
                "NÃO inventes probabilidades — usa as do motor quantitativo. "
                f"OBRIGATÓRIO: top_candidates_comparison com os {OPENAI_TOP_CANDIDATES} "
                "candidatos (a favor, contra, custos, o que falta, "
                "verdict accept|reject + motivo com ATR/spread/EV/probs). "
                "HOLD só se nenhum candidato tiver EV líquido positivo e evidência suficiente — "
                "explica rejeição concreta; indica mesmo assim a direção mais provável. "
                "BUY/SELL com entry/SL/TP + why_preferred se houver edge demonstrável. "
                "Não declares vantagem estatística sem calib_samples/evidence_quality. "
                "Não inventes cotações, velas nem notícias."
            ),
        }


def _data_quality_for_ai(pairs_payload: list[dict[str, Any]]) -> dict[str, Any]:
    """Resumo factual da origem/qualidade — sem afirmar frescura inventada."""
    sources: dict[str, int] = {}
    candle_sources: dict[str, int] = {}
    update_ages: list[float] = []
    clock_n = 0
    mt5_recent = 0
    for p in pairs_payload:
        q = p.get("quote") or {}
        src = str(q.get("source") or "desconhecido")
        sources[src] = sources.get(src, 0) + 1
        cs = str(p.get("candle_source") or "desconhecido")
        candle_sources[cs] = candle_sources.get(cs, 0) + 1
        if q.get("clock_anomaly"):
            clock_n += 1
        ua = q.get("update_age_s")
        if ua is not None:
            try:
                update_ages.append(float(ua))
            except (TypeError, ValueError):
                pass
        if src == "mt5" and q.get("feed_state") == "recente":
            mt5_recent += 1
    primary = "mt5" if sources.get("mt5", 0) >= max(1, len(pairs_payload) // 2 + 1) else (
        max(sources, key=sources.get) if sources else "desconhecido"
    )
    return {
        "primary_quote_source": primary,
        "quote_sources": sources,
        "candle_sources": candle_sources,
        "pairs_total": len(pairs_payload),
        "mt5_feed_state_recente": mt5_recent,
        "clock_anomaly_pairs": clock_n,
        "update_age_s_min": round(min(update_ages), 3) if update_ages else None,
        "update_age_s_max": round(max(update_ages), 3) if update_ages else None,
        "claims_realtime": False,
        "claims_realtime_note": (
            "claims_realtime=false: o timestamp tick.time do MT5 não é tratado como UTC "
            "verificável. Isto NÃO significa automaticamente que o preço está atrasado — "
            "ver update_age_s e feed_state."
        ),
    }


def _temporal_disclaimer(pairs_payload: list[dict[str, Any]]) -> str:
    """Disclaimer baseado nos dados reais do scan (não assume Dukascopy)."""
    dq = _data_quality_for_ai(pairs_payload)
    primary = dq["primary_quote_source"]
    candle_primary = (
        max(dq["candle_sources"], key=dq["candle_sources"].get)
        if dq["candle_sources"]
        else "desconhecido"
    )
    ua_min, ua_max = dq["update_age_s_min"], dq["update_age_s_max"]
    age_txt = (
        f"idade da última atualização de cotação update_age_s≈{ua_min}–{ua_max}s"
        if ua_min is not None and ua_max is not None
        else "idade de atualização de cotação não disponível em todos os pares"
    )
    clock_txt = (
        f"{dq['clock_anomaly_pairs']} par(es) com anomalia de relógio no tick.time MT5 "
        "(offset tipicamente ~3h); a frescura operacional usa update_age_s, não tick.time como UTC."
        if dq["clock_anomaly_pairs"]
        else "sem anomalia de relógio reportada nos pares deste scan."
    )
    if primary == "mt5" and candle_primary == "mt5":
        origin = (
            "Origem principal: MetaTrader 5 (bid/ask e velas M15/H1/H4). "
            "Dukascopy não é a fonte deste contexto."
        )
    elif primary == "mt5":
        origin = (
            f"Cotações principais: MT5. Velas: {candle_primary} "
            "(histórico complementar quando MT5 não tem série completa)."
        )
    else:
        origin = (
            f"Origem dominante das cotações neste scan: {primary}; "
            f"velas: {candle_primary}. Não afirmar tempo-real sem evidência."
        )
    return (
        f"{origin} {age_txt}. {clock_txt} "
        "Cada par tem o seu timestamp; não compares preços de momentos diferentes "
        "como se fossem o mesmo instante. Velas no contexto são FECHADAS "
        "(o fecho pode ter vários minutos — isso é normal para M15/H1/H4, "
        "não é o mesmo que cotação tick obsoleta)."
    )


def validate_scan_for_paid(scan: "RadarScan") -> dict[str, Any]:
    """Suspende análise paga se a qualidade/origem não puder ser validada."""
    # Validar sobretudo os candidatos que vão no contexto OpenAI (top-5).
    top = list(scan.candidates[:OPENAI_TOP_CANDIDATES]) or list(scan.all_valid[:OPENAI_TOP_CANDIDATES])
    pairs = [c.summary() for c in top]
    dq = _data_quality_for_ai(pairs)
    mt5_quotes = int(dq["quote_sources"].get("mt5", 0))
    mt5_candles = int(dq["candle_sources"].get("mt5", 0))
    min_n = min(MIN_PAID_MT5_PAIRS, max(1, len(scan.all_valid)))
    if len(scan.all_valid) < min_n:
        return {
            "ok": False,
            "reason": (
                f"Qualidade insuficiente para análise paga: "
                f"{len(scan.all_valid)} pares válidos (mínimo {min_n}; "
                f"monitorizados={scan.monitored}, alvo universo=28)."
            ),
            "data_quality": dq,
        }
    need = min(MIN_PAID_MT5_PAIRS, len(pairs) or MIN_PAID_MT5_PAIRS)
    if mt5_quotes < need:
        return {
            "ok": False,
            "reason": (
                f"Cotações MT5 incompletas nos top candidatos ({mt5_quotes}/{need}). "
                "Análise paga suspensa."
            ),
            "data_quality": dq,
        }
    if mt5_candles < need:
        return {
            "ok": False,
            "reason": (
                f"Velas MT5 incompletas nos top candidatos ({mt5_candles}/{need}). "
                "Análise paga suspensa."
            ),
            "data_quality": dq,
        }
    # Inconsistência: update_age recente mas feed_state não recente.
    bad = []
    for p in pairs:
        q = p.get("quote") or {}
        try:
            ua = float(q["update_age_s"]) if q.get("update_age_s") is not None else None
        except (TypeError, ValueError):
            ua = None
        if q.get("source") == "mt5" and ua is not None and ua <= 30 and q.get("feed_state") not in {
            "recente",
            "atrasado",
            "mercado_fechado",
        }:
            bad.append(p.get("symbol"))
    if bad:
        return {
            "ok": False,
            "reason": (
                "Inconsistência temporal concreta: update_age baixo com feed_state "
                f"inesperado em {', '.join(bad)}."
            ),
            "data_quality": dq,
        }
    return {"ok": True, "reason": "Contexto MT5 validado para análise paga.", "data_quality": dq}


def _triage_score(snap: dict[str, Any]) -> tuple[float, list[str], str | None]:
    """Pontuação de prioridade (não estratégia de entrada).

    Favorece: dados OK, alguma atividade (ATR), deslocamento relativo à média,
    custos baixos. Penaliza: atraso extremo, sintético, sem velas.
    """
    reasons: list[str] = []
    dq = snap.get("data_quality") or {}
    q = snap.get("quote") or {}
    v = snap.get("volatility") or {}
    s = snap.get("structure") or {}
    costs = snap.get("trading_costs") or {}

    if q.get("is_synthetic"):
        return 0.0, ["sintético — excluído da triagem real"], "synthetic"
    if not dq.get("data_ok"):
        return 0.0, ["dados insuficientes (velas/cotação)"], "insufficient_data"

    lag = q.get("lag_seconds")
    if lag is not None and lag > _MAX_LAG_CANDIDATE:
        return 0.0, [f"atraso demasiado elevado ({lag:.0f}s)"], "too_stale"

    score = 0.5
    reasons.append("dados base OK")

    atr_v = v.get("atr14_m15")
    last = s.get("last_close")
    sma20 = s.get("sma20")
    if atr_v and atr_v > 0 and last and sma20:
        displacement = abs(float(last) - float(sma20)) / float(atr_v)
        # Atividade / interesse — não direção obrigatória.
        score += min(0.35, displacement * 0.12)
        reasons.append(f"deslocamento_relativo≈{displacement:.2f}")
    elif atr_v:
        score += 0.1
        reasons.append("volatilidade presente")

    total_cost = float(costs.get("total") or 2.5)
    # Custos altos reduzem prioridade (poupar análise em pares caros sem edge aparente).
    if total_cost <= 2.5:
        score += 0.1
        reasons.append("custos relativos baixos")
    elif total_cost >= 4.0:
        score -= 0.15
        reasons.append("custos relativos elevados")

    try:
        spread = float(q.get("spread_pips") or 0)
    except (TypeError, ValueError):
        spread = 0.0
    if spread >= 3.5:
        score -= 0.2
        reasons.append(f"spread elevado ({spread:.1f} pips)")
        if spread >= 6.0:
            return max(0.0, score), reasons + ["spread proibitivo"], "spread_too_wide"
    elif 0 < spread <= 1.5:
        score += 0.05

    # Distância a zonas (swing) em ATR — interesse, não direcção.
    atr_v2 = v.get("atr14_m15")
    last2 = s.get("last_close")
    sh = s.get("swing_high_20")
    sl = s.get("swing_low_20")
    if atr_v2 and atr_v2 > 0 and last2 is not None and sh is not None and sl is not None:
        dist = min(abs(float(last2) - float(sh)), abs(float(last2) - float(sl))) / float(atr_v2)
        if dist <= 1.0:
            score += 0.06
            reasons.append(f"perto_zona_swing≈{dist:.2f} ATR")

    # Frescura operacional (update_age) — sem inventar score alto.
    try:
        ua = float(q["update_age_s"]) if q.get("update_age_s") is not None else None
    except (TypeError, ValueError):
        ua = None
    if q.get("source") == "mt5" and q.get("feed_state") == "recente" and ua is not None and ua <= 30:
        score += 0.08
        reasons.append("feed MT5 recente")
    elif ua is not None and ua > 120:
        score -= 0.12
        reasons.append("cotação pouco fresca")

    if q.get("claims_realtime"):
        score += 0.15
        reasons.append("cotação mais fresca")
    elif q.get("channel") == "candle_derived":
        score -= 0.05
        reasons.append("mid derivado de vela (não executável)")

    if lag is not None:
        if lag < 600:
            score += 0.08
        elif lag > 3600:
            score -= 0.1
            reasons.append("atraso horário elevado")

    # Cap duro — não fabricar scores ~1.0 sem evidência.
    return min(0.98, max(0.0, score)), reasons, None


def scan_radar(db: Session, *, top_n: int = _TOP_N) -> RadarScan:
    candidates: list[RadarCandidate] = []
    all_valid: list[RadarCandidate] = []
    rejected: list[dict[str, Any]] = []
    pairs_status: list[dict[str, Any]] = []
    valid = 0

    # Preferir símbolos elegíveis descobertos no MT5; senão os 28 canónicos.
    try:
        discover_forex_universe()
    except Exception:  # noqa: BLE001
        pass
    watch = symbols_for_radar(prefer_mt5=True)
    if not watch:
        watch = list(FOREX_28_SYMBOLS)

    for sym in watch:
        try:
            meta = pair_meta(sym)
        except ValueError:
            rejected.append({"symbol": sym, "reason": "fora do universo", "score": 0.0})
            continue
        snap = build_pair_snapshot(db, meta.symbol)
        q = snap.get("quote") or {}
        dq = snap.get("data_quality") or {}
        if dq.get("data_ok"):
            valid += 1
        refresh = get_pair_refresh_tracker().get(meta.symbol).as_dict()
        # Estado: MT5 feed_state tem prioridade sobre refresh bi5.
        ui_state = refresh.get("state") or "indisponivel"
        if q.get("source") == "mt5":
            fs = q.get("feed_state") or ""
            if fs == "ligacao_perdida":
                ui_state = "indisponivel"
            elif fs in {"atrasado", "mercado_fechado"}:
                ui_state = "atrasado"
            elif fs == "recente" and dq.get("data_ok"):
                ui_state = "disponivel"
            elif not dq.get("data_ok"):
                ui_state = "indisponivel"
        elif ui_state != "a_atualizar":
            if not dq.get("data_ok"):
                ui_state = "indisponivel"
            elif q.get("lag_seconds") is not None and float(q["lag_seconds"]) > 1800:
                ui_state = "atrasado"
            elif dq.get("data_ok"):
                ui_state = "disponivel" if ui_state == "disponivel" else ui_state
        pairs_status.append(
            {
                "symbol": meta.symbol,
                "display": meta.display,
                "data_ok": bool(dq.get("data_ok")),
                "executable": bool(dq.get("executable")),
                "lag_seconds": q.get("lag_seconds"),
                "tick_time_lag_s": q.get("tick_time_lag_s"),
                "update_age_s": q.get("update_age_s"),
                "mid": q.get("mid"),
                "data_class": q.get("data_class"),
                "source": q.get("source"),
                "feed_state": q.get("feed_state"),
                "claims_realtime": bool(q.get("claims_realtime")),
                "is_synthetic": bool(q.get("is_synthetic")),
                "candles_counts": snap.get("candles_counts"),
                "candle_source": snap.get("candle_source"),
                "state": ui_state,
                "state_label": {
                    "disponivel": "Disponível",
                    "a_atualizar": "A atualizar",
                    "atrasado": "Atrasado",
                    "indisponivel": "Indisponível",
                }.get(ui_state, ui_state),
                "refresh_message": q.get("message") or refresh.get("message") or "",
            }
        )
        score, reasons, reject_code = _triage_score(snap)
        # Dados válidos entram sempre na comparação IA; a triagem só ordena prioridade.
        if dq.get("data_ok") and not q.get("is_synthetic"):
            all_valid.append(
                RadarCandidate(
                    symbol=meta.symbol,
                    display=meta.display,
                    score=score,
                    reasons=reasons,
                    snapshot=snap,
                )
            )
        if reject_code or score <= 0:
            rejected.append(
                {
                    "symbol": meta.symbol,
                    "reason": "; ".join(reasons) or reject_code or "rejeitado",
                    "score": score,
                    "is_synthetic": bool(q.get("is_synthetic")),
                }
            )
            continue
        candidates.append(
            RadarCandidate(
                symbol=meta.symbol,
                display=meta.display,
                score=score,
                reasons=reasons,
                snapshot=snap,
            )
        )

    all_valid.sort(key=lambda c: c.score, reverse=True)
    candidates.sort(key=lambda c: c.score, reverse=True)
    top = candidates[:top_n]
    # Não meter no "rejected" os pares válidos fora do top — a IA deve vê-los todos.

    # Motor quantitativo: reutiliza snapshots já obtidos (sem custo OpenAI).
    directional: dict[str, Any] = {}
    try:
        from app.robot.directional_engine import directional_from_snapshots

        snaps = [c.snapshot for c in all_valid] or [
            build_pair_snapshot(db, pair_meta(s).symbol) for s in watch
        ]
        directional = directional_from_snapshots(snaps, top_n=top_n)
        # Reordenar candidatos do radar pelo EV da melhor oportunidade do par
        # (mantém score de triagem; só ajusta prioridade se houver EV positivo).
        ev_by_sym: dict[str, float] = {}
        for v in directional.get("pair_views") or []:
            ev_by_sym[str(v.get("symbol"))] = float(v.get("best_expected_net_usd") or 0.0)
        if any(v > 0 for v in ev_by_sym.values()):
            candidates.sort(
                key=lambda c: (ev_by_sym.get(c.symbol, -999.0), c.score),
                reverse=True,
            )
            top = candidates[:top_n]
            # Preferir top oportunidades direcionais como candidatos detalhados.
            dir_syms = [
                str(o.get("symbol"))
                for o in (directional.get("top_opportunities") or [])
                if o.get("symbol")
            ]
            if dir_syms:
                by_sym = {c.symbol: c for c in all_valid}
                rebuilt: list[RadarCandidate] = []
                for s in dir_syms:
                    if s in by_sym and by_sym[s] not in rebuilt:
                        rebuilt.append(by_sym[s])
                for c in candidates:
                    if c not in rebuilt:
                        rebuilt.append(c)
                top = rebuilt[:top_n]
    except Exception:  # noqa: BLE001
        directional = {}

    return RadarScan(
        monitored=len(watch),
        with_valid_data=valid,
        candidates=top,
        rejected=rejected,
        pairs=pairs_status,
        all_valid=all_valid,
        directional=directional,
    )
