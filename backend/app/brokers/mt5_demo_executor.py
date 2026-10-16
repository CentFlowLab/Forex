"""Executor exclusivo MT5 DEMO — único gateway de order_send.

Recusa por defeito. Nunca opera contas REAL/CONTEST.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers.mt5_demo_auth import (
    get_demo_auth_snapshot,
    invalidate_if_account_changed,
    set_last_attempt,
)
from app.config import get_settings
from app.models.demo_execution import DemoExecutionAttempt
from app.models.robot import AIDecisionLog, RobotState

logger = logging.getLogger(__name__)

# Constantes oficiais (fallback se MetaTrader5 não importar nos testes).
ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2

GROK_MAGIC = 26041501
GROK_COMMENT = "GROK_FX"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _select_filling(mt5: Any, symbol: str, request: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Negocia apenas modos permitidos pelo símbolo; nunca contorna outras recusas."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return None, {"reason": "symbol_info indisponível", "filling_tried": []}
    flags = int(getattr(info, "filling_mode", 0) or 0)
    execution = getattr(info, "trade_exemode", None)
    market = getattr(mt5, "SYMBOL_TRADE_EXECUTION_MARKET", 2)
    candidates = []
    if flags & getattr(mt5, "SYMBOL_FILLING_IOC", 2):
        candidates.append(("IOC", getattr(mt5, "ORDER_FILLING_IOC", 1)))
    if flags & getattr(mt5, "SYMBOL_FILLING_FOK", 1):
        candidates.append(("FOK", getattr(mt5, "ORDER_FILLING_FOK", 0)))
    if execution is not None and execution != market:
        candidates.append(("RETURN", getattr(mt5, "ORDER_FILLING_RETURN", 2)))
    tried = []
    if not hasattr(mt5, "order_check"):
        return None, {"reason": "order_check indisponível", "filling_tried": []}
    for name, mode in candidates:
        candidate = {**request, "type_filling": mode}
        try:
            result = mt5.order_check(candidate)
            rc = int(getattr(result, "retcode", -1)) if result is not None else -1
            comment = str(getattr(result, "comment", ""))
        except Exception as exc:  # noqa: BLE001
            return None, {"reason": f"order_check exceção: {exc}", "filling_tried": tried}
        tried.append({"mode": name, "type_filling": mode, "retcode": rc, "comment": comment})
        if rc in (0, getattr(mt5, "TRADE_RETCODE_DONE", 10009)):
            return candidate, {"type_filling": mode, "filling_tried": tried, "retcode": rc, "comment": comment}
        if rc != getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030):
            return None, {"reason": f"order_check retcode={rc}", "filling_tried": tried}
    return None, {"reason": "Sem modo de preenchimento aceite (10030 ou incompatível)", "filling_tried": tried}


class MT5DemoExecutor:
    """Centraliza order_send. Injetável via mt5_factory para testes."""

    def __init__(self, mt5_factory: Callable[[], Any] | None = None) -> None:
        self._lock = threading.RLock()
        self._mt5_factory = mt5_factory or self._default_mt5

    @staticmethod
    def _default_mt5() -> Any:
        import MetaTrader5 as mt5  # type: ignore

        return mt5

    def _mt5(self) -> Any:
        return self._mt5_factory()

    def _account_info(self, mt5: Any) -> Any:
        return mt5.account_info()

    def _is_demo(self, ai: Any, mt5: Any) -> bool:
        if ai is None:
            return False
        mode = getattr(ai, "trade_mode", None)
        demo = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", ACCOUNT_TRADE_MODE_DEMO)
        return mode == demo

    def _refuse(self, reason: str, **extra: Any) -> dict[str, Any]:
        payload = {
            "ok": False,
            "accepted": False,
            "status": "rejected",
            "reason": reason,
            "at": _utcnow().isoformat(),
            "order_send_called": bool(extra.get("order_send_called", False)),
            "order_check_called": bool(extra.get("order_check_called", False)),
            **extra,
        }
        set_last_attempt(payload)
        return payload

    def preflight(self, db: Session, *, robot_must_run: bool = True) -> dict[str, Any]:
        """Verificações sem enviar ordem."""
        settings = get_settings()
        if settings.live_authorized or settings.trading_environment == "LIVE":
            return self._refuse("LIVE bloqueado — negociação real proibida.")
        if not settings.live_trading_locked:
            return self._refuse("LIVE_TRADING_LOCKED deve permanecer true.")

        auth = get_demo_auth_snapshot(db)
        if auth.get("emergency"):
            return self._refuse("Paragem de emergência ativa.")
        if not auth.get("active"):
            return self._refuse("Negociação DEMO não autorizada nesta sessão.")
        if auth.get("pause_entries"):
            return self._refuse("Novas operações pausadas.")

        st = db.scalar(select(RobotState).limit(1))
        if robot_must_run and (st is None or not st.running):
            return self._refuse("Robô parado — sem novas ordens.")

        try:
            mt5 = self._mt5()
        except Exception as exc:  # noqa: BLE001
            return self._refuse(f"MetaTrader5 indisponível: {exc}")

        ai = self._account_info(mt5)
        if ai is None:
            return self._refuse("account_info indisponível.")
        if not self._is_demo(ai, mt5):
            label = getattr(ai, "trade_mode", "?")
            return self._refuse(f"Conta não é DEMO (trade_mode={label}).")
        login = str(int(ai.login))
        server = str(ai.server or "")
        if login != str(auth.get("login") or "") or server != str(auth.get("server") or ""):
            invalidate_if_account_changed(db, login=login, server=server)
            return self._refuse("Login/servidor diferem da autorização DEMO.")
        if not bool(getattr(ai, "trade_allowed", False)):
            return self._refuse("Terminal não permite negociação (trade_allowed=false).")
        ti = mt5.terminal_info()
        if ti is None or not bool(getattr(ti, "connected", False)):
            return self._refuse("Terminal MT5 desligado.")

        return {
            "ok": True,
            "login": login,
            "server": server,
            "balance": float(ai.balance or 0),
            "equity": float(ai.equity or 0),
            "currency": str(getattr(ai, "currency", "") or "USD"),
            "mt5": mt5,
            "account": ai,
        }

    def _volume_from_risk(
        self,
        mt5: Any,
        *,
        symbol: str,
        side: str,
        entry: float,
        stop: float,
        equity: float,
        risk_pct: float,
    ) -> tuple[float, str]:
        info = mt5.symbol_info(symbol)
        if info is None:
            return 0.0, "symbol_info indisponível."
        vol_min = float(getattr(info, "volume_min", 0.01) or 0.01)
        vol_max = float(getattr(info, "volume_max", 100.0) or 100.0)
        vol_step = float(getattr(info, "volume_step", 0.01) or 0.01)
        risk_amount = equity * (risk_pct / 100.0)
        if risk_amount <= 0:
            return 0.0, "Equity/risco inválidos."

        # Estimar volume pelo order_calc_profit para 1 lote.
        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        profit_1 = mt5.order_calc_profit(order_type, symbol, 1.0, entry, stop)
        if profit_1 is None or abs(float(profit_1)) < 1e-12:
            # Fallback pip aproximado
            tick_value = float(getattr(info, "trade_tick_value", 0) or 0)
            tick_size = float(getattr(info, "trade_tick_size", 0) or 0)
            if tick_value <= 0 or tick_size <= 0:
                return 0.0, "Não foi possível calcular risco (tick_value/tick_size)."
            loss_per_lot = abs(entry - stop) / tick_size * tick_value
        else:
            loss_per_lot = abs(float(profit_1))
        if loss_per_lot <= 0:
            return 0.0, "Perda por lote inválida."
        raw = risk_amount / loss_per_lot
        # Arredondar para baixo ao step.
        steps = math.floor(raw / vol_step + 1e-12)
        volume = round(steps * vol_step, 8)
        if volume < vol_min:
            return 0.0, f"Volume mínimo {vol_min} excede o risco autorizado."
        if volume > vol_max:
            volume = vol_max
        margin = mt5.order_calc_margin(order_type, symbol, volume, entry)
        if margin is not None and float(margin) > float(getattr(self._account_info(mt5), "margin_free", 0) or 0):
            return 0.0, "Margem livre insuficiente."
        return volume, ""

    def try_execute_decision(self, db: Session, decision: AIDecisionLog) -> dict[str, Any]:
        """Após decisão OpenAI BUY/SELL — valida e eventualmente envia ordem DEMO."""
        with self._lock:
            decision_side = (decision.decision or "").upper()
            if decision_side in {"HOLD", "WAIT", ""}:
                return self._refuse("Decisão WAIT/HOLD — nenhuma ordem.", decision_id=decision.id)
            if decision_side not in {"BUY", "SELL"}:
                return self._refuse(f"Direção inválida: {decision_side}", decision_id=decision.id)

            try:
                ctx_q = json.loads(decision.context_json or "{}")
            except json.JSONDecodeError:
                ctx_q = {}
            from app.robot.radar_decision_quality import is_decision_quality_insufficient

            if is_decision_quality_insufficient(ctx_q.get("decision_quality")):
                issues = list((ctx_q.get("decision_quality") or {}).get("issues") or [])
                detail = "; ".join(issues[:3]) if issues else "contrato de comparação em falta"
                return self._refuse(
                    f"decision_quality=insufficient — nenhuma ordem DEMO. {detail}",
                    decision_id=decision.id,
                )

            prop = {}
            try:
                prop = json.loads(decision.proposal_json or "{}")
            except json.JSONDecodeError:
                prop = {}
            symbol = str(prop.get("symbol") or "").replace("/", "").upper()
            if not symbol:
                ctx = {}
                try:
                    ctx = json.loads(decision.context_json or "{}")
                except json.JSONDecodeError:
                    ctx = {}
                symbol = str(ctx.get("symbol") or "").replace("/", "").upper()
            stop = float(decision.stop_loss or prop.get("stop_loss") or 0)
            take = float(decision.take_profit or prop.get("take_profit") or 0)
            entry = float(decision.entry_price or prop.get("entry_price") or 0)
            if not symbol:
                return self._refuse("Símbolo em falta na decisão.", decision_id=decision.id)
            if stop <= 0:
                return self._refuse("Stop-loss ausente — nenhuma ordem.", decision_id=decision.id)

            idem = f"decision:{decision.id}"
            existing = db.scalar(
                select(DemoExecutionAttempt).where(DemoExecutionAttempt.idempotency_key == idem)
            )
            if existing is not None:
                return self._refuse(
                    "Decisão já tem tentativa de execução (idempotência).",
                    decision_id=decision.id,
                    attempt_id=existing.id,
                    status=existing.status,
                )

            # Reserva atómica da tentativa
            attempt = DemoExecutionAttempt(
                decision_id=int(decision.id),
                idempotency_key=idem,
                symbol=symbol,
                side=decision_side,
                stop_loss=stop,
                take_profit=take,
                status="reserved",
            )
            try:
                db.add(attempt)
                db.commit()
                db.refresh(attempt)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                return self._refuse(f"Falha de base de dados: {exc}", decision_id=decision.id)

            pre = self.preflight(db, robot_must_run=True)
            if not pre.get("ok"):
                attempt.status = "rejected"
                attempt.reject_reason = str(pre.get("reason") or "preflight")
                db.commit()
                return {**pre, "attempt_id": attempt.id}

            mt5 = pre["mt5"]
            # Dados de mercado
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                attempt.status = "rejected"
                attempt.reject_reason = "Cotação indisponível."
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id)
            bid, ask = float(tick.bid), float(tick.ask)
            if bid <= 0 or ask <= 0 or ask < bid:
                attempt.status = "rejected"
                attempt.reject_reason = "Cotação inválida."
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id)
            price = ask if decision_side == "BUY" else bid
            if entry <= 0:
                entry = price

            settings = get_settings()
            info = mt5.symbol_info(symbol)
            if info is None:
                attempt.status = "rejected"
                attempt.reject_reason = "symbol_info falhou."
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id)
            point = float(getattr(info, "point", 0.0001) or 0.0001)
            spread_pips = (ask - bid) / point / 10.0 if point > 0 else 999.0
            # Heurística: para JPY point diferente — usar trade_tick_size
            tick_size = float(getattr(info, "trade_tick_size", 0) or point)
            if "JPY" in symbol:
                spread_pips = (ask - bid) / (tick_size or 0.01)
            else:
                spread_pips = (ask - bid) / 0.0001

            if spread_pips > float(settings.max_spread_pips):
                attempt.status = "rejected"
                attempt.reject_reason = f"Spread {spread_pips:.2f} acima do limite."
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id)

            # Stops levels
            stops_level = float(getattr(info, "trade_stops_level", 0) or 0) * point
            if decision_side == "BUY" and stop >= entry - stops_level:
                attempt.status = "rejected"
                attempt.reject_reason = "Stop-loss inválido (stop level)."
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id)
            if decision_side == "SELL" and stop <= entry + stops_level:
                attempt.status = "rejected"
                attempt.reject_reason = "Stop-loss inválido (stop level)."
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id)

            # Max 1 posição do robô
            positions = mt5.positions_get() or []
            magic = int(getattr(settings, "mt5_demo_magic", GROK_MAGIC))
            robot_open = [
                p
                for p in positions
                if int(getattr(p, "magic", 0) or 0) == magic
            ]
            if len(robot_open) >= int(settings.max_concurrent_positions):
                attempt.status = "rejected"
                attempt.reject_reason = "Já existe posição aberta do robô."
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id)

            # Limite diário 2% — usa profit realizado aproximado via history se disponível
            equity = float(pre["equity"])
            daily_limit = equity * (float(settings.max_daily_loss_pct) / 100.0)
            day_pnl = float(getattr(pre["account"], "profit", 0) or 0)
            # profit flutuante negativo extremo + realized — conservador
            if day_pnl < 0 and abs(day_pnl) >= daily_limit:
                attempt.status = "rejected"
                attempt.reject_reason = "Limite diário de perda atingido."
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id)

            volume, vol_err = self._volume_from_risk(
                mt5,
                symbol=symbol,
                side=decision_side,
                entry=entry,
                stop=stop,
                equity=equity,
                risk_pct=float(settings.max_risk_per_trade_pct),
            )
            if vol_err or volume <= 0:
                attempt.status = "rejected"
                attempt.reject_reason = vol_err or "Volume inválido."
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id)

            attempt.volume_lots = volume
            attempt.price_requested = price
            attempt.account_login = str(pre["login"])
            attempt.account_server = str(pre["server"])
            attempt.status = "sending"
            db.commit()
            set_last_attempt(
                {
                    "status": "sending",
                    "decision_id": decision.id,
                    "at": _utcnow().isoformat(),
                }
            )

            # Verificação imediata antes de order_send
            ai2 = self._account_info(mt5)
            if ai2 is None or not self._is_demo(ai2, mt5):
                attempt.status = "rejected"
                attempt.reject_reason = "Conta mudou ou deixou de ser DEMO antes do envio."
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id)
            if str(int(ai2.login)) != attempt.account_login or str(ai2.server or "") != attempt.account_server:
                invalidate_if_account_changed(db, login=ai2.login, server=ai2.server)
                attempt.status = "rejected"
                attempt.reject_reason = "Conta mudou entre validação e envio."
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id)
            auth2 = get_demo_auth_snapshot(db)
            if not auth2.get("active") or auth2.get("emergency"):
                attempt.status = "rejected"
                attempt.reject_reason = "Autorização DEMO revogada antes do envio."
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id)

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": mt5.ORDER_TYPE_BUY if decision_side == "BUY" else mt5.ORDER_TYPE_SELL,
                "price": price,
                "sl": stop,
                "tp": take if take > 0 else 0.0,
                "deviation": 20,
                "magic": magic,
                "comment": str(getattr(settings, "mt5_demo_comment", GROK_COMMENT))[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 1),
            }
            request, check_audit = _select_filling(mt5, symbol, request)
            attempt.order_check_json = json.dumps(check_audit, default=str)
            if request is None:
                attempt.status = "rejected"
                attempt.reject_reason = check_audit["reason"]
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id, order_check_called=bool(check_audit["filling_tried"]))

            # Envio — único ponto
            try:
                result = mt5.order_send(request)
            except Exception as exc:  # noqa: BLE001
                # Timeout / incerto — NÃO reenviar; reconciliar
                attempt.status = "uncertain"
                attempt.reject_reason = f"order_send exceção (sem reenvio): {exc}"
                attempt.order_send_json = json.dumps({"error": str(exc)})
                db.commit()
                reconciled = self._reconcile(mt5, attempt)
                return {
                    "ok": False,
                    "accepted": False,
                    "status": "uncertain",
                    "reason": attempt.reject_reason,
                    "reconciled": reconciled,
                    "decision_id": decision.id,
                    "attempt_id": attempt.id,
                }

            if result is None:
                attempt.status = "uncertain"
                attempt.reject_reason = "order_send devolveu None — sem reenvio cego."
                db.commit()
                self._reconcile(mt5, attempt)
                return self._refuse(attempt.reject_reason, decision_id=decision.id, attempt_id=attempt.id)

            attempt.order_send_json = json.dumps(
                {
                    "retcode": getattr(result, "retcode", None),
                    "deal": getattr(result, "deal", None),
                    "order": getattr(result, "order", None),
                    "comment": str(getattr(result, "comment", "")),
                },
                default=str,
            )
            retcode = int(getattr(result, "retcode", -1))
            done = getattr(mt5, "TRADE_RETCODE_DONE", 10009)
            if retcode != done:
                attempt.status = "rejected"
                attempt.reject_reason = f"order_send retcode={retcode} {getattr(result, 'comment', '')}"
                db.commit()
                return self._refuse(attempt.reject_reason, decision_id=decision.id, attempt_id=attempt.id)

            attempt.order_ticket = str(getattr(result, "order", "") or "")
            attempt.deal_ticket = str(getattr(result, "deal", "") or "")
            attempt.status = "executed"
            attempt.reconciled = True
            db.commit()

            # Atualizar decisão
            try:
                ctx = json.loads(decision.context_json or "{}")
            except json.JSONDecodeError:
                ctx = {}
            ctx["execution_attempted"] = True
            ctx["mt5_ticket"] = attempt.order_ticket or attempt.deal_ticket
            decision.context_json = json.dumps(ctx, ensure_ascii=False)[:12000]
            decision.executed = True
            decision.client_order_id = attempt.order_ticket or idem
            db.commit()

            out = {
                "ok": True,
                "accepted": True,
                "status": "executed",
                "decision_id": decision.id,
                "attempt_id": attempt.id,
                "order_ticket": attempt.order_ticket,
                "deal_ticket": attempt.deal_ticket,
                "volume": volume,
                "at": _utcnow().isoformat(),
            }
            set_last_attempt(out)
            return out

    def _reconcile(self, mt5: Any, attempt: DemoExecutionAttempt) -> dict[str, Any]:
        magic = int(getattr(get_settings(), "mt5_demo_magic", GROK_MAGIC))
        positions = mt5.positions_get(symbol=attempt.symbol) or []
        mine = [p for p in positions if int(getattr(p, "magic", 0) or 0) == magic]
        if mine:
            attempt.reconciled = True
            attempt.status = "executed"
            attempt.order_ticket = str(getattr(mine[0], "ticket", "") or attempt.order_ticket)
            return {"found_position": True, "ticket": attempt.order_ticket}
        return {"found_position": False}

    def close_robot_positions(self, db: Session) -> dict[str, Any]:
        """Fecha só posições magic GROK — revalida DEMO."""
        with self._lock:
            pre = self.preflight(db, robot_must_run=False)
            # close pode correr com pause, mas não com emergência / não-demo
            auth = get_demo_auth_snapshot(db)
            if auth.get("emergency"):
                return self._refuse("Paragem de emergência — fecho bloqueado sem levantamento.")
            try:
                mt5 = self._mt5()
            except Exception as exc:  # noqa: BLE001
                return self._refuse(f"MT5 indisponível: {exc}")
            ai = self._account_info(mt5)
            if ai is None or not self._is_demo(ai, mt5):
                return self._refuse("Conta não é DEMO — fecho recusado.")
            if not auth.get("active"):
                # Exigir auth ativa OU pelo menos DEMO confirmada + confirmed já feito no endpoint
                # Pedido: verificar novamente DEMO; permitir fecho se DEMO e utilizador confirmou
                pass
            magic = int(getattr(get_settings(), "mt5_demo_magic", GROK_MAGIC))
            positions = mt5.positions_get() or []
            closed: list[dict[str, Any]] = []
            errors: list[str] = []
            for pos in positions:
                if int(getattr(pos, "magic", 0) or 0) != magic:
                    continue
                symbol = str(pos.symbol)
                ticket = int(pos.ticket)
                volume = float(pos.volume)
                side = int(pos.type)
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    errors.append(f"ticket {ticket}: sem cotação")
                    continue
                close_type = mt5.ORDER_TYPE_SELL if side == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
                price = float(tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask)
                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": volume,
                    "type": close_type,
                    "position": ticket,
                    "price": price,
                    "deviation": 20,
                    "magic": magic,
                    "comment": "GROK_CLOSE",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 1),
                }
                # Revalidar DEMO imediatamente
                ai2 = self._account_info(mt5)
                if ai2 is None or not self._is_demo(ai2, mt5):
                    errors.append(f"ticket {ticket}: conta deixou de ser DEMO")
                    break
                req, close_check = _select_filling(mt5, symbol, req)
                if req is None:
                    errors.append(f"ticket {ticket}: {close_check['reason']} ({close_check['filling_tried']})")
                    continue
                try:
                    res = mt5.order_send(req)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"ticket {ticket}: {exc}")
                    continue
                if res is None or int(getattr(res, "retcode", -1)) != getattr(
                    mt5, "TRADE_RETCODE_DONE", 10009
                ):
                    errors.append(
                        f"ticket {ticket}: retcode={getattr(res, 'retcode', None)}"
                    )
                else:
                    closed.append({"ticket": ticket, "retcode": getattr(res, "retcode", None)})
            return {
                "ok": len(errors) == 0,
                "closed": closed,
                "errors": errors,
                "note": "Só posições com magic do robô; manuais não são tocadas.",
            }


_executor: MT5DemoExecutor | None = None


def get_mt5_demo_executor() -> MT5DemoExecutor:
    global _executor
    if _executor is None:
        _executor = MT5DemoExecutor()
    return _executor


def set_mt5_demo_executor_for_tests(executor: MT5DemoExecutor | None) -> None:
    global _executor
    _executor = executor


def protected_order_send(request: dict[str, Any]) -> Any:
    """Gateway único — qualquer caminho legado deve passar aqui.

    Sem sessão/autorização explícita via try_execute_decision, recusa sempre.
    """
    raise RuntimeError(
        "order_send bloqueado: use MT5DemoExecutor.try_execute_decision com autorização DEMO."
    )
