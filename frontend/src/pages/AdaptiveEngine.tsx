import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  api,
  type AdaptiveDashboard,
  type AdaptiveDownloadJob,
  type AdaptiveExperiment,
  type AdaptiveStrategyCard,
} from "../api/client";
import { formatDateTime, formatNumber } from "../i18n/format";

const DEFAULT_START = "2025-10-01T00:00";
const DEFAULT_END = "2026-10-01T00:00";

function toApiIso(localValue: string): string {
  // datetime-local sem fuso → tratar como UTC (investigação em UTC).
  if (!localValue) return localValue;
  if (localValue.endsWith("Z") || /[+-]\d{2}:\d{2}$/.test(localValue)) return localValue;
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(localValue)) return `${localValue}:00Z`;
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(localValue)) return `${localValue}Z`;
  return localValue;
}

function StrategyList({
  title,
  items,
  empty,
}: {
  title: string;
  items: AdaptiveStrategyCard[];
  empty: string;
}) {
  return (
    <section className="panel p-4">
      <div className="panel-title mb-2">
        {title} ({items.length})
      </div>
      <div className="space-y-2 text-xs text-slate-300">
        {items.length === 0 && <div className="text-slate-500">{empty}</div>}
        {items.slice(0, 8).map((s) => (
          <div key={s.id} className="rounded border border-white/10 px-2 py-1.5">
            <div className="font-medium text-white">{s.display_name}</div>
            <div>
              score val. {formatNumber(s.validation_score)} · OOS {formatNumber(s.out_of_sample_score)} · DD{" "}
              {formatNumber(s.max_drawdown_pct)}%
            </div>
            <div className="text-slate-500">{s.reason}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

export function AdaptiveEngine() {
  const { t } = useTranslation();
  const [dash, setDash] = useState<AdaptiveDashboard | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [start, setStart] = useState(DEFAULT_START);
  const [end, setEnd] = useState(DEFAULT_END);
  const [scheduleOn, setScheduleOn] = useState(false);
  const [job, setJob] = useState<AdaptiveDownloadJob | null>(null);
  const [expRun, setExpRun] = useState<AdaptiveExperiment | null>(null);
  const pollRef = useRef<number | null>(null);
  const expPollRef = useRef<number | null>(null);
  const loadRef = useRef<() => Promise<void>>(async () => undefined);

  const startPoll = useCallback((id: number) => {
    if (pollRef.current) window.clearInterval(pollRef.current);
    pollRef.current = window.setInterval(async () => {
      try {
        const j = await api.adaptiveHistoryDownloadStatus(id);
        setJob(j);
        if (["completed", "failed", "cancelled"].includes(j.status)) {
          if (pollRef.current) window.clearInterval(pollRef.current);
          pollRef.current = null;
          await loadRef.current();
        }
      } catch {
        /* ignore transient */
      }
    }, 1500);
  }, []);

  const load = useCallback(async () => {
    const d = await api.adaptiveDashboard();
    setDash(d);
    setScheduleOn(Boolean(d.schedule?.enabled));
    const latest = d.history?.latest_download;
    if (latest) {
      setJob(latest);
      if (latest.status === "running" || latest.status === "queued") {
        startPoll(latest.id);
      }
    }
  }, [startPoll]);

  loadRef.current = load;

  useEffect(() => {
    load().catch((e) => setErr(String(e)));
  }, [load]);

  useEffect(() => {
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
      if (expPollRef.current) window.clearInterval(expPollRef.current);
    };
  }, []);

  useEffect(() => {
    const running = dash?.experiments?.find((e) => ["queued", "running"].includes(e.status));
    if (running && !expPollRef.current) {
      setExpRun(running);
      pollExperiment(running.id);
      setBusy(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dash?.experiments]);

  const onDownload = async (resumeId?: number) => {
    setBusy(true);
    setErr("");
    try {
      const hist = dash?.history;
      const est = hist?.download_estimate || hist?.preparation?.download_estimate;
      const ok = window.confirm(
        t("adaptive.downloadConfirm", {
          hours: est?.missing_hours ?? "?",
          duration: est?.estimated_duration_label ?? "—",
          size: est?.estimated_size_mb ?? "?",
          disclaimer: est?.disclaimer ?? "",
        }),
      );
      if (!ok) return;
      const j = await api.adaptiveHistoryDownload({
        start: toApiIso(start),
        end: toApiIso(end),
        symbol: "EURUSD",
        timeframe: "M15",
        confirmed: true,
        resume_job_id: resumeId,
      });
      setJob(j);
      if (j.status === "running" || j.status === "queued") startPoll(j.id);
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onCancel = async () => {
    if (!job) return;
    setBusy(true);
    try {
      const j = await api.adaptiveHistoryDownloadCancel(job.id);
      setJob(j);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const pollExperiment = (id: number) => {
    if (expPollRef.current) window.clearInterval(expPollRef.current);
    expPollRef.current = window.setInterval(async () => {
      try {
        const e = await api.adaptiveExperiment(id);
        setExpRun(e);
        if (["completed", "failed", "cancelled"].includes(e.status)) {
          if (expPollRef.current) window.clearInterval(expPollRef.current);
          expPollRef.current = null;
          setBusy(false);
          await load();
        }
      } catch {
        /* ignore */
      }
    }, 1200);
  };

  const onRun = async () => {
    setBusy(true);
    setErr("");
    try {
      const started = await api.adaptiveStartExperiment({
        start: toApiIso(start),
        end: toApiIso(end),
        symbol: "EURUSD",
        timeframe: "M15",
        name: "experiência-dashboard",
        authorize_openai: false,
        require_real_data: true,
      });
      setExpRun(started);
      if (["queued", "running", "pending"].includes(started.status) || started.async) {
        pollExperiment(started.id);
      } else {
        setBusy(false);
        await load();
      }
    } catch (e) {
      setErr(String(e));
      setBusy(false);
    }
  };

  const onSchedule = async () => {
    setBusy(true);
    setErr("");
    try {
      await api.adaptiveSchedule({
        enabled: !scheduleOn,
        interval_minutes: 1440,
        lookback_days: 30,
        authorize_openai: false,
      });
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

  const experimental = dash.strategies.experimental || [];
  const validating = dash.strategies.validating || [];
  const approved = dash.strategies.approved || [];
  const hist = dash.history;
  const cov = hist?.coverage;
  const prep = hist?.preparation;
  const m15 = hist?.cache_dukascopy?.by_timeframe?.M15;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">{t("adaptive.title")}</h1>
        <p className="mt-1 text-sm text-slate-400">{t("adaptive.help")}</p>
        <p className="mt-1 text-xs text-emerald-400/90">{t("adaptive.noAi")}</p>
      </div>

      <div className="rounded-lg border border-accent-gold/20 bg-accent-gold/5 px-3 py-2 text-xs text-accent-gold">
        {dash.disclaimer}
      </div>

      {err && (
        <div className="rounded-lg border border-accent-sell/30 bg-accent-sell/10 px-3 py-2 text-sm text-accent-sell">
          {err}
        </div>
      )}

      <section className="panel space-y-3 p-4">
        <div className="panel-title">{t("adaptive.historyTitle")}</div>
        <p className="text-xs text-slate-400">{t("adaptive.historyHelp")}</p>
        <div className="text-sm text-slate-200">
          {t("adaptive.defaultPeriod")}: {hist?.default_period?.label_pt || "01/10/2025 → 01/10/2026 UTC"}
          <button
            type="button"
            className="ml-3 text-xs text-accent-gold underline"
            onClick={() => {
              setStart(DEFAULT_START);
              setEnd(DEFAULT_END);
            }}
          >
            {t("adaptive.useDefaultPeriod")}
          </button>
        </div>
        <div className="grid gap-3 md:grid-cols-4 text-xs text-slate-300">
          <div>
            <div className="text-slate-500">{t("adaptive.oldest")}</div>
            <div>{cov?.oldest_available ? formatDateTime(cov.oldest_available, "UTC") + " UTC" : "—"}</div>
          </div>
          <div>
            <div className="text-slate-500">{t("adaptive.newest")}</div>
            <div>{cov?.newest_available ? formatDateTime(cov.newest_available, "UTC") + " UTC" : "—"}</div>
          </div>
          <div>
            <div className="text-slate-500">{t("adaptive.coverage")}</div>
            <div className="text-white">{formatNumber(cov?.coverage_pct ?? 0)}%</div>
          </div>
          <div>
            <div className="text-slate-500">{t("adaptive.validReal")}</div>
            <div>
              {cov?.bars_valid_real ?? 0} / {cov?.expected_trading_bars ?? 0}
            </div>
          </div>
        </div>
        <div className="text-xs text-slate-400">
          {t("adaptive.barsByTf")}:{" "}
          {["M5", "M15", "H1", "H4"]
            .map((tf) => {
              const row = hist?.cache_dukascopy?.by_timeframe?.[tf];
              return `${tf}=${row?.real_bars ?? 0}`;
            })
            .join(" · ")}
          {m15?.oldest ? ` · cache M15 ${formatDateTime(m15.oldest, "UTC")} → ${formatDateTime(m15.newest || "", "UTC")}` : ""}
        </div>
        <div className="text-xs text-slate-400">
          {t("adaptive.missingHours")}: {cov?.missing_hours_count ?? 0} · {t("adaptive.weekendExcluded")}:{" "}
          {cov?.weekend_slots_excluded ?? 0} · fonte: {cov?.label || "Dukascopy"}
        </div>
        {cov?.distinction?.has_synthetic_contamination && (
          <div className="text-xs text-amber-300">{t("adaptive.syntheticWarn")}</div>
        )}
        {(cov?.continuity_gap_examples?.length ?? 0) > 0 ? (
          <div className="text-xs text-amber-200/90">
            {t("adaptive.gaps")}:
            <ul className="mt-1 list-disc pl-4">
              {cov!.continuity_gap_examples.slice(0, 5).map((g) => (
                <li key={g}>{g}</li>
              ))}
            </ul>
          </div>
        ) : (
          <div className="text-xs text-slate-500">{t("adaptive.noGaps")}</div>
        )}

        {(hist?.download_estimate || prep?.download_estimate) && (
          <div className="text-xs text-slate-400">
            {t("adaptive.estimate")}:{" "}
            {(hist?.download_estimate || prep?.download_estimate)?.estimated_duration_label} · ~
            {(hist?.download_estimate || prep?.download_estimate)?.estimated_size_mb} MB
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          <button
            className="rounded-lg bg-accent-gold/20 px-3 py-2 text-sm text-accent-gold"
            disabled={busy || job?.status === "running"}
            onClick={() => onDownload()}
          >
            {t("adaptive.downloadMissing")}
          </button>
          {job && (job.status === "running" || job.cancel_requested) && (
            <button className="rounded-lg bg-white/10 px-3 py-2 text-sm" disabled={busy} onClick={onCancel}>
              {t("adaptive.cancelDownload")}
            </button>
          )}
          {job && ["cancelled", "failed", "interrupted"].includes(job.status) && (
            <button
              className="rounded-lg bg-accent-gold/20 px-3 py-2 text-sm text-accent-gold"
              disabled={busy}
              onClick={() => onDownload(job.id)}
            >
              {t("adaptive.resumeDownload")} #{job.id}
            </button>
          )}
        </div>

        {job && (
          <div className="space-y-1 text-xs text-slate-300">
            <div>
              {t("adaptive.lastJob")} #{job.id} · {job.status} · {formatNumber(job.progress_pct)}% (
              {job.hours_done}/{job.hours_total})
            </div>
            <div className="h-2 overflow-hidden rounded bg-ink-800">
              <div className="h-full bg-accent-gold/70" style={{ width: `${Math.min(100, job.progress_pct)}%` }} />
            </div>
            <div>{job.message}</div>
            {job.error && <div className="text-accent-sell">{job.error}</div>}
          </div>
        )}

        <div
          className={`rounded border px-3 py-2 text-xs ${
            prep?.ready ? "border-emerald-500/30 text-emerald-300" : "border-accent-sell/30 text-amber-200"
          }`}
        >
          <div className="font-medium">
            {t("adaptive.prepState")}: {prep?.ready ? t("adaptive.prepReady") : t("adaptive.prepBlocked")}
          </div>
          <div className="mt-1">{prep?.message}</div>
          {!prep?.ready &&
            (prep?.reasons || []).slice(0, 4).map((r) => (
              <div key={r} className="mt-0.5 text-amber-100/80">
                • {r}
              </div>
            ))}
        </div>
      </section>

      <section className="panel grid gap-3 p-4 md:grid-cols-3">
        <div>
          <div className="panel-title mb-1">{t("adaptive.regime")}</div>
          <div className="text-lg text-white">{dash.regime.label_pt}</div>
          <div className="text-xs text-slate-500">
            {dash.regime.as_of ? formatDateTime(dash.regime.as_of, "UTC") + " UTC" : t("common.none")}
            {dash.regime.data_source ? ` · ${dash.regime.data_source}` : ""}
          </div>
        </div>
        <div>
          <div className="panel-title mb-1">{t("adaptive.selection")}</div>
          <div className="text-sm text-slate-200">
            {String(dash.latest_selection?.reason || t("adaptive.noSelection"))}
          </div>
        </div>
        <div>
          <div className="panel-title mb-1">{t("adaptive.costs")}</div>
          <div className="text-sm text-slate-300">
            {dash.costs_model
              ? `${t("adaptive.spread")} ${formatNumber(Number(dash.costs_model.spread_pips))} pips · ${t("adaptive.slippage")} ${formatNumber(Number(dash.costs_model.slippage_pips))} · ${t("adaptive.commission")} ${formatNumber(Number(dash.costs_model.commission))}`
              : t("adaptive.costsDefault")}
          </div>
          <div className="mt-1 text-xs text-slate-500">{t("adaptive.riskLocked")}</div>
        </div>
      </section>

      <section className="panel grid gap-3 p-4 md:grid-cols-2">
        <div>
          <div className="panel-title mb-2">{t("adaptive.startExperiment")}</div>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="text-xs text-slate-400">
              {t("market.start")} (UTC)
              <input
                type="datetime-local"
                className="mt-1 w-full rounded bg-ink-800 p-2 text-sm"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </label>
            <label className="text-xs text-slate-400">
              {t("market.end")} (UTC)
              <input
                type="datetime-local"
                className="mt-1 w-full rounded bg-ink-800 p-2 text-sm"
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </label>
          </div>
          <button
            className="mt-3 rounded-lg bg-accent-gold/20 px-3 py-2 text-sm text-accent-gold disabled:opacity-40"
            disabled={busy || !prep?.ready || ["queued", "running"].includes(expRun?.status || "")}
            onClick={onRun}
          >
            {busy || ["queued", "running"].includes(expRun?.status || "")
              ? t("adaptive.running")
              : t("adaptive.runLocal")}
          </button>
          {expRun && (
            <div className="mt-3 space-y-1 text-xs text-slate-300">
              <div>
                {t("adaptive.expProgress")} #{expRun.id} · {expRun.status} ·{" "}
                {formatNumber(expRun.progress_pct ?? 0)}%
                {expRun.candidates_total
                  ? ` · ${expRun.candidates_tested}/${expRun.candidates_total} ${t("adaptive.combinations")}`
                  : ""}
              </div>
              <div className="h-2 overflow-hidden rounded bg-ink-800">
                <div
                  className="h-full bg-emerald-500/70 transition-all"
                  style={{ width: `${Math.min(100, expRun.progress_pct ?? 0)}%` }}
                />
              </div>
              <div>{expRun.progress_message || expRun.error || "—"}</div>
              {expRun.status === "failed" && expRun.error && (
                <div className="text-accent-sell">{expRun.error}</div>
              )}
            </div>
          )}
        </div>
        <div>
          <div className="panel-title mb-2">{t("adaptive.schedule")}</div>
          <p className="text-xs text-slate-400">{t("adaptive.scheduleHelp")}</p>
          <button className="mt-3 rounded-lg bg-white/10 px-3 py-2 text-sm" disabled={busy} onClick={onSchedule}>
            {scheduleOn ? t("adaptive.scheduleDisable") : t("adaptive.scheduleEnable")}
          </button>
          {dash.schedule?.next_run_at && (
            <div className="mt-2 text-xs text-slate-500">
              {t("adaptive.nextRun")}: {formatDateTime(dash.schedule.next_run_at, "UTC")} UTC
            </div>
          )}
        </div>
      </section>

      <div className="grid gap-4 lg:grid-cols-3">
        <StrategyList title={t("adaptive.investigating")} items={experimental} empty={t("adaptive.empty")} />
        <StrategyList title={t("adaptive.validating")} items={validating} empty={t("adaptive.empty")} />
        <StrategyList title={t("adaptive.approved")} items={approved} empty={t("adaptive.empty")} />
      </div>

      <section className="panel p-4">
        <div className="panel-title mb-2">{t("adaptive.performance")}</div>
        <div className="overflow-auto">
          <table className="w-full text-left text-xs text-slate-300">
            <thead className="text-slate-500">
              <tr>
                <th className="py-1">{t("adaptive.colStrategy")}</th>
                <th>{t("adaptive.colStatus")}</th>
                <th>{t("adaptive.colVal")}</th>
                <th>{t("adaptive.colOos")}</th>
                <th>{t("adaptive.colDd")}</th>
                <th>{t("adaptive.colCosts")}</th>
                <th>{t("adaptive.colExperiments")}</th>
              </tr>
            </thead>
            <tbody>
              {dash.performance.map((p) => (
                <tr key={p.id} className="border-t border-white/5">
                  <td className="py-1.5 text-white">{p.name}</td>
                  <td>{p.status}</td>
                  <td>{formatNumber(p.validation_score)}</td>
                  <td>{formatNumber(p.out_of_sample_score)}</td>
                  <td>{formatNumber(p.max_drawdown_pct)}%</td>
                  <td>{formatNumber(p.trading_costs)}</td>
                  <td>{p.experiment_count}</td>
                </tr>
              ))}
              {dash.performance.length === 0 && (
                <tr>
                  <td colSpan={7} className="py-3 text-slate-500">
                    {t("adaptive.empty")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="panel p-4">
          <div className="panel-title mb-2">{t("adaptive.changes")}</div>
          <div className="space-y-2 text-xs text-slate-300">
            {dash.changes.length === 0 && <div className="text-slate-500">{t("adaptive.empty")}</div>}
            {dash.changes.slice(0, 12).map((c) => (
              <div key={c.id} className="rounded border border-white/10 px-2 py-1.5">
                <div className="text-white">
                  {c.action}: {c.from_status} → {c.to_status}
                </div>
                <div className="text-slate-500">{c.reason}</div>
              </div>
            ))}
          </div>
        </section>
        <section className="panel p-4">
          <div className="panel-title mb-2">{t("adaptive.experiments")}</div>
          <div className="space-y-2 text-xs text-slate-300">
            {dash.experiments.length === 0 && <div className="text-slate-500">{t("adaptive.empty")}</div>}
            {dash.experiments.slice(0, 8).map((e) => (
              <div key={e.id} className="rounded border border-white/10 px-2 py-1.5">
                <div className="text-white">
                  #{e.id} {e.name} · {e.status} · {e.selected_action}
                </div>
                <div>
                  {e.current_regime_pt || e.current_regime} · {e.candidates_tested} {t("adaptive.candidates")} ·{" "}
                  {e.data_source}
                  {e.is_synthetic ? ` · ${t("env.synthetic")}` : ""}
                </div>
                {e.error && <div className="text-accent-sell">{e.error}</div>}
              </div>
            ))}
          </div>
        </section>
      </div>

      <section className="panel p-4">
        <div className="panel-title mb-2">{t("adaptive.catalog")}</div>
        <div className="grid gap-2 md:grid-cols-2">
          {dash.catalog.map((c) => (
            <div key={c.key} className="rounded border border-white/10 px-3 py-2 text-xs text-slate-300">
              <div className="text-sm text-white">{c.display_name}</div>
              <div>{c.description}</div>
              <div className="mt-1 text-slate-500">
                {t("adaptive.complexity")}: {formatNumber(c.complexity)} · regimes:{" "}
                {c.preferred_regimes.join(", ")}
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
