"""Mensagens de utilizador em português de Portugal (backend)."""

from __future__ import annotations

MSG = {
    "kill_active": "A paragem de emergência está ativa — novas ordens desativadas.",
    "kill_active_short": "Paragem de emergência ativa.",
    "live_disabled": "A negociação real está desativada.",
    "live_permanently_disabled": (
        "A negociação real está permanentemente desativada nesta fase de desenvolvimento. "
        "Utilize PAPER/BACKTEST apenas com execução simulada."
    ),
    "live_phase_disclaimer": (
        "Os resultados PAPER/BACKTEST são simulados. "
        "DADOS HISTÓRICOS REAIS e DADOS SINTÉTICOS estão etiquetados separadamente. "
        "A negociação real está permanentemente desativada nesta fase de desenvolvimento."
    ),
    "agent_disabled": "Agente desativado nas definições.",
    "provider_not_configured": "O fornecedor {provider} não está configurado. Agente desativado.",
    "no_dukascopy_candles": (
        "Não existem velas históricas Dukascopy em cache. Descarregue primeiro um intervalo de datas."
    ),
    "download_not_found": "Tarefa de descarregamento não encontrada.",
    "replay_refuse_synthetic": (
        "A reprodução recusa dados sintéticos. Descarregue primeiro DADOS HISTÓRICOS REAIS."
    ),
    "replay_insufficient": (
        "Dados históricos reais insuficientes para reprodução "
        "({bars} velas {timeframe} de {source}). Descarregue dados Dukascopy primeiro."
    ),
    "replay_not_found": "Sessão de reprodução não encontrada.",
    "replay_no_synthetic_play": "Não é possível reproduzir sessões sintéticas.",
    "sim_broker_online": "Corretora simulada ligada",
    "sim_broker_offline": "Corretora simulada desligada",
    "sim_fill": "Execução simulada",
    "download_failed": "Descarregamento falhou — dados sintéticos NÃO foram substituídos.",
    "download_ok": (
        "Descarregados DADOS HISTÓRICOS REAIS: "
        "M5={m5} M15={m15} H1={h1} H4={h4} ticks={ticks}"
    ),
    "download_connecting": "A ligar ao datafeed público Dukascopy…",
    "synthetic_backtest_note": (
        "DADOS SINTÉTICOS — não reivindique rentabilidade de mercado real."
    ),
    "report_disclaimer": (
        "Todas as operações são simuladas. A negociação real está permanentemente "
        "desativada nesta fase."
    ),
    "only_simulated_close": "O fecho manual só está disponível na corretora simulada.",
    "position_not_found": "Posição não encontrada.",
    "invalid_environment": "Ambiente inválido.",
    "unknown_agent": "Agente desconhecido.",
    "unknown_provider": "Fornecedor desconhecido.",
    "unsupported_timeframe": "Intervalo temporal não suportado.",
    "account_missing": "Conta não inicializada.",
    "analysis_running": "Já existe uma análise em curso.",
    "analysis_not_found": "Análise não encontrada.",
    "stale_data_block": "Dados de mercado obsoletos — novas entradas simuladas bloqueadas.",
    "missing_macro": "Dados macroeconómicos indisponíveis (UNKNOWN).",
}
