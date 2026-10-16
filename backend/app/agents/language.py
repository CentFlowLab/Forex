"""Deteção heurística e reforço de português de Portugal nas respostas dos agentes."""

from __future__ import annotations

import re
from typing import Any

# Marcadores de prosa inglesa (evitar termos técnicos partilhados com o Forex).
_ENGLISH_MARKERS = re.compile(
    r"\b("
    r"the|and|with|from|that|this|based|suggests|indicates|indicating|however|although|"
    r"outlook|likely|appears|currently|therefore|because|stronger|weaker|"
    r"overbought|oversold|recommendation|uncertainty|rationale|evidence|"
    r"insufficient|information|invalidation|challenger|coordinator|"
    r"price\s+action|moving\s+average|higher\s+highs|lower\s+lows|"
    r"there\s+is|there\s+are|we\s+see|looking\s+at|in\s+conclusion|"
    r"timeframe|conditions|momentum|across|multiple|supports|strongly|"
    r"show|shows|showed|very\s+low|no\s+fundamental|are\s+available|"
    r"on\s+the|in\s+the|for\s+the|to\s+the|of\s+the"
    r")\b",
    re.IGNORECASE,
)

# Marcadores típicos de português europeu / comum.
_PORTUGUESE_MARKERS = re.compile(
    r"\b("
    r"não|com|para|uma|dos|das|pelo|pela|este|esta|isso|assim|porém|"
    r"todavia|portanto|indicador|tendência|suporte|resistência|volatilidade|"
    r"recomendação|confiança|incerteza|fundamento|evidência|risco|"
    r"insuficiente|informação|entrada|invalidação|aguardar|comprar|vender|"
    r"mercado|preço|média|cruzamento|alinhamento|estrutura"
    r")\b",
    re.IGNORECASE,
)

# Expressões típicas do português do Brasil a evitar (aviso, não bloqueio duro).
_BR_MARKERS = re.compile(
    r"\b(você|vocês|celular|a gente|tá|né|pra|pro|cadê|legal|massa)\b",
    re.IGNORECASE,
)

PT_PT_USER_WRAPPER = (
    "INSTRUÇÃO OBRIGATÓRIA DE IDIOMA: Todos os campos de texto da resposta JSON "
    "(rationale, argument, evidence.claim, risks, invalidation, veto_reason, "
    "dissenting_views, contradictions, execution_risks, key_evidence, disclaimer) "
    "DEVEM estar em português de Portugal (pt-PT). "
    "Não uses inglês narrativo. Não uses português do Brasil. "
    "Mantém apenas os enums técnicos (BUY, SELL, HOLD, BULLISH, BEARISH, NEUTRAL, etc.) em inglês.\n\n"
    "DADOS DE ANÁLISE (JSON):\n"
)

PT_PT_RETRY_SYSTEM = (
    "\n\nCORREÇÃO OBRIGATÓRIA: A resposta anterior estava em inglês ou misturada. "
    "Reescreve TODOS os campos de texto exclusivamente em português de Portugal (pt-PT). "
    "Não traduzas os enums técnicos BUY/SELL/HOLD/BULLISH/BEARISH/NEUTRAL."
)


def collect_text_fields(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "rationale",
        "argument",
        "invalidation",
        "veto_reason",
        "disclaimer",
        "debate_summary",
    ):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val)
    for key in (
        "risks",
        "contradictions",
        "overconfidence_flags",
        "execution_risks",
        "dissenting_views",
        "counterpoints_addressed",
        "key_evidence",
        "constraints",
    ):
        for item in payload.get(key) or []:
            if isinstance(item, str) and item.strip():
                parts.append(item)
    for ev in payload.get("evidence") or []:
        if isinstance(ev, dict) and isinstance(ev.get("claim"), str):
            parts.append(ev["claim"])
        elif isinstance(ev, str):
            parts.append(ev)
    return "\n".join(parts)


def language_assessment(text: str) -> dict[str, Any]:
    """Avalia se o texto parece pt-PT (heurística, não tradução automática)."""
    cleaned = (text or "").strip()
    if len(cleaned) < 24:
        return {
            "ok": True,
            "likely_english": False,
            "likely_brazilian": False,
            "en_hits": 0,
            "pt_hits": 0,
            "warning": "",
        }
    # Remover enums técnicos conhecidos do score.
    scrubbed = re.sub(
        r"\b(BUY|SELL|HOLD|BULLISH|BEARISH|NEUTRAL|RSI|MACD|ATR|EMA|SMA|UTC|API)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    en = len(_ENGLISH_MARKERS.findall(scrubbed))
    pt = len(_PORTUGUESE_MARKERS.findall(scrubbed))
    br = len(_BR_MARKERS.findall(scrubbed))
    # Limiar mais sensível: prosa inglesa típica dos modelos deve ser detetada.
    likely_english = (en >= 3 and en > pt) or (en >= 5 and pt == 0)
    likely_br = br >= 2
    warning = ""
    if likely_english:
        warning = "Resposta parece estar em inglês apesar das instruções pt-PT."
    elif likely_br:
        warning = "Resposta contém expressões típicas do português do Brasil."
    return {
        "ok": not likely_english,
        "likely_english": likely_english,
        "likely_brazilian": likely_br,
        "en_hits": en,
        "pt_hits": pt,
        "warning": warning,
    }


def assess_payload_language(payload: dict[str, Any]) -> dict[str, Any]:
    return language_assessment(collect_text_fields(payload))
