/**
 * Fase 1 — painel: polling, ligação perdida, respostas fora de ordem, WAIT, capital.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RobotStatus } from "../api/client";

let status: RobotStatus;
let failNext = 0;
const resolveQueue: Array<() => void> = [];

function baseStatus(over: Partial<RobotStatus> = {}): RobotStatus {
  return {
    title: "Robô",
    disclaimer: "Simulação",
    running: true,
    thread_alive: true,
    panel_generated_at: "2026-10-15T12:00:00+00:00",
    robot_state: { code: "monitoring", label: "A monitorizar" },
    mt5_state: { code: "demo_connected", label: "Ligado a DEMO" },
    openai_state: { code: "decision_received", label: "Decisão recebida" },
    execution_state: { code: "blocked", label: "Bloqueada" },
    why_not_trading: {
      code: "demo_execution_disabled",
      message: "Execução DEMO desativada — o robô observa mas não envia ordens.",
    },
    account_mt5: {
      available: true,
      balance: 12345,
      equity: 12000,
      currency: "USD",
      login_masked: "50…798",
      server: "MetaQuotes-Demo",
      trade_mode_label: "DEMO",
      margin: 100,
      margin_free: 11900,
      profit: -345,
    },
    account_simulated: { balance: 10000, equity: 10000, is_simulated: true },
    decision: "HOLD",
    decision_pt: "AGUARDAR",
    block_reason: "",
    last_message: "",
    ticks_completed: 0,
    quote: { source: "mt5", mid: 1.1, feed_state: "recente" },
    capital: {
      balance: 10000,
      equity: 10000,
      currency: "USD",
      unrealized_pnl: 0,
      realized_pnl: 0,
      starting_equity: 10000,
      is_simulated: true,
    },
    open_positions: [],
    trades: [],
    strategy: { key: "ai_radar", approved_count: 0, approved: [] },
    security: {
      live_trading_locked: true,
      ai_enabled: true,
      ai_calls: 0,
      environment: "PAPER",
    },
    openai_real: {
      available: true,
      latest: {
        available: true,
        decision: "HOLD",
        decision_pt: "AGUARDAR",
        symbol: "EURUSD",
        justification: "Sem assimetria",
        confidence: "50%",
        analyzed_at: "2026-10-15T11:00:00+00:00",
        estimated_cost_usd: 0.001,
      },
      recent: [],
    },
    openai_autonomous: {
      confirmed: false,
      armed: false,
      ready_for_activation: true,
      spent_today_usd: 0,
      remaining_today_usd: 0.1,
      daily_budget_usd: 0.1,
      interval_minutes: 15,
    },
    ...over,
  };
}

vi.mock("../components/CandleChart", () => ({
  CandleChart: () => <div data-testid="chart-stub" />,
}));

vi.mock("../api/client", () => ({
  api: {
    robotStatus: vi.fn(async () => {
      if (failNext > 0) {
        failNext -= 1;
        throw new Error("network down");
      }
      if (resolveQueue.length) {
        await new Promise<void>((r) => resolveQueue.push(r));
      }
      return status;
    }),
    candles: vi.fn(async () => ({ candles: [], source: "mt5", label: "MT5" })),
    robotStart: vi.fn(async () => status),
    robotStop: vi.fn(async () => status),
    robotAiAutonomousArm: vi.fn(async () => status.openai_autonomous),
    robotAiAutonomousActivate: vi.fn(async () => status.openai_autonomous),
    robotAiAutonomousDeactivate: vi.fn(async () => status.openai_autonomous),
    robotDemoActivate: vi.fn(async () => ({ active: true })),
    robotDemoPause: vi.fn(async () => ({ pause_entries: true })),
    robotDemoEmergency: vi.fn(async () => ({ emergency: true })),
    robotDemoClosePositions: vi.fn(async () => ({ ok: true })),
  },
}));

import { Robot } from "./Robot";

describe("Robot panel phase1", () => {
  beforeEach(() => {
    cleanup();
    failNext = 0;
    resolveQueue.length = 0;
    status = baseStatus();
    vi.stubGlobal("confirm", vi.fn(() => true));
  });
  afterEach(() => cleanup());

  it("mostra estados, WAIT e capital DEMO separado", async () => {
    render(<Robot />);
    expect(await screen.findByText("A monitorizar")).toBeInTheDocument();
    expect(screen.getByText("Ligado a DEMO")).toBeInTheDocument();
    expect(screen.getByText(/Porque não está a negociar/i)).toBeInTheDocument();
    expect(screen.getByText(/Execução DEMO desativada/i)).toBeInTheDocument();
    expect(screen.getAllByText(/AGUARDAR/i).length).toBeGreaterThan(0);
    // Saldo DEMO (não 10000 do simulated) — formatMoney pode usar espaços
    expect(document.body.textContent || "").toMatch(/12[\s.,]?345/);
    expect(screen.getByText("Bloqueada")).toBeInTheDocument();
  });

  it("mostra ligação perdida após falhas de poll", async () => {
    failNext = 3;
    render(<Robot />);
    await waitFor(
      () => {
        expect(screen.getByText(/Ligação perdida/i)).toBeInTheDocument();
      },
      { timeout: 5000 },
    );
  });
});
