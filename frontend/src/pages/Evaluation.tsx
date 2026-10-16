import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type EvalResult } from "../api/client";

export function Evaluation() {
  const { t } = useTranslation();
  const [start, setStart] = useState("2024-01-02T00:00:00Z");
  const [end, setEnd] = useState("2024-02-01T00:00:00Z");
  const [result, setResult] = useState<EvalResult | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [fileMsg, setFileMsg] = useState("");

  const run = async () => {
    setBusy(true);
    setErr("");
    try {
      const out = await api.evaluate({
        symbol: "EURUSD",
        timeframe: "M15",
        start,
        end,
        providers: ["xai", "openai", "anthropic"],
        max_bars: 3,
      });
      setResult(out);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onFile = async (file: File | undefined) => {
    if (!file) return;
    try {
      const out = await api.importCsv(file, "M15");
      setFileMsg(t("evaluation.imported", { count: out.imported }));
    } catch (e) {
      setErr(String(e));
    }
  };

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">{t("evaluation.title")}</h1>
      <p className="text-sm text-slate-400">{t("evaluation.help")}</p>
      <section className="panel grid gap-3 p-4 md:grid-cols-2">
        <label className="text-xs text-slate-400">
          {t("market.start")}
          <input className="mt-1 w-full rounded bg-ink-800 p-2 text-sm" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className="text-xs text-slate-400">
          {t("market.end")}
          <input className="mt-1 w-full rounded bg-ink-800 p-2 text-sm" value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>
        <button className="rounded-lg bg-accent-gold/20 px-3 py-2 text-sm text-accent-gold md:col-span-2" disabled={busy} onClick={run}>
          {busy ? t("evaluation.evaluating") : t("actions.compareProviders")}
        </button>
      </section>
      <section className="panel p-4 text-sm">
        <div className="panel-title">{t("evaluation.importCsv")}</div>
        <p className="mt-2 text-slate-400">{t("evaluation.importHelp")}</p>
        <input
          className="mt-3 text-sm"
          type="file"
          accept=".csv,text/csv"
          onChange={(e) => onFile(e.target.files?.[0])}
        />
        {fileMsg && <div className="mt-2 text-accent-buy">{fileMsg}</div>}
      </section>
      {err && <div className="rounded-lg bg-accent-sell/10 p-3 text-sm text-accent-sell">{err}</div>}
      {result && (
        <section className="panel p-4">
          <div className="panel-title">{t("evaluation.result")}</div>
          <pre className="mt-2 overflow-auto text-xs text-slate-300">{JSON.stringify(result, null, 2)}</pre>
        </section>
      )}
    </div>
  );
}
