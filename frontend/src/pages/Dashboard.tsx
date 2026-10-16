import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  api,
  type AuditItem,
  type Candle,
  type ChartMarker,
  type Dashboard as Dash,
  type DownloadJobResult,
  type QuoteFeedStatus,
  type ReplayStatus,
} from "../api/client";
import { CandleChart } from "../components/CandleChart";
import { EquityChart } from "../components/EquityChart";
import { TradingAgentsPanel } from "../components/TradingAgentsPanel";
import { formatDateTime, formatMoney, formatNumber, formatPercent, formatPrice, recommendationLabel } from "../i18n/format";

const ROLES = [
  "technical",
  "trend",
  "fundamental",
  "strategy",
  "risk_challenger",
  "coordinator",
] as const;

function recColor(rec: string) {
  if (rec === "BUY") return "text-accent-buy";
  if (rec === "SELL") return "text-accent-sell";
  return "text-accent-hold";
}

export function Dashboard() {
  const { t } = useTranslation();
  const [dash, setDash] = useState<Dash | null>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [markers, setMarkers] = useState<ChartMarker[]>([]);
  const [tf, setTf] = useState("M15");
  const [dataSource, setDataSource] = useState<"dukascopy" | "synthetic" | "auto">("auto");
  const [label, setLabel] = useState("");
  const [isSynthetic, setIsSynthetic] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [logs, setLogs] = useState<AuditItem[]>([]);
  const [tick, setTick] = useState<{ bid?: number; ask?: number; equity?: number; spread?: number }>({});
  const [quoteFeed, setQuoteFeed] = useState<QuoteFeedStatus | null>(null);
  const [histStart, setHistStart] = useState("2025-10-13T00:00:00Z");
  const [histEnd, setHistEnd] = useState("2025-10-14T00:00:00Z");
  const [download, setDownload] = useState<DownloadJobResult | null>(null);
  const [replay, setReplay] = useState<ReplayStatus | null>(null);
  const [aiEvery, setAiEvery] = useState(12);
  const [speed, setSpeed] = useState(2);
  const [reportJson, setReportJson] = useState("");

  const load = async () => {
    const source = dataSource === "auto" ? undefined : dataSource;
    const [d, c, l, qf] = await Promise.all([
      api.dashboard(),
      api.candles(tf, source),
      api.logs(),
      api.quoteFeed(),
    ]);
    setDash(d);
    setCandles(c.candles);
    setLabel(c.label);
    setIsSynthetic(c.is_synthetic);
    setLogs(l);
    setQuoteFeed(qf ?? d.quote_feed ?? null);
  };

  useEffect(() => {
    load().catch((e) => setError(String(e)));
  }, [tf, dataSource]);

  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/prices`);
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        setTick({
          bid: msg.bid,
          ask: msg.ask,
          equity: msg.equity,
          spread: msg.spread_pips,
        });
        if (msg.quote_feed) {
          setQuoteFeed(msg.quote_feed as QuoteFeedStatus);
          // Atualizar gráfico quando o feed persistir novas velas (sem disparar IA).
          if (msg.quote_feed.candles_updated && Object.keys(msg.quote_feed.candles_updated).length) {
            const source = dataSource === "auto" ? undefined : dataSource;
            api.candles(tf, source).then((c) => {
              setCandles(c.candles);
              setLabel(c.label);
              setIsSynthetic(c.is_synthetic);
            }).catch(() => undefined);
          }
        }
      } catch {
        /* ignore */
      }
    };
    return () => ws.close();
  }, [tf, dataSource]);

  useEffect(() => {
    if (!replay || replay.session.status !== "playing") return;
    const id = window.setInterval(() => {
      api
        .replayStatus(replay.session.id)
        .then((r) => {
          setReplay(r);
          setCandles(r.candles);
          setMarkers(r.markers);
          setLabel(r.session.label);
          setIsSynthetic(false);
        })
        .catch(() => undefined);
    }, 1000);
    return () => window.clearInterval(id);
  }, [replay?.session.id, replay?.session.status]);

  const lastRecCode =
    replay?.timeline.at(-1)?.recommendation ?? dash?.decisions[0]?.recommendation ?? "HOLD";
  const lastRec = recommendationLabel(lastRecCode, t);

  const usage = useMemo(
    () => dash?.api_usage.reduce((s, u) => s + u.estimated_cost_usd, 0) ?? 0,
    [dash],
  );

  const equityPoints = replay?.equity_curve?.length ? replay.equity_curve : (dash?.equity_curve ?? []);

  const onRun = async (execute: boolean) => {
    setBusy(true);
    setError("");
    try {
      const { startAuthorizedAnalysis } = await import("../ai/startAnalysis");
      await startAuthorizedAnalysis(
        {
          symbol: "EURUSD",
          timeframe: tf,
          execute,
          debate_rounds: 2,
          enabled_roles: Object.fromEntries(ROLES.map((r) => [r, true])),
          source: dataSource === "auto" ? "auto" : dataSource,
        },
        {
          aiDisabled: t("aiUsage.mustEnable"),
          confirmTemplate: (maxCalls, cost, disclaimer) =>
            t("aiUsage.confirmAnalysis", { maxCalls, cost, disclaimer }),
        },
      );
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onKill = async () => {
    if (!dash) return;
    await api.kill(!dash.kill_switch);
    await load();
  };

  const onDownload = async () => {
    setBusy(true);
    setError("");
    setDownload(null);
    try {
      const job = await api.downloadDukascopy(histStart, histEnd);
      setDownload(job);
      if (job.status === "completed") {
        setDataSource("dukascopy");
        await load();
      } else if (job.status === "failed") {
        setError(job.error || job.message);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onCreateReplay = async () => {
    setBusy(true);
    setError("");
    try {
      const r = await api.createReplay({
        start: histStart,
        end: histEnd,
        timeframe: tf,
        speed,
        ai_every_n_bars: aiEvery,
        execute_trades: true,
      });
      setReplay(r);
      setCandles(r.candles);
      setMarkers(r.markers);
      setLabel(r.session.label);
      setIsSynthetic(false);
      setDataSource("dukascopy");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onReplayCtrl = async (action: "play" | "pause" | "restart") => {
    if (!replay) return;
    const id = replay.session.id;
    const fn = action === "play" ? api.replayPlay : action === "pause" ? api.replayPause : api.replayRestart;
    const r = await fn(id);
    setReplay(r);
    setCandles(r.candles);
    setMarkers(r.markers);
  };

  const onExport = async () => {
    if (!replay) return;
    const report = await api.replayReport(replay.session.id);
    const text = JSON.stringify(report, null, 2);
    setReportJson(text);
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `grokforex-reproducao-${replay.session.id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  if (!dash) {
    return <div className="py-20 text-center text-slate-400">{error || t("app.loading")}</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-slate-500">{t("env.label")}</div>
          <div className="flex items-center gap-2">
            <span className="text-lg font-semibold">{dash.environment}</span>
            <span className="rounded bg-accent-gold/15 px-2 py-0.5 text-[11px] text-accent-gold">
              {t("env.simulated")}
            </span>
            <span
              className={`rounded px-2 py-0.5 text-[11px] ${
                isSynthetic ? "bg-slate-500/20 text-slate-300" : "bg-emerald-500/15 text-emerald-300"
              }`}
            >
              {isSynthetic ? t("env.synthetic") : t("env.realHistorical")}
            </span>
            {dash.kill_switch && (
              <span className="rounded bg-accent-sell/20 px-2 py-0.5 text-[11px] text-accent-sell">
                {t("env.killSwitch")}
              </span>
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="rounded-lg bg-white/10 px-3 py-2 text-sm hover:bg-white/15" disabled={busy} onClick={() => onRun(false)}>
            {t("actions.runAgents")}
          </button>
          <button
            className="rounded-lg bg-accent-gold/20 px-3 py-2 text-sm text-accent-gold hover:bg-accent-gold/30"
            disabled={busy || dash.kill_switch}
            onClick={() => onRun(true)}
          >
            {t("actions.analyzeExecute")}
          </button>
          <button className="rounded-lg bg-accent-sell/20 px-3 py-2 text-sm text-accent-sell hover:bg-accent-sell/30" onClick={onKill}>
            {dash.kill_switch ? t("actions.disarmEmergency") : t("actions.emergencyStop")}
          </button>
        </div>
      </div>

      <div className="rounded-lg border border-accent-gold/20 bg-accent-gold/5 px-3 py-2 text-xs text-accent-gold">
        {dash.disclaimer} · {label}
      </div>
      <div className="rounded-lg border border-white/10 bg-ink-800/60 px-3 py-2 text-xs text-slate-300">
        {t("market.activeSource")}:{" "}
        <strong className="text-white">
          {label} ({dataSource === "auto" ? dash.active_market?.resolved_source || "auto" : dataSource})
        </strong>
        {" · "}
        {t("ta.timeframe")}: {tf}
        {" · "}
        {isSynthetic ? t("env.synthetic") : t("env.realHistorical")}
        {dash.active_market?.reason ? ` · ${dash.active_market.reason}` : ""}
      </div>

      {quoteFeed && (
        <section className="rounded-lg border border-white/10 bg-ink-800/70 px-4 py-3">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div className="panel-title">{t("market.quoteFeedTitle")}</div>
            <div className="flex items-center gap-2 text-xs">
              <span
                className={`inline-flex h-2.5 w-2.5 rounded-full ${
                  quoteFeed.connected ? "bg-emerald-400" : "bg-slate-500"
                }`}
                title={quoteFeed.connected ? t("market.linkOn") : t("market.linkOff")}
              />
              <span className="text-slate-400">
                {t("market.connection")}:{" "}
                {quoteFeed.connected ? t("market.linkOn") : t("market.linkOff")}
              </span>
              <span
                className={`rounded px-2 py-0.5 ${
                  quoteFeed.status === "atualizado"
                    ? "bg-emerald-500/20 text-emerald-300"
                    : quoteFeed.status === "com_atraso"
                      ? "bg-amber-500/20 text-amber-200"
                      : quoteFeed.status === "obsoleto"
                        ? "bg-accent-sell/20 text-accent-sell"
                        : "bg-slate-500/20 text-slate-300"
                }`}
              >
                {t("market.feedStatus")}: {quoteFeed.status_label}
              </span>
            </div>
          </div>
          <div className="grid gap-2 text-xs text-slate-300 sm:grid-cols-2 lg:grid-cols-3">
            <div>
              <div className="text-slate-500">{t("market.quoteFeedSource")}</div>
              <div className="text-white">{quoteFeed.source_label}</div>
              <div className="text-slate-500">canal: {quoteFeed.channel}</div>
            </div>
            <div>
              <div className="text-slate-500">{t("market.lastQuote")}</div>
              <div className="mono text-sm text-white">
                {quoteFeed.mid != null ? formatPrice(quoteFeed.mid) : t("common.none")}
                {quoteFeed.bid != null && quoteFeed.ask != null && !quoteFeed.mid_only
                  ? ` · ${t("market.bid")} ${formatPrice(quoteFeed.bid)} / ${t("market.ask")} ${formatPrice(quoteFeed.ask)}`
                  : ""}
              </div>
              {quoteFeed.mid_only && (
                <div className="text-slate-500">{t("market.midOnlyNote")}</div>
              )}
            </div>
            <div>
              <div className="text-slate-500">{t("market.lastUpdate")}</div>
              <div>
                {quoteFeed.quote_ts
                  ? `${formatDateTime(quoteFeed.quote_ts, "UTC")} UTC`
                  : t("common.none")}
              </div>
            </div>
            <div>
              <div className="text-slate-500">{t("market.measuredLag")}</div>
              <div>
                {quoteFeed.lag_seconds != null
                  ? `${Math.round(quoteFeed.lag_seconds)} s`
                  : t("common.none")}
              </div>
            </div>
            <div>
              <div className="text-slate-500">{t("market.spread")}</div>
              <div>
                {quoteFeed.spread_pips != null
                  ? `${formatNumber(quoteFeed.spread_pips)} pips`
                  : t("common.none")}
              </div>
            </div>
            <div>
              <div className="text-slate-500">{t("market.notRealtime")}</div>
              <div>
                {quoteFeed.block_entries ? t("market.entriesBlocked") : quoteFeed.message}
              </div>
            </div>
          </div>
        </section>
      )}

      {error && (
        <div className="rounded-lg border border-accent-sell/30 bg-accent-sell/10 px-3 py-2 text-sm text-accent-sell">{error}</div>
      )}

      <section className="panel grid gap-3 p-4 lg:grid-cols-2">
        <div>
          <div className="panel-title mb-2">{t("market.historicalTitle")}</div>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="text-xs text-slate-400">
              {t("market.start")} ({t("market.hourUtc")})
              <input className="mt-1 w-full rounded bg-ink-800 p-2 text-sm" value={histStart} onChange={(e) => setHistStart(e.target.value)} />
            </label>
            <label className="text-xs text-slate-400">
              {t("market.end")} ({t("market.hourUtc")})
              <input className="mt-1 w-full rounded bg-ink-800 p-2 text-sm" value={histEnd} onChange={(e) => setHistEnd(e.target.value)} />
            </label>
          </div>
          <div className="mt-2 flex flex-wrap gap-2">
            <button className="rounded-lg bg-emerald-500/20 px-3 py-2 text-sm text-emerald-300" disabled={busy} onClick={onDownload}>
              {t("actions.download")}
            </button>
            <select
              className="rounded bg-ink-800 px-2 py-2 text-sm"
              value={dataSource}
              onChange={(e) => setDataSource(e.target.value as typeof dataSource)}
            >
              <option value="auto">{t("market.sourceAuto")}</option>
              <option value="dukascopy">{t("market.sourceReal")}</option>
              <option value="synthetic">{t("market.sourceSynthetic")}</option>
            </select>
          </div>
          <div className="mt-2 text-xs text-slate-400">
            {t("market.cache")}: M5={dash.dukascopy_bars?.M5 ?? 0} · M15={dash.dukascopy_bars?.M15 ?? 0} · H1=
            {dash.dukascopy_bars?.H1 ?? 0} · H4={dash.dukascopy_bars?.H4 ?? 0}
          </div>
          {download && (
            <div className="mt-2 rounded border border-white/10 p-2 text-xs">
              <div>
                {t("market.downloadProgress")} #{download.id}: {download.status} ·{" "}
                {formatPercent(download.progress_pct, 0)}
              </div>
              <div className="text-slate-300">{download.message}</div>
              {download.error && <div className="text-accent-sell">{download.error}</div>}
              {download.bars && (
                <div className="text-slate-400">
                  {t("market.bars")}: M5={download.bars.M5} M15={download.bars.M15} H1={download.bars.H1} H4=
                  {download.bars.H4}
                </div>
              )}
            </div>
          )}
        </div>

        <div>
          <div className="panel-title mb-2">{t("replay.title")}</div>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="text-xs text-slate-400">
              {t("replay.aiEvery")}
              <input
                type="number"
                className="mt-1 w-full rounded bg-ink-800 p-2 text-sm"
                value={aiEvery}
                onChange={(e) => setAiEvery(Number(e.target.value))}
              />
            </label>
            <label className="text-xs text-slate-400">
              {t("replay.speed")}
              <input
                type="number"
                step="0.5"
                className="mt-1 w-full rounded bg-ink-800 p-2 text-sm"
                value={speed}
                onChange={(e) => setSpeed(Number(e.target.value))}
              />
            </label>
          </div>
          <div className="mt-2 flex flex-wrap gap-2">
            <button className="rounded-lg bg-white/10 px-3 py-2 text-sm" disabled={busy} onClick={onCreateReplay}>
              {t("actions.createReplay")}
            </button>
            <button className="rounded-lg bg-accent-buy/20 px-3 py-2 text-sm text-accent-buy" disabled={!replay} onClick={() => onReplayCtrl("play")}>
              {t("actions.play")}
            </button>
            <button className="rounded-lg bg-white/10 px-3 py-2 text-sm" disabled={!replay} onClick={() => onReplayCtrl("pause")}>
              {t("actions.pause")}
            </button>
            <button className="rounded-lg bg-white/10 px-3 py-2 text-sm" disabled={!replay} onClick={() => onReplayCtrl("restart")}>
              {t("actions.restart")}
            </button>
            <button className="rounded-lg bg-accent-gold/20 px-3 py-2 text-sm text-accent-gold" disabled={!replay} onClick={onExport}>
              {t("actions.exportReport")}
            </button>
          </div>
          {replay && (
            <div className="mt-2 space-y-1 text-xs text-slate-300">
              <div>
                {t("replay.title")} #{replay.session.id} · {replay.session.status} · {t("replay.bar")}{" "}
                {replay.session.cursor_index}/{replay.session.bars_total}
                {replay.session.cursor_ts ? ` · ${formatDateTime(replay.session.cursor_ts, "UTC")} UTC` : ""}
              </div>
              <div>
                {t("replay.decisions")} {replay.session.decisions} · {t("replay.trades")} {replay.session.trades} ·{" "}
                {t("replay.aiPnl")} {formatNumber(replay.session.ai_net_pnl)} · {t("replay.baseline")}{" "}
                {formatNumber(replay.session.baseline_net_pnl)} · DD {formatPercent(replay.session.max_drawdown_pct)} ·{" "}
                {t("replay.apiCost")} ${formatNumber(replay.session.api_cost_usd, { maximumFractionDigits: 4 })}
              </div>
            </div>
          )}
        </div>
      </section>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
        <Stat label={t("stats.balance")} value={formatMoney(dash.account.balance, dash.account.currency)} />
        <Stat
          label={t("stats.equity")}
          value={formatMoney(tick.equity ?? dash.account.equity, dash.account.currency)}
        />
        <Stat
          label={t("stats.realizedPnl")}
          value={formatMoney(dash.account.realized_pnl, dash.account.currency)}
          tone={dash.account.realized_pnl}
        />
        <Stat label={t("stats.winRate")} value={formatPercent(dash.metrics.win_rate)} />
        <Stat label={t("stats.profitFactor")} value={formatNumber(dash.metrics.profit_factor)} />
        <Stat
          label={t("stats.maxDd")}
          value={formatPercent(replay?.drawdown_pct ?? dash.metrics.max_drawdown_pct)}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <section className="panel p-4 xl:col-span-2">
          <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
            <div>
              <div className="panel-title">{t("market.title")}</div>
              <div className="mono text-2xl">
                {formatPrice(tick.bid ?? replay?.last_price ?? candles.at(-1)?.close ?? 0)}
                <span className={`ml-3 text-base ${recColor(lastRecCode)}`}>{lastRec}</span>
              </div>
              <div className="text-xs text-slate-500">
                {t("market.bid")} {tick.bid != null ? formatPrice(tick.bid) : t("common.none")} · {t("market.ask")}{" "}
                {tick.ask != null ? formatPrice(tick.ask) : t("common.none")}
                {tick.spread != null ? ` · ${t("market.spread")} ${formatNumber(tick.spread)} pips` : ""}
                {" · "}
                {dash.broker.message}
              </div>
            </div>
            <div className="flex gap-1">
              {["M5", "M15", "H1", "H4"].map((x) => (
                <button
                  key={x}
                  onClick={() => setTf(x)}
                  className={`rounded px-2 py-1 text-xs ${tf === x ? "bg-white/15 text-white" : "text-slate-400"}`}
                >
                  {x}
                </button>
              ))}
            </div>
          </div>
          <CandleChart candles={candles} markers={markers} />
        </section>

        <section className="panel p-4">
          <div className="panel-title mb-2">{t("panels.equity")}</div>
          <EquityChart points={equityPoints} />
          <div className="mt-3 space-y-1 text-xs text-slate-400">
            <div>
              {t("panels.broker")}: {dash.broker.adapter} ·{" "}
              {dash.broker.connected ? t("panels.connected") : t("panels.disconnected")}
            </div>
            <div>
              {t("panels.apiCost")}: ${formatNumber(usage, { maximumFractionDigits: 4 })}
            </div>
            <div>{t("env.liveOff")}</div>
            {replay?.api_cost_by_agent?.map((a) => (
              <div key={a.role}>
                {t(`agents.${a.role}`, { defaultValue: a.role })}: $
                {formatNumber(a.estimated_cost_usd, { maximumFractionDigits: 4 })} ({a.calls})
              </div>
            ))}
          </div>
        </section>
      </div>

      <TradingAgentsPanel
        dash={dash}
        timeframe={tf}
        dataSource={dataSource}
        onTimeframeChange={setTf}
        onRefresh={async () => {
          await load();
        }}
      />

      <section className="panel p-4">
        <div className="panel-title mb-1">{t("panels.timeline")}</div>
        <p className="mb-3 text-xs text-slate-500">{t("panels.timelineHelp")}</p>
        <div className="space-y-2">
          {(replay?.timeline?.length
            ? replay.timeline
            : dash.decisions.map((d) => ({
                id: d.id,
                time: d.created_at,
                recommendation: d.recommendation,
                risk_approved: d.risk_approved,
                executed: d.executed,
                rationale: d.rationale,
                data_source: d.data_source,
              }))
          ).map((d) => (
            <div key={d.id} className="rounded-lg border border-white/5 px-3 py-2 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className={`font-semibold ${recColor(d.recommendation)}`}>
                  {recommendationLabel(d.recommendation, t)}
                </span>
                <span className={d.risk_approved ? "text-accent-buy" : "text-accent-hold"}>
                  {d.risk_approved ? t("panels.riskApproved") : t("panels.riskRejected")}
                </span>
                <span className="text-slate-500">{formatDateTime(d.time)}</span>
                <span className="text-[11px] text-accent-gold">{d.data_source}</span>
              </div>
              {d.rationale && <div className="mt-1 text-slate-300">{d.rationale}</div>}
            </div>
          ))}
        </div>
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="panel overflow-hidden">
          <div className="panel-title px-4 pt-4">{t("panels.openPositions")}</div>
          <table className="mt-2 w-full text-left text-sm">
            <thead className="text-xs text-slate-500">
              <tr>
                <th className="px-4 py-2">{t("panels.side")}</th>
                <th>{t("panels.lots")}</th>
                <th>{t("panels.entry")}</th>
                <th>{t("panels.pnl")}</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {dash.positions.length === 0 && (
                <tr>
                  <td className="px-4 py-3 text-slate-500" colSpan={5}>
                    {t("panels.noPositions")}
                  </td>
                </tr>
              )}
              {dash.positions.map((p) => (
                <tr key={p.id} className="border-t border-white/5">
                  <td className={`px-4 py-2 ${recColor(p.side)}`}>{recommendationLabel(p.side, t)}</td>
                  <td>{formatNumber(p.volume_lots)}</td>
                  <td className="mono">{formatPrice(p.entry_price)}</td>
                  <td className={p.unrealized_pnl >= 0 ? "text-accent-buy" : "text-accent-sell"}>
                    {formatNumber(p.unrealized_pnl)}
                  </td>
                  <td>
                    <button className="text-xs text-slate-400 hover:text-white" onClick={() => api.closePosition(p.id).then(load)}>
                      {t("actions.close")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="panel p-4">
          <div className="panel-title mb-3">{t("panels.audit")}</div>
          <div className="max-h-64 space-y-1 overflow-auto font-mono text-xs text-slate-400">
            {logs.length === 0 && <div>{t("panels.noAudit")}</div>}
            {logs.slice(0, 40).map((log) => (
              <div key={log.id}>
                <span className="text-slate-500">{formatDateTime(log.created_at)}</span> [{log.category}] {log.message}
              </div>
            ))}
          </div>
        </section>
      </div>

      {reportJson && (
        <section className="panel p-4">
          <div className="panel-title mb-2">{t("panels.exportedReport")}</div>
          <pre className="max-h-64 overflow-auto text-xs text-slate-300">{reportJson.slice(0, 4000)}</pre>
        </section>
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: number }) {
  const color = tone === undefined ? "text-white" : tone >= 0 ? "text-accent-buy" : "text-accent-sell";
  return (
    <div className="panel px-3 py-3">
      <div className="panel-title">{label}</div>
      <div className={`mono mt-1 text-lg ${color}`}>{value}</div>
    </div>
  );
}
