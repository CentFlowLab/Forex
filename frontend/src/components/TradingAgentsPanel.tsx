import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  api,
  type AgentCard,
  type AnalysisSummary,
  type Dashboard as Dash,
} from "../api/client";
import { formatDateTime, formatNumber, formatPercent, recommendationLabel } from "../i18n/format";

const CORE_ROLES = [
  "technical",
  "trend",
  "fundamental",
  "strategy",
  "risk_challenger",
  "coordinator",
] as const;

const STAGE_KEYS = [
  "market_data",
  "analysts",
  "bull_opening",
  "bear_opening",
  "debate",
  "risk",
  "coordinator",
] as const;

function recColor(rec: string) {
  if (rec === "BUY") return "text-accent-buy";
  if (rec === "SELL") return "text-accent-sell";
  return "text-accent-hold";
}

function statusLabel(status: string | undefined, t: (k: string) => string): string {
  const s = (status || "pending").toLowerCase();
  if (s === "running" || s === "queued") return t("ta.stateRunning");
  if (s === "completed" || s === "completed_with_errors" || s === "completed_without_debate") {
    return t("ta.stateDone");
  }
  if (s === "cancelled") return t("ta.stateCancelled");
  if (s === "blocked" || s === "failed" || s === "error") return t("ta.stateError");
  if (s === "skipped") return t("ta.stateSkipped");
  return t("ta.statePending");
}

function statusTone(status: string | undefined): string {
  const s = (status || "pending").toLowerCase();
  if (s === "running" || s === "queued") return "text-accent-gold bg-accent-gold/15";
  if (s === "completed") return "text-accent-buy bg-accent-buy/15";
  if (s.includes("error") || s === "failed" || s === "blocked" || s === "completed_without_debate") {
    return "text-accent-sell bg-accent-sell/15";
  }
  if (s === "cancelled" || s === "skipped") return "text-slate-300 bg-white/10";
  return "text-slate-400 bg-white/5";
}

type Props = {
  dash: Dash;
  timeframe: string;
  dataSource: "dukascopy" | "synthetic" | "auto";
  onTimeframeChange: (tf: string) => void;
  onRefresh: () => Promise<void>;
};

export function TradingAgentsPanel({
  dash,
  timeframe,
  dataSource,
  onTimeframeChange,
  onRefresh,
}: Props) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [debateRounds, setDebateRounds] = useState(dash.max_debate_rounds ?? 2);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [enabled, setEnabled] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(CORE_ROLES.map((r) => [r, true])),
  );
  const [history, setHistory] = useState<AnalysisSummary[]>([]);
  const analysis = dash.latest_analysis;

  const packageData = analysis?.package;
  const debate = analysis?.debate;
  const stages = analysis?.stages || {};
  const diagnostics = analysis?.diagnostics || {};
  const debateExecuted = Boolean(debate?.debate_executed ?? packageData?.debate_executed);
  const debateComplete = Boolean(debate?.debate_complete ?? packageData?.debate_complete);

  const loadHistory = async () => {
    const rows = await api.analysisList(20);
    setHistory(rows);
  };

  const onStart = async (execute: boolean) => {
    setBusy(true);
    setError("");
    try {
      const { startAuthorizedAnalysis } = await import("../ai/startAnalysis");
      await startAuthorizedAnalysis(
        {
          symbol: "EURUSD",
          timeframe,
          execute,
          debate_rounds: debateRounds,
          enabled_roles: enabled,
          source: dataSource === "auto" ? "auto" : dataSource,
        },
        {
          aiDisabled: t("aiUsage.mustEnable"),
          confirmTemplate: (maxCalls, cost, disclaimer) =>
            t("aiUsage.confirmAnalysis", { maxCalls, cost, disclaimer }),
        },
      );
      await onRefresh();
      await loadHistory();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onStop = async () => {
    if (!analysis?.id) return;
    setBusy(true);
    try {
      await api.analysisStop(analysis.id);
      await onRefresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const timelineMarkers = useMemo(() => {
    return (dash.decisions || []).slice(0, 24).map((d) => ({
      id: d.id,
      time: d.created_at,
      recommendation: d.recommendation,
    }));
  }, [dash.decisions]);

  const agentStatus = (role: string): AgentCard | undefined => {
    // Apenas relatórios da última análise TradingAgents — nunca histórico órfão em inglês.
    const fromAnalysis = (analysis?.reports || []).find((r) => r.role === role);
    if (fromAnalysis) {
      return {
        role,
        display_name: t(`agents.${role}`),
        status: fromAnalysis.status,
        bias: fromAnalysis.bias || "NEUTRAL",
        confidence: fromAnalysis.confidence || 0,
        uncertainty: fromAnalysis.uncertainty || 1,
        summary: fromAnalysis.summary || "",
        error: fromAnalysis.error || "",
        provider: fromAnalysis.provider || "",
        model: fromAnalysis.model || "",
        created_at: analysis?.created_at || "",
      };
    }
    return undefined;
  };

  const overallStatus = busy ? "running" : analysis?.status || "pending";

  return (
    <section className="panel space-y-4 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="panel-title">{t("ta.title")}</div>
          <p className="mt-1 max-w-3xl text-sm text-slate-400">{t("ta.subtitle")}</p>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
            <span className={`rounded px-2 py-1 ${statusTone(overallStatus)}`}>
              {statusLabel(overallStatus, t)}
            </span>
            {analysis?.id != null && (
              <span className="rounded bg-white/5 px-2 py-1 text-slate-400">
                {t("ta.analysisId")}: #{analysis.id}
              </span>
            )}
            <span className="rounded bg-white/5 px-2 py-1 text-slate-400">
              {t("ta.dataSource")}: {analysis?.data_source || dash.active_market?.resolved_source || dataSource}
            </span>
            <span className="rounded bg-white/5 px-2 py-1 text-slate-400">
              {t("ta.timeframe")}: {analysis?.timeframe || timeframe}
            </span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            className="rounded-lg bg-accent-gold/20 px-3 py-2 text-sm text-accent-gold hover:bg-accent-gold/30"
            disabled={busy}
            onClick={() => onStart(false)}
          >
            {busy ? t("ta.stateRunning") : t("ta.startAnalysis")}
          </button>
          <button
            className="rounded-lg bg-white/10 px-3 py-2 text-sm hover:bg-white/15"
            disabled={busy || !analysis?.id}
            onClick={onStop}
          >
            {t("ta.stopAnalysis")}
          </button>
          <button
            className="rounded-lg bg-white/10 px-3 py-2 text-sm hover:bg-white/15"
            disabled={busy}
            onClick={() => loadHistory().catch((e) => setError(String(e)))}
          >
            {t("ta.loadHistory")}
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-accent-sell/30 bg-accent-sell/10 px-3 py-2 text-sm text-accent-sell">
          {error}
        </div>
      )}

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7">
        {STAGE_KEYS.map((key) => (
          <div key={key} className={`rounded px-2 py-2 text-xs ${statusTone(stages[key])}`}>
            <div className="text-[10px] uppercase tracking-wide opacity-80">{t(`ta.stages.${key}`)}</div>
            <div className="mt-1 font-medium">{statusLabel(stages[key], t)}</div>
          </div>
        ))}
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <label className="text-xs text-slate-400">
          {t("ta.debateDepth")}
          <input
            type="number"
            min={0}
            max={4}
            className="mt-1 w-full rounded bg-ink-800 p-2 text-sm"
            value={debateRounds}
            onChange={(e) => setDebateRounds(Number(e.target.value))}
          />
        </label>
        <label className="text-xs text-slate-400">
          {t("ta.timeframe")}
          <select
            className="mt-1 w-full rounded bg-ink-800 p-2 text-sm"
            value={timeframe}
            onChange={(e) => onTimeframeChange(e.target.value)}
          >
            {["M5", "M15", "H1", "H4"].map((tf) => (
              <option key={tf} value={tf}>
                {tf}
              </option>
            ))}
          </select>
        </label>
        <div className="text-xs text-slate-400">
          {t("ta.aiBudget")}
          <div className="mt-1 rounded bg-ink-800 p-2 text-sm text-slate-200">
            {t("ta.daily")}: ${formatNumber(dash.ai_budget?.daily_usd ?? 0, { maximumFractionDigits: 2 })} ·{" "}
            {t("ta.session")}: ${formatNumber(dash.ai_budget?.session_usd ?? 0, { maximumFractionDigits: 2 })}
          </div>
        </div>
        <div className="text-xs text-slate-400">
          {t("ta.dataFreshness")}
          <div className="mt-1 rounded bg-ink-800 p-2 text-sm text-slate-200">
            {analysis?.data_freshness ?? t("common.none")} ·{" "}
            {analysis?.is_synthetic ? t("env.synthetic") : t("env.realHistorical")}
          </div>
        </div>
      </div>

      <div className="flex flex-wrap gap-3 text-xs text-slate-300">
        {CORE_ROLES.map((role) => (
          <label key={role} className="inline-flex items-center gap-2 rounded bg-ink-800/80 px-2 py-1">
            <input
              type="checkbox"
              checked={enabled[role] !== false}
              onChange={(e) => setEnabled((prev) => ({ ...prev, [role]: e.target.checked }))}
            />
            {t(`agents.${role}`)}
          </label>
        ))}
      </div>

      {/* Auditoria */}
      <div className="rounded border border-white/10 p-3 text-xs text-slate-300">
        <div className="panel-title mb-2">{t("ta.diagnostics")}</div>
        <div className="grid gap-1 md:grid-cols-2">
          <div>
            {t("ta.analysisId")}: #{analysis?.id ?? t("common.none")}
          </div>
          <div>
            {t("ta.when")}:{" "}
            {analysis?.created_at ? formatDateTime(analysis.created_at, "UTC") + " UTC" : t("common.none")}
          </div>
          <div>
            {t("ta.dataSource")}: {analysis?.data_source || dash.active_market?.resolved_source || t("common.none")}
          </div>
          <div>
            {t("ta.models")}: {(analysis?.models_used || []).join(", ") || t("common.none")}
          </div>
          <div>
            {t("ta.rounds")}:{" "}
            {(diagnostics.rounds_label as string | undefined) ||
              `${t("ta.openingRound")}: ${
                (diagnostics.opening_round as string) || analysis?.debate?.opening_round || t("common.none")
              } · ${t("ta.responseRounds")}: ${
                analysis?.debate_rounds_completed ?? diagnostics.response_rounds_completed ?? 0
              }/${analysis?.debate_rounds_requested ?? diagnostics.response_rounds_requested ?? debateRounds}`}
          </div>
          {!!(diagnostics.failed_agents as { role?: string; status?: string }[] | undefined)?.length && (
            <div className="md:col-span-2 text-accent-sell">
              {t("ta.failedAgents")}:{" "}
              {(diagnostics.failed_agents as { display_name?: string; role?: string; status?: string }[])
                .map((f) => `${f.display_name || f.role} (${f.status})`)
                .join(" · ")}
            </div>
          )}
          <div>
            {t("ta.cost")}: ${formatNumber(analysis?.estimated_cost_usd ?? 0, { maximumFractionDigits: 4 })} ·{" "}
            {t("ta.tokens")}: {(analysis?.prompt_tokens ?? 0) + (analysis?.completion_tokens ?? 0)}
          </div>
          {analysis?.error && (
            <div className="md:col-span-2 text-accent-sell">
              {t("common.error")}: {analysis.error}
            </div>
          )}
          {!!(diagnostics.language_warnings as string[] | undefined)?.length && (
            <div className="md:col-span-2 text-accent-gold">
              {t("ta.languageWarning")}: {(diagnostics.language_warnings as string[]).join(" · ")}
            </div>
          )}
        </div>
      </div>

      <div className="grid gap-3 lg:grid-cols-3">
        <div className="rounded border border-white/10 p-3 lg:col-span-1">
          <div className="text-xs uppercase tracking-wide text-slate-500">{t("ta.finalDecision")}</div>
          {!debateExecuted && analysis && (
            <div className="mt-2 rounded bg-accent-hold/15 px-2 py-1 text-xs text-accent-hold">
              {t("ta.debateNotExecuted")}
            </div>
          )}
          {debateExecuted && !debateComplete && (
            <div className="mt-2 rounded bg-accent-gold/15 px-2 py-1 text-xs text-accent-gold">
              {t("ta.debateIncomplete")}
            </div>
          )}
          <div
            className={`mt-1 text-2xl font-semibold ${recColor(packageData?.direction ?? analysis?.recommendation ?? "HOLD")}`}
          >
            {recommendationLabel(packageData?.direction ?? analysis?.recommendation ?? "HOLD", t)}
          </div>
          <div className="mono mt-1 text-xs text-slate-400">
            {t("agents.confidence")}{" "}
            {formatNumber(packageData?.confidence_score ?? analysis?.confidence ?? 0)} · {t("ta.cost")} $
            {formatNumber(analysis?.estimated_cost_usd ?? 0, { maximumFractionDigits: 4 })}
          </div>
          {analysis?.risk_veto && (
            <div className="mt-2 rounded bg-accent-sell/15 px-2 py-1 text-xs text-accent-sell">
              {t("ta.riskVeto")}: {analysis.risk_veto_reason || t("common.none")}
            </div>
          )}
          <p className="mt-3 text-sm text-slate-300">
            {packageData?.final_explanation || analysis?.error || t("agents.noReport")}
          </p>
        </div>

        <div className="rounded border border-white/10 p-3">
          <div className="flex items-center justify-between gap-2">
            <div className="text-xs uppercase tracking-wide text-slate-500">{t("debate.bull")}</div>
            <span className={`rounded px-2 py-0.5 text-[10px] ${statusTone(stages.bull_opening)}`}>
              {statusLabel(stages.bull_opening, t)}
            </span>
          </div>
          <ul className="mt-2 max-h-48 list-disc space-y-1 overflow-auto pl-4 text-sm text-slate-300">
            {(packageData?.bull_arguments || debate?.bull_arguments || []).length ? (
              (packageData?.bull_arguments || debate?.bull_arguments || []).map((a, i) => (
                <li key={`b-${i}`}>{a}</li>
              ))
            ) : (
              <li className="list-none text-slate-500">
                {analysis ? t("ta.debateEmpty") : t("ta.noDebateYet")}
              </li>
            )}
          </ul>
        </div>

        <div className="rounded border border-white/10 p-3">
          <div className="flex items-center justify-between gap-2">
            <div className="text-xs uppercase tracking-wide text-slate-500">{t("debate.bear")}</div>
            <span className={`rounded px-2 py-0.5 text-[10px] ${statusTone(stages.bear_opening)}`}>
              {statusLabel(stages.bear_opening, t)}
            </span>
          </div>
          <ul className="mt-2 max-h-48 list-disc space-y-1 overflow-auto pl-4 text-sm text-slate-300">
            {(packageData?.bear_arguments || debate?.bear_arguments || []).length ? (
              (packageData?.bear_arguments || debate?.bear_arguments || []).map((a, i) => (
                <li key={`s-${i}`}>{a}</li>
              ))
            ) : (
              <li className="list-none text-slate-500">
                {analysis ? t("ta.debateEmpty") : t("ta.noDebateYet")}
              </li>
            )}
          </ul>
        </div>
      </div>

      {(debate?.rounds || []).length > 0 && (
        <div className="rounded border border-white/10 p-3">
          <div className="panel-title mb-2">{t("ta.debateRounds")}</div>
          <div className="space-y-2 text-sm">
            {(debate?.rounds || []).map((rnd) => (
              <div key={`r-${rnd.round}-${rnd.type}`} className="rounded bg-ink-800/60 p-2">
                <div className="text-xs text-slate-500">
                  {(rnd as { label?: string }).label ||
                    `${t("ta.round")} ${rnd.round} · ${rnd.type}`}{" "}
                  · {rnd.bull?.status}/{rnd.bear?.status}
                </div>
                <div className="mt-1 text-slate-300">
                  <strong className="text-accent-buy">{t("debate.bull")}:</strong>{" "}
                  {rnd.bull?.summary || rnd.bull?.error || t("common.none")}
                </div>
                <div className="mt-1 text-slate-300">
                  <strong className="text-accent-sell">{t("debate.bear")}:</strong>{" "}
                  {rnd.bear?.summary || rnd.bear?.error || t("common.none")}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div>
        <div className="panel-title mb-2">{t("ta.currentAgents")}</div>
        {!analysis && (
          <div className="mb-3 rounded border border-accent-gold/30 bg-accent-gold/10 px-3 py-2 text-sm text-accent-gold">
            {t("ta.noAnalysisYet")}
          </div>
        )}
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {CORE_ROLES.map((role) => {
            const a = agentStatus(role);
            const open = expanded === role;
            const rejected = a?.status === "language_rejected";
            return (
              <article key={role} className="rounded border border-white/10 p-3">
                <button
                  className="flex w-full items-start justify-between gap-2 text-left"
                  onClick={() => setExpanded(open ? null : role)}
                >
                  <div>
                    <div className="panel-title">{t(`agents.${role}`)}</div>
                    <div className="text-xs text-slate-500">
                      {a?.provider ?? t("common.none")} / {a?.model ?? t("common.none")}
                    </div>
                  </div>
                  <span
                    className={`text-xs ${
                      a?.status === "ok" ? "text-accent-buy" : rejected ? "text-accent-sell" : "text-accent-hold"
                    }`}
                  >
                    {a?.status ?? t("ta.statePending")}
                  </span>
                </button>
                <div
                  className={`mt-2 text-sm font-semibold ${recColor(
                    a?.bias === "BULLISH" ? "BUY" : a?.bias === "BEARISH" ? "SELL" : "HOLD",
                  )}`}
                >
                  {a?.bias ?? "NEUTRAL"}
                </div>
                <p className={`mt-1 text-sm text-slate-300 ${open ? "" : "line-clamp-3"}`}>
                  {a?.summary || a?.error || (analysis ? t("agents.noReport") : t("ta.awaitingStart"))}
                </p>
              </article>
            );
          })}
        </div>
      </div>

      <div>
        <div className="panel-title mb-2">{t("ta.decisionTimeline")}</div>
        <div className="flex flex-wrap gap-2">
          {timelineMarkers.map((m) => (
            <div
              key={m.id}
              className={`rounded px-2 py-1 text-xs ${
                m.recommendation === "BUY"
                  ? "bg-accent-buy/20 text-accent-buy"
                  : m.recommendation === "SELL"
                    ? "bg-accent-sell/20 text-accent-sell"
                    : "bg-white/10 text-slate-300"
              }`}
              title={formatDateTime(m.time, "UTC")}
            >
              {recommendationLabel(m.recommendation, t)} · {formatDateTime(m.time, "UTC")}
            </div>
          ))}
          {!timelineMarkers.length && <div className="text-sm text-slate-500">{t("ta.noHistory")}</div>}
        </div>
      </div>

      {!!history.length && (
        <div>
          <div className="panel-title mb-2">{t("ta.analysisHistory")}</div>
          <div className="max-h-48 space-y-1 overflow-auto text-xs text-slate-300">
            {history.map((h) => (
              <div key={h.id} className="flex flex-wrap justify-between gap-2 rounded bg-ink-800/50 px-2 py-1">
                <span>
                  #{h.id} · {recommendationLabel(h.recommendation, t)} · {statusLabel(h.status, t)} ·{" "}
                  {h.data_source} · {t("ta.rounds")}: {h.debate_rounds_completed}
                </span>
                <span>
                  ${formatNumber(h.estimated_cost_usd, { maximumFractionDigits: 4 })} ·{" "}
                  {h.created_at ? formatDateTime(h.created_at, "UTC") : t("common.none")}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {packageData && (
        <div className="grid gap-2 text-xs text-slate-400 md:grid-cols-3">
          <div>
            {t("ta.entry")}:{" "}
            {packageData.entry_proposal != null
              ? formatNumber(packageData.entry_proposal, { maximumFractionDigits: 5 })
              : t("common.none")}
          </div>
          <div>
            Stop Loss:{" "}
            {packageData.stop_loss_proposal != null
              ? formatNumber(packageData.stop_loss_proposal, { maximumFractionDigits: 5 })
              : t("common.none")}
          </div>
          <div>
            Take Profit:{" "}
            {packageData.take_profit_proposal != null
              ? formatNumber(packageData.take_profit_proposal, { maximumFractionDigits: 5 })
              : t("common.none")}
          </div>
          <div className="md:col-span-3">
            {t("ta.disagreement")}: {packageData.agent_disagreement_summary || t("common.none")}
          </div>
          <div className="md:col-span-3">
            {t("ta.keyEvidence")}: {(packageData.key_evidence || []).join(" · ") || t("common.none")}
          </div>
          <div className="md:col-span-3">
            {t("ta.riskObjections")}: {(packageData.risk_objections || []).join(" · ") || t("common.none")}
          </div>
          <div>
            {t("ta.rounds")}: {packageData.debate_rounds} · {t("agents.confidence")}{" "}
            {formatPercent((packageData.confidence_score || 0) * 100)}
          </div>
        </div>
      )}
    </section>
  );
}
