import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type AutonomyDashboard } from "../api/client";
import { formatDateTime, formatNumber } from "../i18n/format";

export function AutonomyCenter() {
  const { t } = useTranslation();
  const [dash, setDash] = useState<AutonomyDashboard | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const d = await api.autonomyDashboard();
    setDash(d);
  }, []);

  useEffect(() => {
    load().catch((e) => setErr(String(e)));
    const id = window.setInterval(() => {
      load().catch(() => undefined);
    }, 4000);
    return () => window.clearInterval(id);
  }, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setErr("");
    try {
      await fn();
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!dash) {
    return <div className="py-16 text-center text-slate-400">{err || t("app.loading")}</div>;
  }

  const eng = dash.engine;
  const paper = dash.paper;
  const rej = dash.rejection_audit;
  const statusKey = eng.display_status || eng.run_status || (!eng.enabled ? "off" : eng.paused ? "paused" : "idle");
  const statusLabel =
    statusKey === "off"
      ? t("autonomy.off")
      : statusKey === "paused"
        ? t("autonomy.paused")
        : statusKey === "executing"
          ? t("autonomy.executing")
          : statusKey === "error"
            ? t("autonomy.error")
            : t("autonomy.idle");
  const statusClass =
    statusKey === "error"
      ? "text-accent-sell"
      : statusKey === "executing"
        ? "text-accent-gold"
        : statusKey === "idle"
          ? "text-emerald-400"
          : "text-white";
  const lastCycle = dash.last_cycle;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">{t("autonomy.title")}</h1>
        <p className="mt-1 text-sm text-slate-400">{t("autonomy.help")}</p>
      </div>

      <div className="rounded-lg border border-accent-gold/20 bg-accent-gold/5 px-3 py-2 text-xs text-accent-gold">
        {dash.disclaimer}
      </div>

      {err && (
        <div className="rounded-lg border border-accent-sell/30 bg-accent-sell/10 px-3 py-2 text-sm text-accent-sell">
          {err}
        </div>
      )}

      <section className="panel grid gap-3 p-4 md:grid-cols-2">
        <div>
          <div className="panel-title mb-2">{t("autonomy.engineState")}</div>
          <div className="space-y-1 text-sm text-slate-200">
            <div>
              {t("autonomy.status")}: <span className={statusClass}>{statusLabel}</span>
            </div>
            <div>
              {t("autonomy.paper")}: {eng.paper_enabled ? t("autonomy.on") : t("autonomy.off")}
            </div>
            <div>
              {t("autonomy.interval")}: {eng.interval_minutes} min · {t("autonomy.maxCombos")}: {eng.max_combos}
            </div>
            <div>
              {t("autonomy.lastCycle")}:{" "}
              {eng.last_cycle_at ? formatDateTime(eng.last_cycle_at, "UTC") + " UTC" : "—"}
            </div>
            <div>
              {t("autonomy.nextCycle")}:{" "}
              {eng.next_cycle_at ? formatDateTime(eng.next_cycle_at, "UTC") + " UTC" : "—"}
            </div>
            {lastCycle && (
              <div className="text-xs text-slate-400">
                {t("autonomy.lastCycleResult")}: #{lastCycle.id} · {lastCycle.status}
                {lastCycle.duration_seconds != null
                  ? ` · ${t("autonomy.duration")} ${formatNumber(lastCycle.duration_seconds)}s`
                  : ""}
                {lastCycle.strategies_count
                  ? ` · ${lastCycle.strategies_count} ${t("autonomy.strategiesInCycle")}`
                  : ""}
                {lastCycle.message ? ` — ${lastCycle.message}` : ""}
              </div>
            )}
            <div className="text-xs text-slate-400">{eng.last_message}</div>
            {eng.last_error && (
              <div className="text-xs text-accent-sell">
                {t("autonomy.lastError")}: {eng.last_error}
              </div>
            )}
            <div className="text-xs text-emerald-400/90">
              {t("autonomy.security")}: LIVE locked · AI calls={eng.ai_calls} · AI_ENABLED=
              {String(dash.security.ai_enabled)}
            </div>
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              className="rounded-lg bg-accent-gold/20 px-3 py-2 text-sm text-accent-gold"
              disabled={busy}
              onClick={() => act(() => api.autonomyStart())}
            >
              {t("autonomy.start")}
            </button>
            <button className="rounded-lg bg-white/10 px-3 py-2 text-sm" disabled={busy} onClick={() => act(() => api.autonomyPause())}>
              {t("autonomy.pause")}
            </button>
            <button
              className="rounded-lg bg-white/10 px-3 py-2 text-sm"
              disabled={busy}
              onClick={() => act(() => api.autonomyResume())}
            >
              {t("autonomy.resume")}
            </button>
            <button
              className="rounded-lg bg-white/10 px-3 py-2 text-sm"
              disabled={busy}
              onClick={() => act(() => (eng.paper_enabled ? api.autonomyPaperDisable() : api.autonomyPaperEnable()))}
            >
              {eng.paper_enabled ? t("autonomy.paperOff") : t("autonomy.paperOn")}
            </button>
          </div>
        </div>

        <div>
          <div className="panel-title mb-2">{t("autonomy.tasks")}</div>
          {(dash.running_tasks || []).length === 0 && (
            <div className="text-xs text-slate-500">{t("autonomy.noTasks")}</div>
          )}
          {(dash.running_tasks || []).map((task) => (
            <div key={task.id} className="mb-2 text-xs text-slate-300">
              #{task.id} {task.status} · {formatNumber(task.progress_pct)}%
              <div className="mt-1 h-1.5 overflow-hidden rounded bg-ink-800">
                <div className="h-full bg-emerald-500/70" style={{ width: `${Math.min(100, task.progress_pct)}%` }} />
              </div>
              <div>{task.progress_message}</div>
            </div>
          ))}
          <div className="panel-title mb-2 mt-4">{t("autonomy.auditExp1")}</div>
          <div className="text-xs text-slate-300">
            <div>{rej.message}</div>
            <div className="mt-1 text-slate-500">
              {t("autonomy.candidates")}: {rej.candidates} · {t("autonomy.approved")}: {rej.approved}
            </div>
            {rej.rejection_counts &&
              Object.entries(rej.rejection_counts).map(([k, v]) => (
                <div key={k}>
                  {k}: {v}
                </div>
              ))}
            <div className="mt-2 text-slate-500">
              {t("autonomy.criteria")}: score≥{rej.promotion_criteria?.min_score}, PF_val≥
              {rej.promotion_criteria?.min_pf_validation}, PF_oos≥{rej.promotion_criteria?.min_pf_test}, ops≥
              {rej.promotion_criteria?.min_validation_trades}
            </div>
          </div>
        </div>
      </section>

      <section className="panel grid gap-3 p-4 md:grid-cols-4 text-sm">
        <div>
          <div className="text-slate-500">{t("autonomy.netPnl")}</div>
          <div className="text-lg text-white">{formatNumber(paper.net_pnl)}</div>
        </div>
        <div>
          <div className="text-slate-500">{t("autonomy.equity")}</div>
          <div className="text-lg text-white">{formatNumber(paper.equity_approx)}</div>
        </div>
        <div>
          <div className="text-slate-500">{t("autonomy.drawdown")}</div>
          <div className="text-lg text-white">{formatNumber(paper.max_drawdown_pct)}%</div>
        </div>
        <div>
          <div className="text-slate-500">{t("autonomy.pf")}</div>
          <div className="text-lg text-white">{formatNumber(paper.profit_factor)}</div>
        </div>
        <div>
          <div className="text-slate-500">{t("autonomy.trades")}</div>
          <div>{paper.trades}</div>
        </div>
        <div>
          <div className="text-slate-500">{t("autonomy.winRate")}</div>
          <div>{formatNumber(paper.win_rate_pct)}%</div>
        </div>
        <div>
          <div className="text-slate-500">{t("autonomy.costs")}</div>
          <div>{formatNumber(paper.costs)}</div>
        </div>
        <div>
          <div className="text-slate-500">{t("autonomy.catalog")}</div>
          <div>{dash.catalog_size}</div>
        </div>
      </section>

      <div className="grid gap-4 lg:grid-cols-3">
        {(["experimental", "approved", "suspended"] as const).map((key) => (
          <section key={key} className="panel p-4">
            <div className="panel-title mb-2">
              {t(`autonomy.${key}`)} ({(dash.strategies[key] || []).length})
            </div>
            <div className="space-y-2 text-xs text-slate-300">
              {(dash.strategies[key] || []).slice(0, 8).map((s) => (
                <div key={s.id} className="rounded border border-white/10 px-2 py-1.5">
                  <div className="text-white">{s.name}</div>
                  <div>
                    score {formatNumber(s.score)} · OOS {formatNumber(s.oos)}
                  </div>
                  <div className="text-slate-500">{s.reason}</div>
                </div>
              ))}
              {(dash.strategies[key] || []).length === 0 && (
                <div className="text-slate-500">{t("autonomy.empty")}</div>
              )}
            </div>
          </section>
        ))}
      </div>

      <section className="panel p-4">
        <div className="panel-title mb-2">{t("autonomy.cycleHistory")}</div>
        {(dash.recent_cycles || []).length === 0 && (
          <div className="text-xs text-slate-500">{t("autonomy.empty")}</div>
        )}
        {(dash.recent_cycles || []).slice(0, 6).map((c) => (
          <div key={c.id} className="mb-1 text-xs text-slate-400">
            #{c.id} [{c.status}]{" "}
            {c.started_at ? formatDateTime(c.started_at, "UTC") + " UTC" : "—"}
            {c.duration_seconds != null ? ` · ${formatNumber(c.duration_seconds)}s` : ""}
            {c.message ? ` — ${c.message}` : ""}
          </div>
        ))}
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="panel p-4">
          <div className="panel-title mb-2">{t("autonomy.openTrades")}</div>
          {(paper.open || []).length === 0 && <div className="text-xs text-slate-500">{t("autonomy.empty")}</div>}
          {(paper.open || []).map((tr) => (
            <div key={tr.id} className="mb-2 text-xs text-slate-300">
              #{tr.id} {tr.side} @ {formatNumber(tr.entry)} · {tr.strategy}
              {tr.is_replay ? ` · ${t("autonomy.replayWarn")}` : ""}
              <div className="text-slate-500">{tr.reason}</div>
            </div>
          ))}
        </section>
        <section className="panel p-4">
          <div className="panel-title mb-2">{t("autonomy.alerts")}</div>
          {(dash.alerts || []).slice(0, 10).map((a) => (
            <div key={a.id} className="mb-1 text-xs text-slate-400">
              [{a.level}] {a.message}
            </div>
          ))}
          {(dash.alerts || []).length === 0 && <div className="text-xs text-slate-500">{t("autonomy.empty")}</div>}
        </section>
      </div>
    </div>
  );
}
