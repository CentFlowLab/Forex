import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type AppSettings, type AgentAssignment } from "../api/client";
import { formatNumber } from "../i18n/format";

const MODELS: Record<string, string[]> = {
  xai: ["grok-4-latest", "grok-3", "grok-3-mini", "grok-2-latest"],
  openai: ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini"],
  anthropic: [
    "claude-sonnet-4-20250514",
    "claude-3-7-sonnet-latest",
    "claude-3-5-sonnet-latest",
    "claude-3-5-haiku-latest",
  ],
};

export function Settings() {
  const { t } = useTranslation();
  const [data, setData] = useState<AppSettings | null>(null);
  const [env, setEnv] = useState("PAPER");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const load = () =>
    api.settings().then((s) => {
      setData(s);
      setEnv(s.environment);
    });

  useEffect(() => {
    load().catch((e) => setErr(String(e)));
  }, []);

  const saveEnv = async () => {
    setErr("");
    try {
      const next = await api.updateSettings({ trading_environment: env });
      setData(next);
      setMsg(t("settings.saved"));
    } catch (e) {
      setErr(String(e));
    }
  };

  const saveAgent = async (agent: AgentAssignment) => {
    setErr("");
    try {
      await api.updateAgent(agent.role, {
        provider: agent.provider,
        model: agent.model,
        enabled: agent.enabled,
        temperature: agent.temperature,
      });
      setMsg(t("settings.agentSaved", { role: t(`agents.${agent.role}`, { defaultValue: agent.role }) }));
      await load();
    } catch (e) {
      setErr(String(e));
    }
  };

  if (!data) return <div className="text-slate-400">{t("settings.loading")}</div>;

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">{t("settings.title")}</h1>
      {msg && <div className="rounded-lg bg-accent-buy/10 px-3 py-2 text-sm text-accent-buy">{msg}</div>}
      {err && <div className="rounded-lg bg-accent-sell/10 px-3 py-2 text-sm text-accent-sell">{err}</div>}

      <section className="panel p-4">
        <div className="panel-title">{t("settings.envTitle")}</div>
        <p className="mt-2 text-sm text-slate-400">{t("settings.envHelp")}</p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {["BACKTEST", "PAPER", "LIVE"].map((e) => (
            <button
              key={e}
              onClick={() => setEnv(e)}
              className={`rounded-lg px-3 py-1.5 text-sm ${env === e ? "bg-white/15" : "text-slate-400"}`}
            >
              {e}
            </button>
          ))}
          <button className="rounded-lg bg-accent-gold/20 px-3 py-1.5 text-sm text-accent-gold" onClick={saveEnv}>
            {t("actions.apply")}
          </button>
        </div>
        <div className="mt-3 grid gap-2 text-sm text-slate-400 md:grid-cols-2">
          <div>
            {t("settings.riskPerTrade")}: {formatNumber(data.max_risk_per_trade_pct)} %
          </div>
          <div>
            {t("settings.dailyLoss")}: {formatNumber(data.max_daily_loss_pct)} %
          </div>
          <div>
            {t("settings.concurrent")}: {data.max_concurrent_positions}
          </div>
          <div>
            {t("settings.maxSpread")}: {formatNumber(data.max_spread_pips)} pips
          </div>
          <div>
            {t("settings.maxSlippage")}: {formatNumber(data.max_slippage_pips)} pips
          </div>
          <div>
            {t("settings.liveAuthorized")}: {data.live_authorized ? t("settings.yes") : t("settings.no")}
          </div>
        </div>
      </section>

      <section className="panel p-4">
        <div className="panel-title mb-3">{t("settings.providers")}</div>
        <div className="grid gap-3 md:grid-cols-3">
          {data.providers.map((p) => (
            <div key={p.name} className="rounded-lg border border-white/10 p-3">
              <div className="font-medium uppercase">{p.name}</div>
              <div className={p.configured ? "text-accent-buy" : "text-accent-hold"}>
                {p.configured ? t("settings.configured") : t("settings.disabledNoKey")}
              </div>
              <div className="mt-1 text-xs text-slate-500">{p.models.join(", ")}</div>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs text-slate-500">{t("settings.keysHelp")}</p>
      </section>

      <section className="space-y-3">
        <div className="panel-title">{t("settings.modelPerAgent")}</div>
        {data.agents.map((agent, idx) => (
          <AgentEditor
            key={agent.role}
            agent={agent}
            onChange={(next) => {
              const copy = { ...data, agents: data.agents.slice() };
              copy.agents[idx] = next;
              setData(copy);
            }}
            onSave={() => saveAgent(agent)}
          />
        ))}
      </section>
    </div>
  );
}

function AgentEditor({
  agent,
  onChange,
  onSave,
}: {
  agent: AgentAssignment;
  onChange: (a: AgentAssignment) => void;
  onSave: () => void;
}) {
  const { t } = useTranslation();
  const models = MODELS[agent.provider] || [];
  return (
    <div className="panel grid gap-3 p-4 md:grid-cols-5 md:items-end">
      <div>
        <div className="panel-title">{t("settings.agent")}</div>
        <div className="mt-1 text-sm">{t(`agents.${agent.role}`, { defaultValue: agent.display_name ?? agent.role })}</div>
      </div>
      <label className="text-xs text-slate-400">
        {t("settings.provider")}
        <select
          className="mt-1 w-full rounded bg-ink-800 p-2 text-sm text-white"
          value={agent.provider}
          onChange={(e) =>
            onChange({
              ...agent,
              provider: e.target.value,
              model: (MODELS[e.target.value] || [agent.model])[0],
            })
          }
        >
          <option value="xai">xAI (Grok)</option>
          <option value="openai">OpenAI (GPT)</option>
          <option value="anthropic">Anthropic (Claude)</option>
        </select>
      </label>
      <label className="text-xs text-slate-400">
        {t("settings.model")}
        <select
          className="mt-1 w-full rounded bg-ink-800 p-2 text-sm text-white"
          value={agent.model}
          onChange={(e) => onChange({ ...agent, model: e.target.value })}
        >
          {models.includes(agent.model) ? null : <option value={agent.model}>{agent.model}</option>}
          {models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={agent.enabled}
          onChange={(e) => onChange({ ...agent, enabled: e.target.checked })}
        />
        {t("actions.active")}
      </label>
      <button className="rounded-lg bg-white/10 px-3 py-2 text-sm hover:bg-white/15" onClick={onSave}>
        {t("actions.save")}
      </button>
    </div>
  );
}
