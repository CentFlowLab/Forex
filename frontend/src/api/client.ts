const API = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/health"),
  robotStatus: () => request<RobotStatus>("/robot/status"),
  robotStart: () => request<RobotStatus>("/robot/start", { method: "POST" }),
  robotStop: () => request<RobotStatus>("/robot/stop", { method: "POST" }),
  robotAiEstimate: () => request<RobotAiEstimate>("/robot/ai/estimate"),
  robotAiAuthorize: (confirmed: boolean) =>
    request<Record<string, unknown>>("/robot/ai/authorize", {
      method: "POST",
      body: JSON.stringify({ confirmed }),
    }),
  robotAiManual: (authToken: string) =>
    request<Record<string, unknown>>("/robot/ai/manual", {
      method: "POST",
      body: JSON.stringify({ confirmed: true, auth_token: authToken }),
    }),
  robotAiAutonomous: () => request<RobotOpenAIAutonomous>("/robot/ai/autonomous"),
  robotAiAutonomousArm: () =>
    request<RobotOpenAIAutonomous>("/robot/ai/autonomous/arm", { method: "POST" }),
  robotAiAutonomousActivate: () =>
    request<RobotOpenAIAutonomous>("/robot/ai/autonomous/activate", {
      method: "POST",
      body: JSON.stringify({ confirmed: true }),
    }),
  robotAiAutonomousDeactivate: () =>
    request<RobotOpenAIAutonomous>("/robot/ai/autonomous/deactivate", { method: "POST" }),
  robotDemoStatus: () => request<RobotDemoAuth>("/robot/demo"),
  robotDemoActivate: (body: { confirmed: boolean }) =>
    request<RobotDemoAuth>("/robot/demo/activate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  robotDemoDeactivate: () =>
    request<RobotDemoAuth>("/robot/demo/deactivate", { method: "POST" }),
  robotDemoPause: (paused: boolean) =>
    request<RobotDemoAuth>("/robot/demo/pause", {
      method: "POST",
      body: JSON.stringify({ paused }),
    }),
  robotDemoEmergency: () =>
    request<RobotDemoAuth>("/robot/demo/emergency", { method: "POST" }),
  robotDemoClosePositions: (confirmed: boolean) =>
    request<Record<string, unknown>>("/robot/demo/close-positions", {
      method: "POST",
      body: JSON.stringify({ confirmed }),
    }),
  dashboard: () => request<Dashboard>("/dashboard"),
  snapshot: (tf = "M15") => request<MarketSnapshot>(`/market/snapshot?timeframe=${tf}`),
  candles: (tf = "M15", source?: string) =>
    request<CandleResponse>(
      `/market/candles?timeframe=${tf}&limit=500${source ? `&source=${source}` : ""}`,
    ),
  marketSources: () => request<MarketSources>("/market/sources"),
  quoteFeed: () => request<QuoteFeedStatus>("/market/quote-feed"),
  downloadDukascopy: (start: string, end: string) =>
    request<DownloadJobResult>("/market/download/dukascopy", {
      method: "POST",
      body: JSON.stringify({ start, end, symbol: "EURUSD" }),
    }),
  downloadStatus: (id: number) => request<DownloadJobResult>(`/market/download/${id}`),
  createReplay: (body: ReplayCreateBody) =>
    request<ReplayStatus>("/replay", { method: "POST", body: JSON.stringify(body) }),
  replayPlay: (id: number) => request<ReplayStatus>(`/replay/${id}/play`, { method: "POST" }),
  replayPause: (id: number) => request<ReplayStatus>(`/replay/${id}/pause`, { method: "POST" }),
  replayRestart: (id: number) => request<ReplayStatus>(`/replay/${id}/restart`, { method: "POST" }),
  replaySpeed: (id: number, speed: number) =>
    request<ReplayStatus>(`/replay/${id}/speed`, { method: "POST", body: JSON.stringify({ speed }) }),
  replayStatus: (id: number) => request<ReplayStatus>(`/replay/${id}`),
  replayReport: (id: number) => request<Record<string, unknown>>(`/replay/${id}/report`),
  settings: () => request<AppSettings>("/settings"),
  updateSettings: (body: Partial<AppSettingsUpdate>) =>
    request<AppSettings>("/settings", { method: "PUT", body: JSON.stringify(body) }),
  updateAgent: (role: string, body: AgentUpdate) =>
    request<AgentAssignment>(`/settings/agents/${role}`, { method: "PUT", body: JSON.stringify(body) }),
  kill: (active: boolean) => request<{ kill_switch: boolean }>(`/kill-switch?active=${active}`, { method: "POST" }),
  runCollaboration: (
    execute: boolean,
    timeframe = "M15",
    workflow?: string,
    debateRounds?: number,
    source?: string,
  ) =>
    request<CollaborationResult>("/collaboration/run", {
      method: "POST",
      body: JSON.stringify({
        symbol: "EURUSD",
        timeframe,
        execute,
        workflow: workflow ?? "tradingagents",
        debate_rounds: debateRounds ?? null,
        source: source ?? "auto",
      }),
    }),
  analysisStart: (body: AnalysisStartBody) =>
    request<CollaborationResult>("/analysis/start", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  analysisStop: (id: number) =>
    request<{ id: number; cancel_requested: boolean; status: string }>(`/analysis/${id}/stop`, {
      method: "POST",
    }),
  analysisGet: (id: number) => request<AnalysisSummary>(`/analysis/${id}`),
  analysisList: (limit = 30) => request<AnalysisSummary[]>(`/analysis?limit=${limit}`),
  analysisMemory: (limit = 50) => request<MemoryItem[]>(`/analysis/memory?limit=${limit}`),
  compareWorkflows: (body: BacktestBody) =>
    request<Record<string, unknown>>("/backtest/compare-workflows", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  decisions: () => request<Decision[]>("/decisions"),
  logs: () => request<AuditItem[]>("/logs"),
  closePosition: (id: number) => request(`/positions/${id}/close`, { method: "POST" }),
  backtest: (body: BacktestBody) =>
    request<BacktestResult>("/backtest", { method: "POST", body: JSON.stringify(body) }),
  backtests: () => request<BacktestListItem[]>("/backtest"),
  evaluate: (body: EvalBody) =>
    request<EvalResult>("/evaluation", { method: "POST", body: JSON.stringify(body) }),
  evaluations: () => request<EvalResult[]>("/evaluation"),
  importCsv: async (file: File, timeframe: string) => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${API}/market/import?symbol=EURUSD&timeframe=${timeframe}`, {
      method: "POST",
      body: form,
    });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  },
  adaptiveDashboard: () => request<AdaptiveDashboard>("/adaptive/dashboard"),
  adaptiveRegime: () => request<AdaptiveRegime>("/adaptive/regime"),
  adaptiveCatalog: () => request<{ strategies: AdaptiveCatalogItem[] }>("/adaptive/catalog"),
  adaptiveHistory: (start?: string, end?: string) => {
    const q = new URLSearchParams();
    if (start) q.set("start", start);
    if (end) q.set("end", end);
    const qs = q.toString();
    return request<AdaptiveHistorySummary>(`/adaptive/history${qs ? `?${qs}` : ""}`);
  },
  adaptiveHistoryPrepare: (start: string, end: string) =>
    request<AdaptivePreparation>(
      `/adaptive/history/prepare?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`,
    ),
  adaptiveHistoryDownload: (body: AdaptiveHistoryDownloadBody) =>
    request<AdaptiveDownloadJob>("/adaptive/history/download", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  adaptiveHistoryDownloadStatus: (id: number) =>
    request<AdaptiveDownloadJob>(`/adaptive/history/download/${id}`),
  adaptiveHistoryDownloadCancel: (id: number) =>
    request<AdaptiveDownloadJob>(`/adaptive/history/download/${id}/cancel`, { method: "POST" }),
  adaptiveStartExperiment: (body: AdaptiveExperimentBody) =>
    request<AdaptiveExperiment>("/adaptive/experiments", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  adaptiveExperiment: (id: number) => request<AdaptiveExperiment>(`/adaptive/experiments/${id}`),
  adaptiveExperiments: () => request<AdaptiveExperiment[]>("/adaptive/experiments"),
  adaptiveSchedule: (body: AdaptiveScheduleBody) =>
    request<AdaptiveSchedule>("/adaptive/schedule", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  autonomyDashboard: () => request<AutonomyDashboard>("/autonomy/dashboard"),
  autonomyConfig: (body: AutonomyConfigBody) =>
    request<Record<string, unknown>>("/autonomy/config", { method: "PUT", body: JSON.stringify(body) }),
  autonomyStart: () => request<Record<string, unknown>>("/autonomy/cycle/start", { method: "POST" }),
  autonomyPause: () => request<Record<string, unknown>>("/autonomy/cycle/pause", { method: "POST" }),
  autonomyResume: () => request<Record<string, unknown>>("/autonomy/cycle/resume", { method: "POST" }),
  autonomyPaperEnable: () => request<Record<string, unknown>>("/autonomy/paper/enable", { method: "POST" }),
  autonomyPaperDisable: () => request<Record<string, unknown>>("/autonomy/paper/disable", { method: "POST" }),
  aiStatus: () => request<AIStatus>("/ai/status"),
  aiEstimate: (debateRounds = 2) =>
    request<AIEstimate>(`/ai/estimate?debate_rounds=${debateRounds}`),
  aiAuthorize: (body: { confirmed: boolean; debate_rounds?: number; workflow?: string }) =>
    request<AIAuthorization>("/ai/authorize", { method: "POST", body: JSON.stringify(body) }),
  aiBlock: () => request<{ ai_enabled: boolean; message: string }>("/ai/block", { method: "POST" }),
  aiSetEnabled: (enabled: boolean) =>
    request<{ ai_enabled: boolean; message: string }>("/ai/enabled", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  aiCalls: (limit = 50) => request<AICallRow[]>(`/ai/calls?limit=${limit}`),
};

export type AIStatus = {
  ai_enabled: boolean;
  emergency_block: boolean;
  require_manual_approval: boolean;
  max_calls_per_analysis: number;
  max_concurrent_analyses: number;
  max_cost_per_analysis_usd: number;
  daily_budget_usd: number;
  session_budget_usd: number;
  estimated_daily_cost_usd: number;
  estimated_session_cost_usd: number;
  cost_disclaimer: string;
  calls_logged_today: number;
  authorizations_open: number;
  active_analyses: number;
  last_call: {
    created_at?: string | null;
    provider: string;
    model: string;
    role: string;
    status: string;
    authorized: boolean;
    estimated_cost_usd: number;
    analysis_id: number;
  } | null;
};

export type AIEstimate = {
  model: string;
  max_calls: number;
  suggested_calls: number;
  reserve_per_call_usd: number;
  estimated_max_cost_usd: number;
  disclaimer: string;
  ai_enabled: boolean;
};

export type AIAuthorization = {
  authorization_id: string;
  max_calls: number;
  reserved_cost_usd: number;
  expires_at: string;
  estimate: AIEstimate;
  single_use: boolean;
  note: string;
};

export type AICallRow = {
  id: number;
  created_at?: string | null;
  provider: string;
  model: string;
  role: string;
  analysis_id: number;
  reason: string;
  authorized: boolean;
  prompt_tokens: number;
  completion_tokens: number;
  estimated_cost_usd: number;
  status: string;
  error: string;
  attempt: number;
};

export type AdaptiveRegime = {
  label: string;
  label_pt: string;
  trend_slope?: number | null;
  atr_ratio?: number | null;
  bars_used?: number;
  as_of?: string;
  data_source?: string;
  is_synthetic?: boolean;
  message?: string;
};

export type AdaptiveCatalogItem = {
  key: string;
  display_name: string;
  description: string;
  default_params: Record<string, number>;
  preferred_regimes: string[];
  complexity: number;
};

export type AdaptiveStrategyCard = {
  id: number;
  strategy_key: string;
  display_name: string;
  params: Record<string, unknown>;
  status: string;
  preferred_regimes: string[];
  validation_score: number;
  out_of_sample_score: number;
  max_drawdown_pct: number;
  net_pnl: number;
  trading_costs: number;
  experiment_count: number;
  reason: string;
};

export type AdaptiveExperiment = {
  id: number;
  name: string;
  status: string;
  data_source: string;
  is_synthetic: boolean;
  bars_total: number;
  train_bars: number;
  validation_bars: number;
  test_bars: number;
  candidates_tested: number;
  candidates_total?: number;
  progress_pct?: number;
  progress_message?: string;
  selected_action: string;
  current_regime: string;
  current_regime_pt?: string;
  openai_authorized: boolean;
  notes: string;
  error: string;
  results?: Record<string, unknown>;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  async?: boolean;
  poll_url?: string;
};

export type AdaptiveSchedule = {
  id: number;
  enabled: boolean;
  interval_minutes: number;
  lookback_days: number;
  authorize_openai: boolean;
  notes: string;
  next_run_at?: string | null;
  last_run_at?: string | null;
};

export type AdaptiveExperimentBody = {
  start: string;
  end: string;
  symbol?: string;
  timeframe?: string;
  name?: string;
  authorize_openai?: boolean;
  require_real_data?: boolean;
};

export type AdaptiveHistoryCoverage = {
  oldest_available?: string | null;
  newest_available?: string | null;
  bars_in_db: number;
  bars_valid_real: number;
  bars_synthetic_in_range: number;
  expected_trading_bars: number;
  coverage_pct: number;
  missing_trading_slots: number;
  missing_hours_count: number;
  continuity_gap_examples: string[];
  weekend_slots_excluded: number;
  ohlc_errors: number;
  source: string;
  label: string;
  distinction: {
    real_dukascopy: boolean;
    has_synthetic_contamination: boolean;
    synthetic_only: boolean;
  };
};

export type AdaptivePreparation = {
  ready: boolean;
  blocked: boolean;
  reasons: string[];
  message: string;
  coverage: AdaptiveHistoryCoverage;
  download_estimate?: {
    missing_hours: number;
    estimated_duration_label: string;
    estimated_size_mb: number;
    disclaimer: string;
  };
};

export type AdaptiveDownloadJob = {
  id: number;
  status: string;
  progress_pct: number;
  message: string;
  error: string;
  hours_done: number;
  hours_total: number;
  cancel_requested: boolean;
  bars?: { M5: number; M15: number; H1: number; H4: number };
  ai_calls: boolean;
};

export type AdaptiveHistorySummary = {
  default_period: {
    start: string;
    end: string;
    symbol: string;
    timeframe: string;
    label_pt: string;
  };
  cache_dukascopy: {
    by_timeframe: Record<
      string,
      {
        bars: number;
        oldest?: string | null;
        newest?: string | null;
        real_bars: number;
        synthetic_bars: number;
      }
    >;
  };
  coverage: AdaptiveHistoryCoverage;
  preparation: AdaptivePreparation;
  download_estimate?: AdaptivePreparation["download_estimate"];
  latest_download?: AdaptiveDownloadJob | null;
  active_download_id?: number | null;
};

export type AdaptiveHistoryDownloadBody = {
  start?: string;
  end?: string;
  symbol?: string;
  timeframe?: string;
  confirmed: boolean;
  resume_job_id?: number;
};

export type AdaptiveScheduleBody = {
  enabled: boolean;
  interval_minutes?: number;
  lookback_days?: number;
  authorize_openai?: boolean;
};

export type AdaptiveDashboard = {
  title: string;
  disclaimer: string;
  regime: AdaptiveRegime;
  catalog: AdaptiveCatalogItem[];
  strategies: Record<string, AdaptiveStrategyCard[]>;
  performance: Array<{
    id: number;
    name: string;
    key: string;
    status: string;
    validation_score: number;
    out_of_sample_score: number;
    max_drawdown_pct: number;
    net_pnl: number;
    trading_costs: number;
    experiment_count: number;
    reason: string;
  }>;
  out_of_sample: Array<Record<string, unknown>>;
  changes: Array<{
    id: number;
    action: string;
    from_status: string;
    to_status: string;
    reason: string;
    created_at?: string | null;
  }>;
  experiments: AdaptiveExperiment[];
  schedule?: AdaptiveSchedule | null;
  stats: Record<string, number>;
  risk: Record<string, unknown>;
  latest_selection?: Record<string, unknown> | null;
  costs_model?: Record<string, unknown> | null;
  history?: AdaptiveHistorySummary;
  ai_enabled?: boolean;
  ai_calls_in_adaptive?: boolean;
};

export type AutonomyConfigBody = {
  enabled?: boolean;
  paused?: boolean;
  paper_enabled?: boolean;
  interval_minutes?: number;
  lookback_days?: number;
  max_combos?: number;
};

export type AutonomyCycleLog = {
  id: number;
  started_at?: string | null;
  finished_at?: string | null;
  duration_seconds: number;
  status: string;
  message: string;
  experiment_id: number;
  strategies_count: number;
};

export type AutonomyDashboard = {
  title: string;
  disclaimer: string;
  engine: {
    enabled: boolean;
    paused: boolean;
    paper_enabled: boolean;
    interval_minutes: number;
    lookback_days: number;
    max_combos: number;
    last_cycle_at?: string | null;
    next_cycle_at?: string | null;
    last_message: string;
    last_error: string;
    cycles_completed: number;
    ai_calls: number;
    thread_alive: boolean;
    active_experiment_id?: number | null;
    display_status?: string;
    run_status?: string;
    catalog_strategies?: string[];
  };
  last_cycle?: AutonomyCycleLog | null;
  recent_cycles?: AutonomyCycleLog[];
  security: { live_trading_locked: boolean; ai_enabled: boolean; risk_limits_auto_change: boolean };
  strategies: Record<string, Array<{ id: number; key: string; name: string; status: string; score: number; oos: number; reason: string }>>;
  paper: {
    open: Array<{
      id: number;
      side: string;
      entry: number;
      stop: number;
      take: number;
      strategy: string;
      reason: string;
      opened_at?: string | null;
      is_replay: boolean;
    }>;
    closed: Array<{ id: number; side: string; pnl: number; costs: number; strategy: string; closed_at?: string | null }>;
    net_pnl: number;
    equity_approx: number;
    max_drawdown_pct: number;
    trades: number;
    win_rate_pct: number;
    profit_factor: number;
    costs: number;
  };
  hypotheses: Array<{ id: number; strategy_key: string; status: string; rationale: string; priority: number }>;
  running_tasks: Array<{ id: number; status: string; progress_pct: number; progress_message: string }>;
  alerts: Array<{ id: number; level: string; message: string; created_at: string }>;
  rejection_audit: {
    experiment_id?: number | null;
    message: string;
    candidates?: number;
    approved?: number;
    rejection_counts?: Record<string, number>;
    promotion_criteria?: {
      min_score: number;
      min_pf_validation: number;
      min_pf_test: number;
      min_validation_trades: number;
    };
  };
  catalog_size: number;
};

export type RobotAiEstimate = {
  model: string;
  max_calls: number;
  estimated_cost_usd: number;
  estimated_max_cost_usd: number;
  daily_budget_usd: number;
  session_budget_usd: number;
  ai_enabled: boolean;
  disclaimer: string;
};

export type RadarVsOpenAI = {
  radar_best_candidate?: string | null;
  radar_best_score?: number | null;
  openai_decision?: string | null;
  openai_symbol?: string | null;
  discordance_reason?: string;
  entry_conditions?: string;
  quality_issues?: string[];
  quality_failure_reasons?: string[];
  quality_failure_summary?: string;
  decision_quality?: string;
  analysis_incomplete?: boolean;
  ai_decision_inconsistent?: boolean;
  ev_inconsistency?: {
    ai_decision_inconsistent?: boolean;
    inconsistency_type?: string | null;
    real_block_reason?: string | null;
    matches?: Array<{
      symbol?: string;
      motor_expected_net_after_costs?: number;
      motor_cost_usd?: number;
      openai_claimed_ev?: number;
      equals_net_minus_cost?: number;
      diagnosis?: string;
    }>;
  };
  justification_quality_ok?: boolean;
  local_priority_hint?: Array<{ symbol?: string; score?: number }>;
  directional_opportunities_sent?: Array<Record<string, unknown>>;
  note?: string;
};

export type OpenAIRealDecision = {
  id?: number;
  available?: boolean;
  source?: string;
  is_real_paid_openai?: boolean;
  is_synthetic_test?: boolean;
  mode?: string;
  decision?: string | null;
  decision_pt?: string | null;
  symbol?: string;
  justification?: string;
  confidence?: string;
  risks?: string;
  invalidating_scenario?: string;
  favoring_scenario?: string;
  why_preferred?: string;
  missing_information?: string;
  time_horizon?: string;
  model?: string;
  estimated_cost_usd?: number | null;
  executed?: boolean;
  analyzed_at?: string | null;
  persisted_path?: string;
  execution_note?: string;
  entry_conditions?: string;
  radar_vs_openai?: RadarVsOpenAI;
  message?: string;
};

export type OpenAIRealPanel = {
  available?: boolean;
  latest?: OpenAIRealDecision;
  recent?: OpenAIRealDecision[];
  count?: number;
  message?: string;
};

export type LastRejection = {
  reason?: string;
  at?: string | null;
  decision_id?: number | null;
  attempt_id?: number | null;
  side?: string | null;
  symbol?: string | null;
  order_send_called?: boolean;
  stale?: boolean;
};

export type PanelStateChip = {
  code: string;
  label: string;
  reason?: string | null;
  at?: string | null;
  decision_id?: number | null;
  attempt_id?: number | null;
  side?: string | null;
  symbol?: string | null;
  order_send_called?: boolean;
  order_check_called?: boolean;
  ticket?: string | null;
  last_rejection?: LastRejection | null;
  attempt_stale?: boolean;
};

export type WhyNotTrading = {
  code: string;
  message: string;
  decision_id?: number;
  decision_at?: string | null;
  prior_order_rejection?: string;
  attempt_decision_id?: number;
  attempt_at?: string | null;
};

export type AccountMt5 = {
  available?: boolean;
  login?: number | string | null;
  login_masked?: string;
  server?: string;
  trade_mode?: number | null;
  trade_mode_label?: string;
  balance?: number | null;
  equity?: number | null;
  margin?: number | null;
  margin_free?: number | null;
  profit?: number | null;
  currency?: string;
  note?: string;
  trade_allowed?: boolean;
  terminal_trade_allowed?: boolean;
  autotrading_enabled?: boolean;
  autotrading_warning?: string;
};

export type RobotDemoAuth = {
  active?: boolean;
  login?: string;
  login_masked?: string;
  server?: string;
  trade_mode_label?: string;
  balance?: number | null;
  equity?: number | null;
  currency?: string;
  emergency?: boolean;
  pause_entries?: boolean;
  message?: string;
};

export type RobotStatus = {
  title: string;
  disclaimer: string;
  running: boolean;
  thread_alive: boolean;
  panel_generated_at?: string;
  robot_state?: PanelStateChip;
  mt5_state?: PanelStateChip;
  openai_state?: PanelStateChip;
  execution_state?: PanelStateChip;
  why_not_trading?: WhyNotTrading;
  timestamps?: {
    panel_generated_at?: string | null;
    last_quote_at?: string | null;
    last_openai_analysis_at?: string | null;
    next_openai_analysis_at?: string | null;
    last_execution_attempt_at?: string | null;
  };
  account_mt5?: AccountMt5;
  account_simulated?: Record<string, unknown>;
  backend_link?: PanelStateChip;
  decision: string;
  decision_pt: string;
  block_reason: string;
  last_message: string;
  last_justification?: string;
  last_tick_at?: string | null;
  ticks_completed: number;
  openai_real?: OpenAIRealPanel;
  demo_auth?: RobotDemoAuth;
  ai?: {
    mode?: string;
    label?: string;
    is_simulated?: boolean;
    note?: string;
    paid_calls?: number;
    favoring?: string;
    invalidating?: string;
    risks?: string;
    time_horizon?: string;
    executed?: boolean;
    risk_approved?: boolean;
    symbol?: string | null;
    why_preferred?: string;
    evaluation?: {
      decisions?: number;
      net_pnl_simulated?: number;
      vs_hold?: string;
      max_drawdown?: number;
      disclaimer?: string;
    };
  };
  radar?: {
    label?: string;
    note?: string;
    monitored?: number;
    with_valid_data?: number;
    universe_target?: number;
    best_opportunity?: string;
    refreshing?: boolean;
    top5?: Array<{ symbol?: string; score?: number; display?: string }>;
    candidates?: Array<{ symbol?: string; score?: number; display?: string }>;
    pairs?: Array<{
      symbol?: string;
      display?: string;
      data_ok?: boolean;
      lag_seconds?: number | null;
      mid?: number | null;
      is_synthetic?: boolean;
      state?: string;
      state_label?: string;
    }>;
    pair_statuses?: Array<{
      symbol?: string;
      display?: string;
      state?: string;
      state_label?: string;
      data_ok?: boolean;
    }>;
  };
  backtest_validation?: {
    available?: boolean;
    layer?: string;
    generated_at?: string;
    message?: string;
    candidate?: {
      strategy?: string | null;
      status?: string;
      disclaimer?: string;
      metrics?: Record<string, unknown>;
      best_attempt?: Record<string, unknown>;
    };
    aggregate?: Record<string, Record<string, unknown>>;
    pairs?: Array<{
      symbol?: string;
      ok?: boolean;
      actionable_trades?: number;
      actionable_net_pnl?: number;
      actionable_win_rate?: number;
      actionable_pf?: number;
      actionable_max_dd?: number;
      baseline_buy_net?: number;
      m15_months?: number;
      reason?: string;
    }>;
    disclaimer?: string;
    layers_legend?: Record<string, string>;
  };
  directional_predictions?: {
    label?: string;
    generated_at?: string;
    ranking_metric?: string;
    actionable_count?: number;
    disclaimer?: string;
    top_opportunities?: Array<{
      symbol?: string;
      side?: string;
      entry?: number;
      stop_loss?: number;
      take_profit?: number;
      rr?: number;
      p_hit_tp_before_sl?: number;
      expected_net_usd?: number;
      evidence_quality?: number;
      reject_reasons?: string[];
      forecasts?: Array<{
        horizon_min?: number;
        p_up?: number;
        p_down?: number;
        p_flat?: number;
        expected_move_pips?: number;
        confidence?: number;
        most_likely?: string;
        calib_samples?: number;
        method?: string;
      }>;
    }>;
    top_buys?: Array<{ symbol?: string; expected_net_usd?: number; side?: string }>;
    top_sells?: Array<{ symbol?: string; expected_net_usd?: number; side?: string }>;
    pair_directions?: Array<{
      symbol?: string;
      display?: string;
      most_likely_direction?: string;
      best_side?: string | null;
      best_expected_net_usd?: number;
      regime?: string;
      trend_strength?: string;
    }>;
    validation?: {
      n_resolved?: number;
      directional_accuracy?: number | null;
      mean_abs_move_error_pips?: number | null;
      disclaimer?: string;
      calibration_note?: string;
    };
    layers?: Record<string, string>;
  };
  quote: {
    source: string;
    source_label?: string;
    age_seconds?: number | null;
    update_age_s?: number | null;
    tick_time_lag_s?: number | null;
    mid?: number | null;
    bid?: number | null;
    ask?: number | null;
    status?: string;
    status_label?: string;
    feed_state?: string;
    block_entries?: boolean;
    message?: string;
    claims_realtime?: boolean;
    clock_anomaly?: boolean;
    channel?: string;
    data_class?: string;
  };
  capital: {
    balance: number;
    equity: number;
    currency: string;
    unrealized_pnl: number;
    realized_pnl: number;
    session_realized_pnl?: number;
    baseline_realized_pnl?: number;
    starting_equity: number;
    is_simulated: boolean;
  };
  strategy: {
    key: string;
    approved_count: number;
    approved: Array<{ id: number; key: string; name: string }>;
  };
  open_positions: Array<{
    id: number;
    side: string;
    entry: number;
    volume: number;
    stop: number;
    take: number;
    unrealized_pnl: number;
  }>;
  trades: Array<{
    id: number;
    side: string;
    entry: number;
    exit: number;
    pnl: number;
    reason: string;
    closed_at?: string | null;
    is_test: boolean;
    is_baseline?: boolean;
    session?: string;
  }>;
  baseline_trades?: Array<{
    id: number;
    side: string;
    entry: number;
    exit: number;
    pnl: number;
    reason: string;
    closed_at?: string | null;
    is_test: boolean;
    is_baseline?: boolean;
    session?: string;
  }>;
  mt5?: {
    terminal_connected?: boolean;
    initialized?: boolean;
    account_login?: number | null;
    account_server?: string;
    claims_any_realtime?: boolean;
    message?: string;
  };
  dukascopy_complementary?: {
    channel?: string;
    status?: string;
    lag_seconds?: number | null;
    message?: string;
    blocks_robot?: boolean;
  };
  security: {
    live_trading_locked: boolean;
    ai_enabled: boolean;
    ai_calls: number;
    paid_ai_calls?: number;
    environment: string;
    auto_paid_ai?: boolean;
  };
  openai_autonomous?: RobotOpenAIAutonomous;
};

export type RobotOpenAIAutonomous = {
  model?: string;
  interval_minutes?: number;
  openai_key_configured?: boolean;
  ai_enabled?: boolean;
  armed?: boolean;
  confirmed?: boolean;
  active?: boolean;
  scheduling_active?: boolean;
  authorization_state?: string;
  schedule_state?: string;
  robot_running?: boolean;
  execution_enabled?: boolean;
  daily_budget_usd?: number;
  spent_today_usd?: number;
  remaining_today_usd?: number;
  estimate_cost_usd?: number;
  conservative_reserve_usd?: number;
  can_afford_next?: boolean;
  next_analysis_at?: string | null;
  last_run_at?: string | null;
  suspend_reason?: string;
  blockers?: string[];
  ready_for_activation?: boolean;
  paid_calls_blocked_until_confirm?: boolean;
  authorization_label?: string;
  note?: string;
  message?: string;
  error?: string;
  mt5?: { ok?: boolean; reason?: string; clock_anomaly?: boolean; pairs_ok?: number };
  last_result?: Record<string, unknown>;
};

export type Health = {
  status: string;
  environment: string;
  live_authorized: boolean;
};

export type Dashboard = {
  disclaimer: string;
  environment: string;
  is_simulated: boolean;
  live_authorized: boolean;
  live_locked?: boolean;
  kill_switch: boolean;
  synthetic_label: string;
  real_historical_label?: string;
  dukascopy_bars?: Record<string, number>;
  account: {
    balance: number;
    equity: number;
    currency: string;
    unrealized_pnl: number;
    realized_pnl: number;
    max_drawdown_pct: number;
    daily_realized_pnl: number;
    is_simulated: boolean;
  };
  metrics: {
    closed_trades: number;
    win_rate: number;
    profit_factor: number;
    net_pnl: number;
    max_drawdown_pct: number;
  };
  broker: { adapter: string; connected: boolean; message: string; is_simulated: boolean };
  positions: PositionRow[];
  closed_trades: TradeRow[];
  equity_curve: { time: string; equity: number; is_simulated: boolean }[];
  agents: Record<string, AgentCard>;
  decisions: Decision[];
  api_usage: { provider: string; prompt_tokens: number; completion_tokens: number; estimated_cost_usd: number }[];
  latest_analysis?: AnalysisSummary | null;
  ai_budget?: { daily_usd: number; session_usd: number };
  max_debate_rounds?: number;
  active_market?: {
    requested?: string | null;
    resolved_source?: string | null;
    is_synthetic?: boolean;
    label?: string;
    bars_dukascopy?: number;
    bars_synthetic?: number;
    reason?: string;
  };
  quote_feed?: QuoteFeedStatus;
};

export type QuoteFeedStatus = {
  connected: boolean;
  status: "atualizado" | "com_atraso" | "obsoleto" | "desligado";
  status_label: string;
  channel: string;
  source_label: string;
  symbol: string;
  bid: number | null;
  ask: number | null;
  mid: number | null;
  spread_pips: number | null;
  quote_ts: string | null;
  received_at: string | null;
  lag_seconds: number | null;
  update_interval_hint_seconds: number | null;
  mid_only: boolean;
  is_synthetic: boolean;
  block_entries: boolean;
  message: string;
  tick_count: number;
  reconnect_attempts: number;
  last_error: string;
  candles_updated: Record<string, number>;
  claims_realtime: boolean;
};

export type DecisionPackage = {
  instrument: string;
  market_data_timestamp: string;
  timeframe: string;
  direction: string;
  confidence_score: number;
  bull_arguments: string[];
  bear_arguments: string[];
  key_evidence: string[];
  risk_objections: string[];
  entry_proposal: number | null;
  stop_loss_proposal: number | null;
  take_profit_proposal: number | null;
  final_explanation: string;
  agent_disagreement_summary: string;
  risk_veto: boolean;
  risk_veto_reason: string;
  snapshot_id: string;
  data_freshness: string;
  models_used: string[];
  prompt_tokens: number;
  completion_tokens: number;
  estimated_cost_usd: number;
  debate_rounds: number;
  debate_executed?: boolean;
  debate_complete?: boolean;
  data_source?: string;
  is_synthetic?: boolean;
  workflow: string;
};

export type DebateRoundView = {
  round: number;
  type: string;
  bull?: { summary?: string; status?: string; error?: string };
  bear?: { summary?: string; status?: string; error?: string };
};

export type AnalysisReportLite = {
  role: string;
  status: string;
  bias?: string;
  confidence?: number;
  uncertainty?: number;
  summary?: string;
  error?: string;
  provider?: string;
  model?: string;
};

export type AnalysisSummary = {
  id: number;
  status: string;
  workflow?: string;
  recommendation: string;
  confidence: number;
  risk_veto: boolean;
  risk_veto_reason: string;
  data_freshness: string;
  data_source: string;
  is_synthetic?: boolean;
  timeframe?: string;
  debate_rounds_requested?: number;
  debate_rounds_completed: number;
  estimated_cost_usd: number;
  prompt_tokens: number;
  completion_tokens: number;
  models_used: string[];
  package?: DecisionPackage | null;
  debate?: {
    rounds_completed?: number;
    opening_round?: string;
    response_rounds_requested?: number;
    response_rounds_completed?: number;
    rounds_label?: string;
    rounds?: DebateRoundView[];
    debate_executed?: boolean;
    debate_complete?: boolean;
    bull_arguments?: string[];
    bear_arguments?: string[];
  };
  stages?: Record<string, string>;
  diagnostics?: Record<string, unknown>;
  reports?: AnalysisReportLite[];
  created_at: string;
  duration_ms?: number;
  error?: string;
  market_time?: string | null;
};

export type AnalysisStartBody = {
  symbol: string;
  timeframe: string;
  execute: boolean;
  debate_rounds: number;
  enabled_roles: Record<string, boolean>;
  source?: string;
  authorization_id?: string;
};

export type MemoryItem = {
  id: number;
  analysis_run_id: number;
  decision_id: number;
  market_snapshot_id: string;
  analysis_timestamp: string;
  symbol: string;
  timeframe: string;
  final_decision: Record<string, unknown>;
  has_outcome: boolean;
  realized_virtual_pnl: number | null;
  outcome_recorded_at: string | null;
  lessons: string[];
};

export type PositionRow = {
  id: number;
  symbol: string;
  side: string;
  volume_lots: number;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  current_price: number;
  unrealized_pnl: number;
  is_simulated: boolean;
};

export type TradeRow = {
  id: number;
  symbol: string;
  side: string;
  volume_lots: number;
  entry_price: number;
  exit_price: number;
  pnl: number;
  pips: number;
  close_reason: string;
  is_simulated: boolean;
  closed_at: string;
};

export type AgentCard = {
  role: string;
  display_name: string;
  status: string;
  bias: string;
  confidence: number;
  uncertainty: number;
  summary: string;
  error: string;
  provider: string;
  model: string;
  created_at: string;
};

export type Decision = {
  id: number;
  recommendation: string;
  risk_approved: boolean;
  risk_reason: string;
  executed: boolean;
  data_source: string;
  is_simulated: boolean;
  created_at: string;
  rationale: string;
  reports?: AgentReport[];
};

export type AgentReport = {
  role: string;
  status: string;
  bias: string;
  summary: string;
  error: string;
  provider: string;
  model: string;
  payload: Record<string, unknown>;
};

export type MarketSnapshot = {
  last_price: number | null;
  label: string;
  is_synthetic: boolean;
  data_source: string;
  quote?: { bid: number; ask: number; spread_pips: number; is_synthetic: boolean };
  bars: Record<string, Candle[]>;
  primary_indicators: Record<string, number | string | null>;
};

export type Candle = { time: number; open: number; high: number; low: number; close: number };
export type CandleResponse = {
  candles: Candle[];
  label: string;
  is_synthetic: boolean;
  source?: string;
  message?: string;
};

export type ChartMarker = {
  time: number;
  position: "aboveBar" | "belowBar";
  color: string;
  shape: string;
  text: string;
};

export type MarketSources = {
  live_locked: boolean;
  sources: { name: string; label: string; is_synthetic: boolean; bars: Record<string, number> }[];
  ai_daily_budget_usd: number;
  dukascopy_max_days: number;
};

export type DownloadJobResult = {
  id: number;
  status: string;
  progress_pct: number;
  message: string;
  error: string;
  bars: Record<string, number>;
  label: string;
  is_synthetic?: boolean;
  details?: Record<string, unknown>;
};

export type ReplayCreateBody = {
  start: string;
  end: string;
  timeframe: string;
  speed: number;
  ai_every_n_bars: number;
  execute_trades: boolean;
};

export type ReplayStatus = {
  session: {
    id: number;
    status: string;
    timeframe: string;
    source: string;
    label: string;
    is_synthetic: boolean;
    speed: number;
    ai_every_n_bars: number;
    cursor_index: number;
    bars_total: number;
    cursor_ts: string | null;
    decisions: number;
    trades: number;
    ending_equity: number;
    max_drawdown_pct: number;
    baseline_net_pnl: number;
    ai_net_pnl: number;
    api_cost_usd: number;
  };
  candles: Candle[];
  markers: ChartMarker[];
  timeline: {
    id: number;
    time: string;
    recommendation: string;
    risk_approved: boolean;
    executed: boolean;
    rationale: string;
    data_source: string;
  }[];
  equity_curve: { time: string; equity: number }[];
  drawdown_pct: number;
  api_cost_by_agent: { role: string; estimated_cost_usd: number; calls: number }[];
  last_price: number | null;
};

export type AgentAssignment = {
  role: string;
  display_name?: string;
  provider: string;
  model: string;
  enabled: boolean;
  temperature: number;
};

export type AppSettings = {
  environment: string;
  live_authorized: boolean;
  kill_switch: boolean;
  max_risk_per_trade_pct: number;
  max_daily_loss_pct: number;
  max_concurrent_positions: number;
  max_spread_pips: number;
  max_slippage_pips: number;
  providers: { name: string; configured: boolean; models: string[]; status: string }[];
  agents: AgentAssignment[];
};

export type AppSettingsUpdate = { trading_environment: string; kill_switch: boolean };
export type AgentUpdate = { provider: string; model: string; enabled: boolean; temperature: number };

export type CollaborationResult = {
  recommendation: string;
  risk_approved: boolean;
  risk: { reasons: string[]; lots: number };
  executed: boolean;
  label: string;
  reports: { role: string; status: string; summary: string; error: string }[];
};

export type AuditItem = {
  id: number;
  category: string;
  level: string;
  message: string;
  details: string;
  created_at: string;
};

export type BacktestBody = {
  symbol: string;
  timeframe: string;
  start: string;
  end: string;
  walk_forward: boolean;
  engine: string;
};

export type BacktestResult = {
  id?: number;
  engine: string;
  trades?: number;
  win_rate?: number;
  profit_factor?: number;
  net_pnl?: number;
  is_synthetic?: boolean;
  notes?: string;
  disclaimer?: string;
};

export type BacktestListItem = BacktestResult & { name?: string; created_at?: string };

export type EvalBody = {
  symbol: string;
  timeframe: string;
  start: string;
  end: string;
  providers: string[];
  max_bars: number;
};

export type EvalResult = { id?: number; results: Record<string, unknown>; notes: string };
