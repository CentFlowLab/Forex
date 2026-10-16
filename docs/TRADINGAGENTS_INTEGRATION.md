# Plano de integração TradingAgents → GROK FOREX AI

## Referência upstream

| Campo | Valor |
|-------|--------|
| Repositório | https://github.com/TauricResearch/TradingAgents |
| Commit HEAD usado | `1394a3f72aa4393e1a98f51b382434c4b4c2d972` |
| Licença | Apache License 2.0 |
| Cópia local de referência | `vendor/TradingAgents/` (não é dependência de runtime) |

## Princípios

1. Manter o dashboard, os seis agentes e a corretora simulada existentes.
2. Reutilizar a arquitetura conceptual (analistas → debate Bull/Bear → risco → decisão), não o código de ações/Yahoo/LangGraph.
3. Dados Forex via Dukascopy/cache local existente; nunca fabricar velas.
4. LLMs recomendam; o `RiskEngine` determinístico valida; LIVE permanece bloqueado.
5. Interface e prompts em português de Portugal (pt-PT).

## Componentes adaptados (não copiados à letra)

- Papéis Bull/Bear Researcher e debate estruturado por rondas
- Memória persistente de decisões com outcomes separados dos factos
- Orquestração em fases com evidência rastreável
- Limites de orçamento/custo e retries

## Componentes não reutilizados

- LangGraph / LangChain stack
- Yahoo Finance, SEC EDGAR, StockTwits (mercado acionista)
- Portfolio Manager de ações e CLI tipográfica do upstream

## Fluxo implementado

```
Snapshot Forex verificado
  → Analista Técnico / Tendências / Fundamental / Estratégias
  → Analista Comprador (Bull) + Analista Vendedor (Bear)
  → Até N rondas de debate (configurável, predefinido 2)
  → Analista de Risco (veto possível)
  → Coordenador Principal (BUY|SELL|HOLD)
  → RiskEngine determinístico
  → Memória de decisão (sem look-ahead)
```

## Endpoints novos

- `POST /api/analysis/start` — análise manual com debate
- `POST /api/analysis/{id}/stop` — pedido de cancelamento
- `GET /api/analysis/{id}` — estado e resultado
- `GET /api/analysis` — histórico
- `GET /api/analysis/memory` — memória de decisões
- `POST /api/backtest/compare-workflows` — baseline vs clássico vs debate
