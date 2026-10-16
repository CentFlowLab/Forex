import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  api,
  type Candle,
  type OpenAIRealDecision,
  type RobotDemoAuth,
  type RobotStatus,
} from "../api/client";
import { CandleChart } from "../components/CandleChart";
import { formatDateTime, formatMoney, formatNumber, formatPrice } from "../i18n/format";

function decisionClass(d: string | null | undefined) {
  if (d === "BUY") return "text-accent-buy";
  if (d === "SELL") return "text-accent-sell";
  return "text-accent-hold";
}

const NA = "Não indicado pela IA";

function fieldOrNa(value: string | null | undefined) {
  const v = (value || "").trim();
  return v || NA;
}

function chipClass(code: string | undefined) {
  if (!code) return "bg-white/5 text-slate-300";
  if (
    [
      "monitoring",
      "demo_connected",
      "decision_received",
      "active",
      "order_executed",
      "ok",
    ].includes(code)
  ) {
    return "bg-emerald-500/15 text-emerald-300";
  }
  if (
    [
      "waiting_next",
      "analyzing",
      "ready",
      "order_sending",
      "stale_data",
      "active_with_rejection",
    ].includes(code)
  ) {
    return "bg-amber-500/15 text-amber-200";
  }
  if (
    [
      "error",
      "account_not_authorized",
      "budget_exhausted",
      "order_rejected",
      "paused_safety",
    ].includes(code)
  ) {
    return "bg-accent-sell/15 text-accent-sell";
  }
  return "bg-white/5 text-slate-300";
}

export function Robot() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<RobotStatus | null>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [candleMeta, setCandleMeta] = useState<{
    source?: string;
    label?: string;
    is_synthetic?: boolean;
  }>({});
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [diagOpen, setDiagOpen] = useState(false);
  const [linkLost, setLinkLost] = useState(false);
  const [demo, setDemo] = useState<RobotDemoAuth | null>(null);

  const seqRef = useRef(0);
  const inflightRef = useRef(false);
  const failRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const applyStatus = useCallback((s: RobotStatus, c: { candles?: Candle[]; source?: string; label?: string; is_synthetic?: boolean }) => {
    setStatus(s);
    setCandles(c.candles || []);
    setCandleMeta({
      source: c.source,
      label: c.label,
      is_synthetic: c.is_synthetic,
    });
    if (s.demo_auth) setDemo(s.demo_auth);
    if (s.openai_autonomous && "error" in s.openai_autonomous && s.openai_autonomous.error) {
      setErr(String(s.openai_autonomous.error));
    }
  }, []);

  const load = useCallback(async (force = false) => {
    if (inflightRef.current && !force) return;
    inflightRef.current = true;
    const seq = ++seqRef.current;
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const [s, c] = await Promise.all([api.robotStatus(), api.candles("M15")]);
      if (seq !== seqRef.current) return; // resposta fora de ordem
      failRef.current = 0;
      setLinkLost(false);
      applyStatus(s, c);
    } catch (e) {
      if (ac.signal.aborted) return;
      failRef.current += 1;
      if (failRef.current >= 2) setLinkLost(true);
      setErr(`Atualização falhou: ${String(e)}`);
    } finally {
      if (seq === seqRef.current) inflightRef.current = false;
    }
  }, [applyStatus]);

  useEffect(() => {
    void load(true);
    const id = window.setInterval(() => {
      void load(false);
    }, 3000);
    return () => {
      window.clearInterval(id);
      abortRef.current?.abort();
    };
  }, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setErr("");
    try {
      await fn();
      await load(true);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!status) {
    return (
      <div className="space-y-3 py-16 text-center">
        {linkLost || failRef.current >= 2 ? (
          <div className="text-sm text-accent-sell">Ligação perdida com o backend</div>
        ) : null}
        <div className="text-slate-400">{err || t("app.loading")}</div>
      </div>
    );
  }

  const mt5Acc = status.account_mt5;
  const why = status.why_not_trading;
  const openaiReal = status.openai_real;
  const latest: OpenAIRealDecision | undefined = openaiReal?.latest;
  const hasOpenAI = Boolean(openaiReal?.available && latest?.available);
  const ts = status.timestamps || {};
  const currency = mt5Acc?.currency || status.capital?.currency || "USD";
  const dir = status.directional_predictions;
  const dirOpps = dir?.top_opportunities || [];
  const dirBuys = dir?.top_buys || [];
  const dirSells = dir?.top_sells || [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">{t("robot.title")}</h1>
          <p className="mt-1 text-sm text-slate-400">{status.disclaimer}</p>
          <p className="mt-1 text-[11px] text-slate-500">
            Última atualização do painel:{" "}
            {ts.panel_generated_at || status.panel_generated_at
              ? formatDateTime(String(ts.panel_generated_at || status.panel_generated_at), "UTC") +
                " UTC"
              : "—"}
            {" · "}
            Backend: {linkLost ? "Ligação perdida" : status.backend_link?.label || "Ligação OK"}
          </p>
        </div>
        <button
          className={`rounded-lg px-5 py-2.5 text-sm font-semibold ${
            status.running
              ? "bg-accent-sell/20 text-accent-sell"
              : "bg-accent-gold/20 text-accent-gold"
          }`}
          disabled={busy}
          onClick={() => act(() => (status.running ? api.robotStop() : api.robotStart()))}
        >
          {status.running ? t("robot.stop") : t("robot.start")}
        </button>
      </div>

      {linkLost && (
        <div className="rounded-lg border border-accent-sell/40 bg-accent-sell/10 px-3 py-2 text-sm text-accent-sell">
          Ligação perdida com o backend — os dados abaixo podem estar desatualizados.
        </div>
      )}
      {err && !linkLost && (
        <div className="rounded-lg border border-accent-sell/30 bg-accent-sell/10 px-3 py-2 text-sm text-accent-sell">
          {err}
        </div>
      )}

      {/* 1. Estados */}
      <section className="panel grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4">
        {(
          [
            ["Robô", status.robot_state],
            ["MetaTrader 5", status.mt5_state],
            ["OpenAI", status.openai_state],
            ["Execução DEMO", status.execution_state],
          ] as const
        ).map(([title, st]) => (
          <div key={title}>
            <div className="text-xs text-slate-500">{title}</div>
            <div
              className={`mt-1 inline-block rounded px-2 py-1 text-sm font-medium ${chipClass(st?.code)}`}
            >
              {st?.label || "—"}
            </div>
            {title === "Execução DEMO" && st?.last_rejection ? (
              <div className="mt-1 text-[11px] text-slate-500">
                {st.last_rejection.stale
                  ? "Última recusa (obsoleta, não é o estado atual)"
                  : "Última recusa"}
                : {st.last_rejection.reason}
                {st.last_rejection.at
                  ? ` · ${formatDateTime(String(st.last_rejection.at), "UTC")} UTC`
                  : ""}
                {st.last_rejection.decision_id != null
                  ? ` · decisão #${st.last_rejection.decision_id}`
                  : ""}
                {st.last_rejection.order_send_called === false
                  ? " · order_send não chamado"
                  : ""}
              </div>
            ) : title === "Execução DEMO" && st?.reason ? (
              <div className="mt-1 text-[11px] text-slate-500">
                Motivo: {st.reason}
                {st.at ? ` · ${formatDateTime(String(st.at), "UTC")} UTC` : ""}
              </div>
            ) : null}
          </div>
        ))}
      </section>

      {/* 2. Conta DEMO */}
      <section className="panel p-4">
        <div className="panel-title mb-2">Conta MT5 DEMO</div>
        {mt5Acc?.available ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 text-sm">
            <div>
              <div className="text-xs text-slate-500">Saldo</div>
              <div className="text-lg text-white">
                {formatMoney(Number(mt5Acc.balance ?? 0), currency)}
              </div>
            </div>
            <div>
              <div className="text-xs text-slate-500">Equity</div>
              <div className="text-lg text-white">
                {formatMoney(Number(mt5Acc.equity ?? 0), currency)}
              </div>
            </div>
            <div>
              <div className="text-xs text-slate-500">Margem / livre</div>
              <div>
                {formatMoney(Number(mt5Acc.margin ?? 0), currency)} /{" "}
                {formatMoney(Number(mt5Acc.margin_free ?? 0), currency)}
              </div>
            </div>
            <div>
              <div className="text-xs text-slate-500">P&L flutuante</div>
              <div
                className={
                  Number(mt5Acc.profit ?? 0) >= 0 ? "text-accent-buy" : "text-accent-sell"
                }
              >
                {formatMoney(Number(mt5Acc.profit ?? 0), currency)}
              </div>
            </div>
            <div className="text-xs text-slate-500 sm:col-span-2 lg:col-span-4">
              Conta {mt5Acc.login_masked || "—"} @ {mt5Acc.server || "—"} · Tipo:{" "}
              {mt5Acc.trade_mode_label || "—"}
            </div>
            {mt5Acc.autotrading_warning ? (
              <div className="mt-2 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-2 text-xs text-amber-200 sm:col-span-2 lg:col-span-4">
                {mt5Acc.autotrading_warning}
              </div>
            ) : null}
          </div>
        ) : (
          <p className="text-sm text-slate-400">
            {mt5Acc?.note || "Terminal MT5 desligado — sem saldo DEMO."}
          </p>
        )}
      </section>

      {/* 3. Posições */}
      <section className="panel p-4">
        <div className="panel-title mb-2">Posições abertas (robô)</div>
        {(status.open_positions || []).length === 0 ? (
          <div className="text-xs text-slate-500">Sem posições do robô.</div>
        ) : (
          (status.open_positions || []).map((p) => (
            <div key={p.id} className="mb-2 text-sm text-slate-300">
              #{p.id} {p.side} · {formatNumber(p.volume)} lotes @ {formatPrice(p.entry)}
              <div className="text-xs text-slate-500">
                SL {formatPrice(p.stop)} · TP {formatPrice(p.take)} · P&L{" "}
                {formatNumber(p.unrealized_pnl)}
              </div>
            </div>
          ))
        )}
      </section>

      {/* 4. Decisão + porque não negoceia */}
      <section className="panel border border-emerald-500/20 p-4">
        <div className="panel-title mb-2 text-emerald-300">Última decisão OpenAI</div>
        {!hasOpenAI ? (
          <p className="text-sm text-slate-400">
            {openaiReal?.message || "Ainda não existe nenhuma decisão OpenAI real persistida."}
          </p>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 text-sm text-slate-300">
            <div>
              <div className="text-xs text-slate-500">Decisão</div>
              <div className={`text-base font-semibold ${decisionClass(latest?.decision)}`}>
                {latest?.decision_pt}
              </div>
            </div>
            <div>
              <div className="text-xs text-slate-500">Par</div>
              <div>{fieldOrNa(latest?.symbol)}</div>
            </div>
            <div>
              <div className="text-xs text-slate-500">ID / hora</div>
              <div>
                #{latest?.id ?? "—"}
                {latest?.analyzed_at
                  ? ` · ${formatDateTime(latest.analyzed_at, "UTC")} UTC`
                  : ""}
              </div>
            </div>
            <div>
              <div className="text-xs text-slate-500">Custo</div>
              <div>
                {latest?.estimated_cost_usd != null
                  ? `$${formatNumber(latest.estimated_cost_usd, { maximumFractionDigits: 4 })}`
                  : NA}
              </div>
            </div>
            <div className="sm:col-span-2 lg:col-span-4">
              <div className="text-xs text-slate-500">Justificação</div>
              <p className="mt-0.5 whitespace-pre-wrap">{fieldOrNa(latest?.justification)}</p>
            </div>
            {latest?.radar_vs_openai ? (
              <div className="sm:col-span-2 lg:col-span-4 mt-1 grid gap-2 rounded-lg border border-slate-600/40 bg-slate-900/40 px-3 py-3 sm:grid-cols-2">
                <div>
                  <div className="text-xs text-slate-500">Melhor candidato do radar</div>
                  <div className="text-sm text-slate-200">
                    {latest.radar_vs_openai.radar_best_candidate
                      ? `${latest.radar_vs_openai.radar_best_candidate}${
                          latest.radar_vs_openai.radar_best_score != null
                            ? ` (score ${latest.radar_vs_openai.radar_best_score})`
                            : ""
                        }`
                      : NA}
                  </div>
                  <div className="mt-0.5 text-[11px] text-slate-500">
                    Ranking local de atenção — não é ordem MT5.
                  </div>
                </div>
                <div>
                  <div className="text-xs text-slate-500">Decisão OpenAI</div>
                  <div className={`text-sm font-semibold ${decisionClass(latest?.decision)}`}>
                    {latest?.decision_pt}
                    {latest.radar_vs_openai.openai_symbol
                      ? ` · ${latest.radar_vs_openai.openai_symbol}`
                      : ""}
                  </div>
                </div>
                <div className="sm:col-span-2">
                  <div className="text-xs text-slate-500">Motivo da discordância</div>
                  <p className="mt-0.5 text-sm text-slate-200 whitespace-pre-wrap">
                    {fieldOrNa(latest.radar_vs_openai.discordance_reason)}
                  </p>
                </div>
                <div className="sm:col-span-2">
                  <div className="text-xs text-slate-500">Condições necessárias para entrada</div>
                  <p className="mt-0.5 text-sm text-slate-200 whitespace-pre-wrap">
                    {fieldOrNa(
                      latest.radar_vs_openai.entry_conditions || latest.entry_conditions,
                    )}
                  </p>
                </div>
                {latest.radar_vs_openai.ai_decision_inconsistent ? (
                  <div className="sm:col-span-2 rounded border border-rose-500/40 bg-rose-500/10 px-2 py-2">
                    <div className="text-xs font-semibold text-rose-300">
                      ai_decision_inconsistent — justificação contradiz o motor
                    </div>
                    <p className="mt-1 text-sm text-slate-100 whitespace-pre-wrap">
                      {fieldOrNa(latest.radar_vs_openai.quality_failure_summary)}
                    </p>
                    <div className="mt-1 text-[11px] text-slate-400">
                      HOLD mantido (não convertido para BUY/SELL). Bloqueio por inconsistência
                      matemática, não por EV≤0 do motor. Sem ordens DEMO.
                    </div>
                  </div>
                ) : latest.radar_vs_openai.analysis_incomplete ? (
                  <div className="sm:col-span-2 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-2">
                    <div className="text-xs font-semibold text-amber-300">
                      Análise incompleta (decision_quality=insufficient)
                    </div>
                    <p className="mt-1 text-sm text-slate-100 whitespace-pre-wrap">
                      {fieldOrNa(latest.radar_vs_openai.quality_failure_summary)}
                    </p>
                    <div className="mt-1 text-[11px] text-slate-400">
                      Resposta original da OpenAI mantida acima — sem 2.ª chamada paga nem
                      execução DEMO.
                    </div>
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        )}
        <div className="mt-4 rounded-lg border border-accent-gold/30 bg-accent-gold/10 px-3 py-3">
          <div className="text-xs font-semibold uppercase tracking-wide text-accent-gold">
            Porque não está a negociar?
          </div>
          <p className="mt-1 text-sm text-slate-100">{why?.message || "—"}</p>
          {why?.code ? (
            <div className="mt-1 text-[11px] text-slate-500">
              Código: {why.code}
              {why.decision_id != null ? ` · decisão #${why.decision_id}` : ""}
              {why.decision_at
                ? ` · ${formatDateTime(String(why.decision_at), "UTC")} UTC`
                : ""}
            </div>
          ) : null}
          {why?.prior_order_rejection ? (
            <div className="mt-2 text-[11px] text-slate-400">
              Recusa anterior (separada do WAIT): {why.prior_order_rejection}
            </div>
          ) : null}
        </div>
      </section>

      {/* Previsões direcionais (motor local) */}
      <section className="panel border border-amber-500/20 p-4">
        <div className="panel-title mb-2 text-amber-200">
          {dir?.label || "Previsões direcionais (quantitativo local)"}
        </div>
        <p className="mb-2 text-xs text-slate-400">
          {dir?.disclaimer ||
            "Probabilidades calibradas localmente — a OpenAI não inventa estes números."}
        </p>
        <div className="mb-3 grid gap-2 text-[11px] text-slate-400 sm:grid-cols-3">
          <div>
            Acionáveis (EV&gt;0 + evidência): {dir?.actionable_count ?? "—"}
          </div>
          <div>
            Validação resolvida: {dir?.validation?.n_resolved ?? 0}
            {dir?.validation?.directional_accuracy != null
              ? ` · acerto ${(dir.validation.directional_accuracy * 100).toFixed(0)}%`
              : ""}
          </div>
          <div>Métrica: {dir?.ranking_metric || "expected_net_usd_after_costs"}</div>
        </div>
        <div className="grid gap-3 lg:grid-cols-2">
          <div>
            <div className="mb-1 text-xs text-emerald-300/90">Top compras (EV líquido)</div>
            <ul className="space-y-1 text-xs text-slate-300">
              {(dirBuys.length ? dirBuys : dirOpps.filter((o) => o.side === "BUY"))
                .slice(0, 5)
                .map((o, i) => (
                  <li key={`b-${o.symbol}-${i}`}>
                    {o.symbol} · EV {Number(o.expected_net_usd ?? 0).toFixed(2)}$ /0.1 lote
                  </li>
                ))}
              {!dirBuys.length && !dirOpps.some((o) => o.side === "BUY") ? (
                <li className="text-slate-500">Sem ranking ainda</li>
              ) : null}
            </ul>
          </div>
          <div>
            <div className="mb-1 text-xs text-rose-300/90">Top vendas (EV líquido)</div>
            <ul className="space-y-1 text-xs text-slate-300">
              {(dirSells.length ? dirSells : dirOpps.filter((o) => o.side === "SELL"))
                .slice(0, 5)
                .map((o, i) => (
                  <li key={`s-${o.symbol}-${i}`}>
                    {o.symbol} · EV {Number(o.expected_net_usd ?? 0).toFixed(2)}$ /0.1 lote
                  </li>
                ))}
              {!dirSells.length && !dirOpps.some((o) => o.side === "SELL") ? (
                <li className="text-slate-500">Sem ranking ainda</li>
              ) : null}
            </ul>
          </div>
        </div>
        {dirOpps.length > 0 ? (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[640px] text-left text-[11px] text-slate-300">
              <thead className="text-slate-500">
                <tr>
                  <th className="py-1 pr-2">Par</th>
                  <th className="py-1 pr-2">Lado</th>
                  <th className="py-1 pr-2">P↑/P↓/P↔</th>
                  <th className="py-1 pr-2">Mov. esp.</th>
                  <th className="py-1 pr-2">Entrada / SL / TP</th>
                  <th className="py-1 pr-2">EV$</th>
                  <th className="py-1">Evidência</th>
                </tr>
              </thead>
              <tbody>
                {dirOpps.slice(0, 5).map((o, i) => {
                  const f30 =
                    (o.forecasts || []).find((f) => f.horizon_min === 30) ||
                    (o.forecasts || [])[0];
                  return (
                    <tr key={`opp-${o.symbol}-${o.side}-${i}`} className="border-t border-white/5">
                      <td className="py-1 pr-2">{o.symbol}</td>
                      <td className="py-1 pr-2">{o.side}</td>
                      <td className="py-1 pr-2">
                        {f30
                          ? `${((f30.p_up ?? 0) * 100).toFixed(0)}/${((f30.p_down ?? 0) * 100).toFixed(0)}/${((f30.p_flat ?? 0) * 100).toFixed(0)}`
                          : "—"}
                      </td>
                      <td className="py-1 pr-2">
                        {f30?.expected_move_pips != null
                          ? `${Number(f30.expected_move_pips).toFixed(1)} pips`
                          : "—"}
                      </td>
                      <td className="py-1 pr-2">
                        {o.entry}/{o.stop_loss}/{o.take_profit}
                      </td>
                      <td className="py-1 pr-2">{Number(o.expected_net_usd ?? 0).toFixed(2)}</td>
                      <td className="py-1">
                        {o.evidence_quality != null
                          ? Number(o.evidence_quality).toFixed(2)
                          : "—"}
                        {(o.reject_reasons || []).length
                          ? ` · ${(o.reject_reasons || [])[0]}`
                          : ""}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="mt-2 text-xs text-slate-500">
            Aguardando scan do motor quantitativo (sem custo OpenAI).
          </p>
        )}
        {(dir?.pair_directions || []).length > 0 ? (
          <details className="mt-3">
            <summary className="cursor-pointer text-xs text-slate-400">
              Direção mais provável por par ({dir?.pair_directions?.length})
            </summary>
            <ul className="mt-2 grid gap-1 text-[11px] text-slate-400 sm:grid-cols-2 lg:grid-cols-4">
              {(dir?.pair_directions || []).map((p) => (
                <li key={p.symbol}>
                  {p.display || p.symbol}: {p.most_likely_direction}
                  {p.best_side ? ` · melhor ${p.best_side}` : ""}
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </section>

      {/* Negociação DEMO */}
      <section className="panel border border-sky-500/20 p-4">
        <div className="panel-title mb-2 text-sky-300">Negociação DEMO</div>
        <p className="mb-2 text-xs text-slate-400">
          Independente da OpenAI. Após reinício da API é necessário confirmar de novo. Contas REAL
          e CONTEST são sempre recusadas.
        </p>
        <div className="mb-3 grid gap-2 text-xs text-slate-400 sm:grid-cols-2">
          <div>
            Conta: {demo?.login_masked || mt5Acc?.login_masked || "—"} @{" "}
            {demo?.server || mt5Acc?.server || "—"}
          </div>
          <div>Tipo: {mt5Acc?.trade_mode_label || "—"}</div>
          <div>
            Autorização DEMO:{" "}
            {demo?.active || status.demo_auth?.active ? "ativa nesta sessão" : "inativa"}
          </div>
          <div>
            Emergência: {demo?.emergency || status.demo_auth?.emergency ? "SIM" : "não"}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            className="rounded-lg bg-sky-500/20 px-3 py-1.5 text-xs text-sky-200 disabled:opacity-40"
            disabled={busy || Boolean(demo?.active || status.demo_auth?.active)}
            onClick={() => {
              const login = mt5Acc?.login;
              const server = mt5Acc?.server;
              if (
                !window.confirm(
                  "Ativar negociação DEMO?\n\n" +
                    `Conta: ${login} @ ${server}\n` +
                    "O robô poderá abrir e fechar operações com fundos virtuais desta conta DEMO.\n" +
                    "Negociação real permanece bloqueada.",
                )
              ) {
                return;
              }
              void act(async () => {
                const r = await api.robotDemoActivate({ confirmed: true });
                setDemo(r);
              });
            }}
          >
            Ativar negociação DEMO
          </button>
          <button
            className="rounded-lg bg-white/10 px-3 py-1.5 text-xs disabled:opacity-40"
            disabled={busy}
            onClick={() =>
              act(async () => {
                const r = await api.robotDemoPause(true);
                setDemo(r);
              })
            }
          >
            Pausar novas operações
          </button>
          <button
            className="rounded-lg bg-accent-sell/20 px-3 py-1.5 text-xs text-accent-sell disabled:opacity-40"
            disabled={busy}
            onClick={() => {
              if (
                !window.confirm(
                  "Paragem de emergência?\n\nBloqueia imediatamente novas ordens e cancela a autorização DEMO. O bloqueio mantém-se após reinício.",
                )
              ) {
                return;
              }
              void act(async () => {
                const r = await api.robotDemoEmergency();
                setDemo(r);
              });
            }}
          >
            Paragem de emergência
          </button>
          <button
            className="rounded-lg bg-accent-gold/20 px-3 py-1.5 text-xs text-accent-gold disabled:opacity-40"
            disabled={busy}
            onClick={() => {
              if (
                !window.confirm(
                  "Fechar posições DEMO do robô?\n\nSó posições atribuídas a este robô. A conta será revalidada como DEMO.",
                )
              ) {
                return;
              }
              void act(() => api.robotDemoClosePositions(true));
            }}
          >
            Fechar posições DEMO do robô
          </button>
        </div>
      </section>

      {/* 5. Gráfico */}
      <section className="panel p-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-400">
          <span>
            {t("robot.chart")} EUR/USD M15
            {candleMeta.label ? ` · ${candleMeta.label}` : ""}
          </span>
          <span>
            Última cotação:{" "}
            {ts.last_quote_at
              ? formatDateTime(ts.last_quote_at, "UTC") + " UTC"
              : "—"}
            {status.quote?.mid != null ? ` · ${formatPrice(status.quote.mid)}` : ""}
          </span>
        </div>
        <CandleChart candles={candles} height={320} />
      </section>

      {/* 6. Próxima análise + orçamento */}
      {status.openai_autonomous && (
        <section className="panel p-4">
          <div className="panel-title mb-2">OpenAI — agendamento e orçamento</div>
          <div className="grid gap-2 text-sm text-slate-300 sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <div className="text-xs text-slate-500">Próxima análise</div>
              {ts.next_openai_analysis_at || status.openai_autonomous.next_analysis_at
                ? formatDateTime(
                    String(
                      ts.next_openai_analysis_at || status.openai_autonomous.next_analysis_at,
                    ),
                    "UTC",
                  ) + " UTC"
                : "—"}
            </div>
            <div>
              <div className="text-xs text-slate-500">Última análise</div>
              {ts.last_openai_analysis_at
                ? formatDateTime(ts.last_openai_analysis_at, "UTC") + " UTC"
                : "—"}
            </div>
            <div>
              <div className="text-xs text-slate-500">Gasto / restante</div>$
              {formatNumber(status.openai_autonomous.spent_today_usd ?? 0, {
                maximumFractionDigits: 4,
              })}{" "}
              / $
              {formatNumber(status.openai_autonomous.remaining_today_usd ?? 0, {
                maximumFractionDigits: 4,
              })}
            </div>
            <div>
              <div className="text-xs text-slate-500">Teto diário</div>$
              {formatNumber(status.openai_autonomous.daily_budget_usd ?? 0.1, {
                maximumFractionDigits: 2,
              })}{" "}
              · {status.openai_autonomous.interval_minutes ?? 15} min
            </div>
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              className="rounded-lg bg-white/10 px-3 py-1.5 text-xs disabled:opacity-40"
              disabled={busy || Boolean(status.openai_autonomous.confirmed)}
              onClick={() => act(() => api.robotAiAutonomousArm())}
            >
              Preparar
            </button>
            <button
              className="rounded-lg bg-accent-gold/20 px-3 py-1.5 text-xs text-accent-gold disabled:opacity-40"
              disabled={
                busy ||
                Boolean(status.openai_autonomous.confirmed) ||
                !status.openai_autonomous.ready_for_activation
              }
              onClick={() => {
                if (
                  !window.confirm(
                    "Confirmar ativação da OpenAI autónoma?\n\n" +
                      "Autoriza chamadas pagas (gpt-4o-mini) a cada 15 min, teto 0,10 USD/dia.\n" +
                      "Isto NÃO autoriza ordens DEMO.",
                  )
                ) {
                  return;
                }
                void act(() => api.robotAiAutonomousActivate());
              }}
            >
              {status.openai_autonomous.confirmed
                ? "Já confirmado"
                : "Confirmar ativação (autoriza chamadas pagas)"}
            </button>
            <button
              className="rounded-lg bg-accent-sell/20 px-3 py-1.5 text-xs text-accent-sell disabled:opacity-40"
              disabled={
                busy ||
                (!status.openai_autonomous.confirmed && !status.openai_autonomous.armed)
              }
              onClick={() => act(() => api.robotAiAutonomousDeactivate())}
            >
              Desativar
            </button>
          </div>
        </section>
      )}

      {/* 7. Histórico */}
      <section className="panel p-4">
        <div className="panel-title mb-2">Histórico de decisões OpenAI</div>
        {(openaiReal?.recent || []).length === 0 ? (
          <div className="text-xs text-slate-500">Sem decisões persistidas.</div>
        ) : (
          <div className="space-y-2">
            {(openaiReal?.recent || []).map((row) => (
              <div key={row.id} className="rounded border border-slate-700/60 px-2 py-1.5">
                <button
                  type="button"
                  className="flex w-full flex-wrap items-center justify-between gap-2 text-left text-xs text-slate-300"
                  onClick={() =>
                    setExpandedId((id) => (id === row.id ? null : row.id ?? null))
                  }
                >
                  <span>
                    <span className={decisionClass(row.decision)}>{row.decision_pt}</span>
                    {" · "}
                    {row.symbol && row.symbol !== NA ? row.symbol : "par n/d"}
                    {" · "}
                    {row.analyzed_at
                      ? formatDateTime(row.analyzed_at, "UTC") + " UTC"
                      : "—"}
                  </span>
                  <span className="text-slate-500">
                    {row.estimated_cost_usd != null
                      ? `$${formatNumber(row.estimated_cost_usd, { maximumFractionDigits: 4 })}`
                      : "—"}
                    {" · "}
                    {expandedId === row.id ? "ocultar" : "detalhe"}
                  </span>
                </button>
                {expandedId === row.id ? (
                  <div className="mt-2 space-y-1 text-xs text-slate-400">
                    <p>{fieldOrNa(row.justification)}</p>
                    <p>
                      Confiança: {fieldOrNa(row.confidence)} · Tentativa de ordem:{" "}
                      {(row as { execution_attempted?: boolean }).execution_attempted
                        ? "sim"
                        : "não"}
                      {" · "}
                      Risco:{" "}
                      {(row as { risk_approved?: boolean }).risk_approved
                        ? "aprovado"
                        : "não aprovado / n.d."}
                      {(row as { mt5_ticket?: string | null }).mt5_ticket
                        ? ` · Ticket MT5: ${(row as { mt5_ticket?: string }).mt5_ticket}`
                        : ""}
                    </p>
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 8. Diagnósticos */}
      <section className="panel p-4">
        <button
          type="button"
          className="panel-title text-left text-slate-400"
          onClick={() => setDiagOpen((v) => !v)}
        >
          Diagnósticos técnicos {diagOpen ? "▾" : "▸"}
        </button>
        {diagOpen ? (
          <div className="mt-3 space-y-3 text-xs text-slate-500">
            <div>
              Radar: {status.radar?.with_valid_data ?? 0}/{status.radar?.monitored ?? 0} pares
              elegíveis (alvo {status.radar?.universe_target ?? 28}) ·{" "}
              {status.radar?.best_opportunity || "—"}
            </div>
            {(status.radar?.top5 || status.radar?.candidates || []).length > 0 ? (
              <div>
                Top-5:{" "}
                {(status.radar?.top5 || status.radar?.candidates || [])
                  .slice(0, 5)
                  .map((c: { symbol?: string; score?: number }, i: number) => (
                    <span key={`${c.symbol}-${i}`}>
                      {i > 0 ? " · " : ""}
                      {c.symbol}
                      {c.score != null ? ` (${Number(c.score).toFixed(2)})` : ""}
                    </span>
                  ))}
              </div>
            ) : null}
            <div>
              Cotação: {status.quote?.source || "—"} · feed={status.quote?.feed_state || "—"} ·
              clock_anomaly={String(status.quote?.clock_anomaly ?? false)}
            </div>
            <div>
              SimulatedBroker (TEST/BASELINE) — não é saldo DEMO: equity{" "}
              {formatMoney(status.capital?.equity ?? 0, status.capital?.currency || "USD")} ·
              sessão P&L{" "}
              {formatMoney(
                (status.capital?.session_realized_pnl ?? 0) +
                  (status.capital?.unrealized_pnl ?? 0),
                status.capital?.currency || "USD",
              )}
            </div>
            {(status.trades || []).slice(0, 5).map((tr) => (
              <div key={tr.id}>
                TEST #{tr.id} {tr.side} P&L {formatNumber(tr.pnl)}
              </div>
            ))}
            <div>
              LIVE locked · AI={String(status.security.ai_enabled)} · {status.last_message}
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}
