from __future__ import annotations

from app.agents.language import language_assessment


def test_detects_screenshot_style_english() -> None:
    text = (
        "On the M15 timeframe, the RSI is very low at 25.46 indicating oversold conditions "
        "and the MACD shows bearish momentum across multiple supports."
    )
    assessment = language_assessment(text)
    assert assessment["likely_english"] is True


def test_portuguese_analysis_ok() -> None:
    text = (
        "No intervalo M15, o RSI está muito baixo (25,46), o que indica condições de sobrevenda. "
        "O MACD confirma momentum descendente; a recomendação é aguardar confirmação da estrutura."
    )
    assessment = language_assessment(text)
    assert assessment["likely_english"] is False
