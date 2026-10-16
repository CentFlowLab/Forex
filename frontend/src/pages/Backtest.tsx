import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type BacktestListItem, type BacktestResult } from "../api/client";
import { formatNumber } from "../i18n/format";

export function Backtest() {
  const { t } = useTranslation();
  const [start, setStart] = useState("2024-01-02T00:00:00Z");
  const [end, setEnd] = useState("2024-04-01T00:00:00Z");
  const [walk, setWalk] = useState(true);
  const [engine, setEngine] = useState("baseline");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [history, setHistory] = useState<BacktestListItem[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.backtests().then(setHistory).catch((e) => setErr(String(e)));
  }, []);

  const run = async () => {
    setBusy(true);
    setErr("");
    try {
      const out = await api.backtest({
        symbol: "EURUSD",
        timeframe: "M15",
        start,
        end,
        walk_forward: walk,
        engine,
      });
      setResult(out);
      setHistory(await api.backtests());
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">{t("backtest.title")}</h1>
      <p className="text-sm text-slate-400">{t("backtest.help")}</p>
      <section className="panel grid gap-3 p-4 md:grid-cols-2">
        <label className="text-xs text-slate-400">
          {t("market.start")}
          <input className="mt-1 w-full rounded bg-ink-800 p-2 text-sm" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className="text-xs text-slate-400">
          {t("market.end")}
          <input className="mt-1 w-full rounded bg-ink-800 p-2 text-sm" value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={walk} onChange={(e) => setWalk(e.target.checked)} />
          {t("backtest.walkForward")}
        </label>
        <label className="text-xs text-slate-400">
          {t("backtest.engine")}
          <select className="mt-1 w-full rounded bg-ink-800 p-2 text-sm" value={engine} onChange={(e) => setEngine(e.target.value)}>
            <option value="baseline">{t("backtest.engineBaseline")}</option>
            <option value="ai-sample">{t("backtest.engineAi")}</option>
            <option value="compare-workflows">{t("backtest.engineCompare")}</option>
          </select>
        </label>
        <button className="rounded-lg bg-accent-gold/20 px-3 py-2 text-sm text-accent-gold md:col-span-2" disabled={busy} onClick={run}>
          {busy ? t("backtest.running") : t("actions.runBacktest")}
        </button>
      </section>
      {err && <div className="rounded-lg bg-accent-sell/10 p-3 text-sm text-accent-sell">{err}</div>}
      {result && (
        <section className="panel p-4 text-sm">
          <div className="panel-title">{t("backtest.result")}</div>
          <pre className="mt-2 overflow-auto text-xs text-slate-300">{JSON.stringify(result, null, 2)}</pre>
        </section>
      )}
      <section className="panel p-4">
        <div className="panel-title mb-2">{t("backtest.history")}</div>
        <div className="space-y-2 text-sm">
          {history.map((h, i) => (
            <div key={h.id ?? i} className="rounded border border-white/5 px-3 py-2">
              {h.name ?? h.engine} · {t("replay.trades")} {h.trades ?? "—"} · PF{" "}
              {h.profit_factor != null ? formatNumber(h.profit_factor) : "—"} ·{" "}
              {h.is_synthetic ? t("backtest.synthetic") : t("backtest.imported")}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
