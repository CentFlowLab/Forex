import { api, type AnalysisStartBody } from "../api/client";

/** Fluxo seguro: estimativa → confirmação → autorização única → análise. */
export async function startAuthorizedAnalysis(
  body: Omit<AnalysisStartBody, "authorization_id">,
  labels: {
    confirmTemplate: (maxCalls: number, cost: string, disclaimer: string) => string;
    aiDisabled: string;
  },
): Promise<unknown> {
  const status = await api.aiStatus();
  if (!status.ai_enabled) {
    throw new Error(labels.aiDisabled);
  }
  const estimate = await api.aiEstimate(body.debate_rounds);
  const cost = estimate.estimated_max_cost_usd.toFixed(4);
  const ok = window.confirm(
    labels.confirmTemplate(estimate.max_calls, cost, estimate.disclaimer),
  );
  if (!ok) {
    throw new Error("Análise cancelada pelo utilizador.");
  }
  const auth = await api.aiAuthorize({
    confirmed: true,
    debate_rounds: body.debate_rounds,
    workflow: "tradingagents",
  });
  return api.analysisStart({
    ...body,
    authorization_id: auth.authorization_id,
  });
}
