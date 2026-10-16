# GROK FOREX AI

Terminal local de Forex com seis agentes de IA colaborativos, motor de risco determinístico e execução simulada.

O sistema **não coloca ordens reais** na configuração inicial. LIVE está desligado por defeito e exige autorização manual explícita.

## O que está incluído

- Dashboard React (tema escuro) com gráfico EUR/USD, painéis dos 6 agentes, equity, posições, métricas e kill switch
- Pipeline multiagente inspirado em [TradingAgents](https://github.com/TauricResearch/TradingAgents) (Apache-2.0): debate Analista Comprador vs Analista Vendedor, veto de risco e memória de decisões
- Backend FastAPI em `127.0.0.1`
- Broker **simulado** funcional (PAPER / BACKTEST)
- Adaptadores **IG** e **MetaTrader 5** apenas como scaffolding — não fingem endpoints reais
- Motor de risco separado da IA (a consenso de agentes nunca ignora os limites)
- Backtest reprodutível sem look-ahead, com baseline SMA e walk-forward
- Provedores de IA independentes: **xAI (Grok)**, **OpenAI (GPT)**, **Anthropic (Claude)**
- Comparação de modelos sobre as mesmas barras históricas
- Dataset **sintético** claramente etiquetado — nunca apresentado como preço live

## Requisitos (Windows 11)

- Python 3.10+ (3.12 recomendado)
- Node.js 18+
- Contas de API opcionais. Sem chaves, a aplicação corre em modo simulado e os agentes ficam `unavailable`

## Instalação rápida

```powershell
cd C:\Users\Emanuel\Documents\GrokForexAI
copy .env.example .env
.\start.bat
```

O script cria o venv, instala dependências e abre:

- API: http://127.0.0.1:8000
- Docs: http://127.0.0.1:8000/docs
- UI: http://127.0.0.1:5173

## Instalação manual

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cd ..
copy .env.example .env
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Noutro terminal:

```powershell
cd frontend
npm install
npm run dev
```

## Testes

```powershell
cd backend
.\.venv\Scripts\activate
pytest -q
```

## Ambientes

| Ambiente  | Comportamento |
|-----------|----------------|
| BACKTEST  | Simulação histórica. Sem ordens live. |
| PAPER     | Simulação sobre o broker virtual. Predefinição. |
| LIVE      | Bloqueado até `LIVE_TRADING_ENABLED=true` **e** `LIVE_MANUAL_AUTHORIZATION=I_AUTHORIZE_LIVE_TRADING` **e** um adaptador real implementado. |

Nesta build, mesmo com LIVE autorizado, a execução real IG/MT5 **rejeita** ordens porque os endpoints ainda não estão implementados.

## Seis agentes (+ debate)

1. Analista Técnico — RSI, MACD, médias, S/R, volatilidade
2. Analista de Tendências — M5/M15/H1/H4, estrutura, momentum
3. Analista Fundamental e Macroeconómico — calendário/notícias **apenas se existirem fontes**. Nunca inventa dados
4. Analista de Estratégias — entrada, SL obrigatório, TP, R:R
5. Analista de Risco — contradiz, deteta excesso de confiança, pode vetar
6. Coordenador Principal — BUY / SELL / HOLD

Papéis internos de debate (TradingAgents): **Analista Comprador** e **Analista Vendedor**.
Fluxo predefinido: relatórios → debate → veto de risco → decisão final → `RiskEngine` determinístico.
Documentação: `docs/TRADINGAGENTS_INTEGRATION.md` · Atribuição: `THIRD_PARTY_NOTICES.md`.

Cada agente tem fornecedor e modelo configuráveis no painel **Configuração**.
Análise manual: `POST /api/analysis/start` (também no painel do dashboard).

## Risco (predefinições conservadoras)

- 0,25% de risco simulado por trade
- 1% de perda diária máxima
- 1 posição concorrente
- Stop-loss obrigatório
- Spread e slippage máximos configuráveis
- Sem martingale e sem aumento de lote após perdas
- Sem trading com dados stale ou broker desligado
- Kill switch persistente
- `client_order_id` único (duplicados são ignorados)

A IA **não** acede a credenciais de broker e **não** envia ordens.

## Dados de mercado

- Instrumento inicial: EUR/USD
- Timeframes: M5, M15, H1, H4
- **Histórico real Dukascopy** (sem conta de broker, sem API key): descarregável no dashboard
- Cache local em SQLite + Parquet (`backend/data/historical/parquet/`)
- Importação CSV: `timestamp,open,high,low,close,volume`
- O dataset sintético é gerado na primeira arranque e marcado `SYNTHETIC — not live market prices`
- Dados Dukascopy são marcados `REAL HISTORICAL DATA` e **nunca** são substituídos silenciosamente por sintéticos

### Replay histórico

No dashboard: escolher intervalo → Descarregar histórico real → Criar replay → Play/Pause/Restart.
Os agentes só veem candles até ao cursor do replay (anti look-ahead). LIVE permanece permanentemente desligado nesta fase.

Não reivindique rentabilidade com dados sintéticos.

## Segurança

- Credenciais só no `.env` (nunca no código)
- `.env` está no `.gitignore`
- Chaves não são registadas em logs
- Backend preso a localhost por defeito
- LIVE_TRADING_ENABLED=false

## Arquitetura

```
backend/app/
  agents/      orquestração dos 6 agentes
  ai/          provedores xAI / OpenAI / Anthropic
  brokers/     simulated + scaffolds IG/MT5
  risk/        motor determinístico + specs de instrumentos
  market/      indicadores, sintético, importação
  backtest/    baseline + walk-forward
  evaluation/  comparação de modelos
  api/         FastAPI
frontend/src/  terminal React
```

## O que funciona agora

- Dashboard, gráfico, métricas, kill switch, seletor de ambiente
- Broker simulado, posições, fecho, stops
- Motor de risco e auditoria persistente
- Backtest baseline com custos e walk-forward
- Agentes com JSON validado **quando há chaves**
- Agentes desativados com erro explícito **quando não há chaves**

## O que permanece incompleto

- Handshake real IG REST e sessão MT5 (scaffolds apenas)
- Feed de notícias/calendário económico (o agente fundamental reporta informação insuficiente)
- Paper trading com cotações reais de um broker — o caminho está preparado, mas falta ligar um adaptador de dados live
- Backtest IA barra-a-barra completo (existe amostragem limitada por custo de API)

## Aviso

Isto é software de investigação e simulação. Forex envolve risco de perda de capital. Nada aqui é aconselhamento financeiro nem garantia de lucro.
