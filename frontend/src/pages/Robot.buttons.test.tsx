/**
 * Teste mínimo dos botões INICIAR / PARAR / Preparar / Confirmar / Desativar.
 * Sem chamadas pagas — API mockada.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RobotOpenAIAutonomous, RobotStatus } from "../api/client";

const calls: string[] = [];

function baseAutonomous(over: Partial<RobotOpenAIAutonomous> = {}): RobotOpenAIAutonomous {
  return {
    model: "gpt-4o-mini",
    interval_minutes: 15,
    openai_key_configured: true,
    ai_enabled: true,
    armed: false,
    confirmed: false,
    active: false,
    scheduling_active: false,
    execution_enabled: false,
    ready_for_activation: true,
    paid_calls_blocked_until_confirm: true,
    authorization_label: "A aguardar autorização",
    note: "A aguardar autorização — nenhuma chamada paga até confirmar.",
    daily_budget_usd: 0.1,
    spent_today_usd: 0,
    remaining_today_usd: 0.1,
    blockers: [],
    ...over,
  };
}

let status: RobotStatus = {
  title: "Robô",
  disclaimer: "Simulação",
  running: false,
  thread_alive: false,
  robot_state: { code: "stopped", label: "Parado" },
  mt5_state: { code: "disconnected", label: "Desligado" },
  openai_state: { code: "disabled", label: "Desativada" },
  execution_state: { code: "blocked", label: "Bloqueada" },
  why_not_trading: { code: "robot_stopped", message: "O robô está parado." },
  account_mt5: { available: false },
  decision: "HOLD",
  decision_pt: "AGUARDAR",
  block_reason: "",
  last_message: "",
  ticks_completed: 0,
  capital: {
    balance: 10000,
    equity: 10000,
    currency: "USD",
    unrealized_pnl: 0,
    realized_pnl: 0,
    starting_equity: 10000,
    is_simulated: true,
  },
  security: {
    live_trading_locked: true,
    ai_enabled: true,
    ai_calls: 0,
    paid_ai_calls: 0,
    environment: "PAPER",
    auto_paid_ai: false,
  },
  quote: {
    source: "mt5",
    mid: 1.1,
    bid: 1.0999,
    ask: 1.1001,
    age_seconds: 0.5,
    status: "atualizado",
    feed_state: "recente",
  },
  openai_autonomous: baseAutonomous(),
  open_positions: [],
  trades: [],
  strategy: { key: "ai_radar", approved_count: 0, approved: [] },
};

vi.mock("../components/CandleChart", () => ({
  CandleChart: () => <div data-testid="chart-stub" />,
}));

vi.mock("../api/client", () => ({
  api: {
    robotStatus: vi.fn(async () => status),
    candles: vi.fn(async () => ({ candles: [], source: "mt5", label: "MT5", is_synthetic: false })),
    robotStart: vi.fn(async () => {
      calls.push("POST /robot/start");
      status = {
        ...status,
        running: true,
        thread_alive: true,
        robot_state: { code: "monitoring", label: "A monitorizar" },
      };
      return status;
    }),
    robotStop: vi.fn(async () => {
      calls.push("POST /robot/stop");
      status = {
        ...status,
        running: false,
        thread_alive: false,
        robot_state: { code: "stopped", label: "Parado" },
      };
      return status;
    }),
    robotAiAutonomousArm: vi.fn(async () => {
      calls.push("POST /robot/ai/autonomous/arm");
      status = {
        ...status,
        openai_autonomous: baseAutonomous({
          armed: true,
          confirmed: false,
          ready_for_activation: true,
        }),
      };
      return status.openai_autonomous;
    }),
    robotAiAutonomousActivate: vi.fn(async () => {
      calls.push("POST /robot/ai/autonomous/activate");
      status = {
        ...status,
        openai_autonomous: baseAutonomous({
          armed: true,
          confirmed: true,
          paid_calls_blocked_until_confirm: false,
          authorization_label: "Autorizado",
          execution_enabled: false,
        }),
      };
      return status.openai_autonomous;
    }),
    robotAiAutonomousDeactivate: vi.fn(async () => {
      calls.push("POST /robot/ai/autonomous/deactivate");
      status = {
        ...status,
        openai_autonomous: baseAutonomous({ armed: false, confirmed: false }),
      };
      return status.openai_autonomous;
    }),
    robotDemoActivate: vi.fn(async () => ({ active: true })),
    robotDemoPause: vi.fn(async () => ({ pause_entries: true })),
    robotDemoEmergency: vi.fn(async () => ({ emergency: true })),
    robotDemoClosePositions: vi.fn(async () => ({ ok: true })),
  },
}));

import { Robot } from "./Robot";

describe("Robot buttons", () => {
  beforeEach(() => {
    cleanup();
    calls.length = 0;
    status = {
      ...status,
      running: false,
      thread_alive: false,
      robot_state: { code: "stopped", label: "Parado" },
      openai_autonomous: baseAutonomous(),
    };
    vi.stubGlobal("confirm", vi.fn(() => true));
  });

  afterEach(() => {
    cleanup();
  });

  it("INICIAR e PARAR disparam HTTP e atualizam estado visual", async () => {
    const user = userEvent.setup();
    render(<Robot />);
    const startBtn = await screen.findByRole("button", { name: "INICIAR" });
    expect(startBtn).toBeEnabled();
    await user.click(startBtn);
    await waitFor(() => expect(calls).toContain("POST /robot/start"));
    expect(await screen.findByRole("button", { name: "PARAR" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "PARAR" }));
    await waitFor(() => expect(calls).toContain("POST /robot/stop"));
    expect(await screen.findByRole("button", { name: "INICIAR" })).toBeInTheDocument();
  });

  it("Preparar, Confirmar (com diálogo) e Desativar — sem execução", async () => {
    const user = userEvent.setup();
    const { container } = render(<Robot />);
    const preparar = await screen.findByRole("button", { name: "Preparar" });
    await user.click(preparar);
    await waitFor(() => expect(calls).toContain("POST /robot/ai/autonomous/arm"));

    const confirmBtn = await screen.findByRole("button", {
      name: /Confirmar ativação/,
    });
    expect(confirmBtn).toBeEnabled();
    await user.click(confirmBtn);
    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => expect(calls).toContain("POST /robot/ai/autonomous/activate"));
    expect(container.textContent).toMatch(/Já confirmado|confirmada/i);

    await user.click(screen.getByRole("button", { name: "Desativar" }));
    await waitFor(() => expect(calls).toContain("POST /robot/ai/autonomous/deactivate"));
  });

  it("Cancelar confirmação não chama activate", async () => {
    vi.stubGlobal("confirm", vi.fn(() => false));
    const user = userEvent.setup();
    status = {
      ...status,
      openai_autonomous: baseAutonomous({ armed: true, ready_for_activation: true }),
    };
    render(<Robot />);
    const confirmBtn = await screen.findByRole("button", { name: /Confirmar ativação/ });
    await user.click(confirmBtn);
    expect(window.confirm).toHaveBeenCalled();
    expect(calls).not.toContain("POST /robot/ai/autonomous/activate");
  });
});
