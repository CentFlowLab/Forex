import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type AICallRow, type AIStatus } from "../api/client";
import { formatDateTime, formatNumber } from "../i18n/format";

export function AIUsage() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<AIStatus | null>(null);
  const [calls, setCalls] = useState<AICallRow[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = async () => {
    const [s, c] = await Promise.all([api.aiStatus(), api.aiCalls(40)]);
    setStatus(s);
    setCalls(c);
  };

  useEffect(() => {
    load().catch((e) => setErr(String(e)));
  }, []);

  const onBlock = async () => {
    setBusy(true);
    setErr("");
    try {
      await api.aiBlock();
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onEnable = async () => {
    if (!window.confirm(t("aiUsage.confirmEnable"))) return;
    setBusy(true);
    setErr("");
    try {
      await api.aiSetEnabled(true);
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!status) {
    return <div className="py-16 text-center text-slate-400">{err || t("app.loading")}</div>;
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">{t("aiUsage.title")}</h1>
        <p className="mt-1 text-sm text-slate-400">{t("aiUsage.help")}</p>
      </div>

      <div className="rounded-lg border border-accent-gold/20 bg-accent-gold/5 px-3 py-2 text-xs text-accent-gold">
        {status.cost_disclaimer}
      </div>
      <div className="rounded-lg border border-white/10 bg-ink-800/60 px-3 py-2 text-xs text-slate-300">
        {t("aiUsage.blockNote")}
      </div>

      {err && (
        <div className="rounded-lg border border-accent-sell/30 bg-accent-sell/10 px-3 py-2 text-sm text-accent-sell">
          {err}
        </div>
      )}

      <section className="panel grid gap-3 p-4 md:grid-cols-4">
        <div>
          <div className="panel-title mb-1">{t("aiUsage.state")}</div>
          <div className={`text-lg ${status.ai_enabled ? "text-emerald-300" : "text-accent-sell"}`}>
            {status.ai_enabled ? t("aiUsage.on") : t("aiUsage.off")}
          </div>
          {status.emergency_block && (
            <div className="text-xs text-accent-sell">{t("aiUsage.emergency")}</div>
          )}
        </div>
        <div>
          <div className="panel-title mb-1">{t("aiUsage.sessionCost")}</div>
          <div className="text-lg text-white">${formatNumber(status.estimated_session_cost_usd, { maximumFractionDigits: 4 })}</div>
        </div>
        <div>
          <div className="panel-title mb-1">{t("aiUsage.dailyCost")}</div>
          <div className="text-lg text-white">${formatNumber(status.estimated_daily_cost_usd, { maximumFractionDigits: 4 })}</div>
          <div className="text-xs text-slate-500">
            / ${formatNumber(status.daily_budget_usd)} {t("aiUsage.budget")}
          </div>
        </div>
        <div>
          <div className="panel-title mb-1">{t("aiUsage.callsToday")}</div>
          <div className="text-lg text-white">{status.calls_logged_today}</div>
          <div className="text-xs text-slate-500">
            {t("aiUsage.authOpen")}: {status.authorizations_open}
          </div>
        </div>
      </section>

      <div className="flex flex-wrap gap-2">
        <button
          className="rounded-lg bg-accent-sell/20 px-3 py-2 text-sm text-accent-sell"
          disabled={busy}
          onClick={onBlock}
        >
          {t("aiUsage.blockButton")}
        </button>
        <button className="rounded-lg bg-white/10 px-3 py-2 text-sm" disabled={busy || status.ai_enabled} onClick={onEnable}>
          {t("aiUsage.enableButton")}
        </button>
        <button className="rounded-lg bg-white/10 px-3 py-2 text-sm" disabled={busy} onClick={() => load()}>
          {t("aiUsage.refresh")}
        </button>
      </div>

      <section className="panel p-4">
        <div className="panel-title mb-2">{t("aiUsage.lastCall")}</div>
        {status.last_call ? (
          <div className="text-sm text-slate-300">
            {status.last_call.created_at ? formatDateTime(status.last_call.created_at, "UTC") + " UTC" : "—"} ·{" "}
            {status.last_call.provider}/{status.last_call.model} · {status.last_call.role} · {status.last_call.status} · $
            {formatNumber(status.last_call.estimated_cost_usd, { maximumFractionDigits: 4 })}
          </div>
        ) : (
          <div className="text-sm text-slate-500">{t("aiUsage.noCalls")}</div>
        )}
      </section>

      <section className="panel p-4">
        <div className="panel-title mb-2">{t("aiUsage.history")}</div>
        <div className="overflow-auto">
          <table className="w-full text-left text-xs text-slate-300">
            <thead className="text-slate-500">
              <tr>
                <th className="py-1">{t("aiUsage.colTime")}</th>
                <th>{t("aiUsage.colAgent")}</th>
                <th>{t("aiUsage.colModel")}</th>
                <th>{t("aiUsage.colAuth")}</th>
                <th>{t("aiUsage.colStatus")}</th>
                <th>{t("aiUsage.colCost")}</th>
              </tr>
            </thead>
            <tbody>
              {calls.map((c) => (
                <tr key={c.id} className="border-t border-white/5">
                  <td className="py-1.5">{c.created_at ? formatDateTime(c.created_at, "UTC") : "—"}</td>
                  <td>{c.role || "—"}</td>
                  <td>
                    {c.provider}/{c.model}
                  </td>
                  <td>{c.authorized ? t("aiUsage.yes") : t("aiUsage.no")}</td>
                  <td>{c.status}</td>
                  <td>${formatNumber(c.estimated_cost_usd, { maximumFractionDigits: 4 })}</td>
                </tr>
              ))}
              {calls.length === 0 && (
                <tr>
                  <td colSpan={6} className="py-3 text-slate-500">
                    {t("aiUsage.noCalls")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
