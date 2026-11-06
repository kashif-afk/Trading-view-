"""Postmortem classifier and lesson generator (Phase 4.5).

Mirrors the taxonomy and outcome classification from `signal-postmortem` and `trader-memory-core`.
Supported mistake taxonomy tags:
  - `against_regime`
  - `late_entry`
  - `stop_too_tight`
  - `stop_too_wide`
  - `news_event`
  - `slippage`
  - `rule_violation`
  - `overtrading`
  - `good_trade_bad_outcome`
  - `clean_win`
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

MISTAKE_TAXONOMY = (
    "against_regime",
    "late_entry",
    "stop_too_tight",
    "stop_too_wide",
    "news_event",
    "slippage",
    "rule_violation",
    "overtrading",
    "good_trade_bad_outcome",
    "clean_win",
)


def classify_trade(
    *,
    direction: str,
    r_multiple: Decimal,
    exit_reason: str,
    holding_seconds: int,
    slippage_usd: Decimal,
    initial_risk_usd: Decimal,
    mae_usd: Decimal,
    mfe_usd: Decimal,
    indicators: dict[str, Any],
    nearby_events: list[dict[str, Any]],
    entries_today: int = 1,
    rule_violated: bool = False,
) -> tuple[list[str], str]:
    """Returns (tags, lesson_text) using deterministic domain heuristics."""
    tags: list[str] = []
    r = float(r_multiple)
    risk = float(initial_risk_usd) if initial_risk_usd > 0 else 10.0

    htf_bias = int(indicators.get("htf_bias", 0))
    rsi = float(indicators.get("rsi", 50.0))

    if rule_violated:
        tags.append("rule_violation")
    if (direction == "long" and htf_bias == -1) or (direction == "short" and htf_bias == 1):
        tags.append("against_regime")
    if (direction == "long" and rsi >= 65.0) or (direction == "short" and rsi <= 35.0):
        tags.append("late_entry")
    if nearby_events:
        tags.append("news_event")
    if float(slippage_usd) > 0.25 * risk:
        tags.append("slippage")
    if entries_today > 4 and r < 0:
        tags.append("overtrading")

    if r < 0:
        if holding_seconds < 900 and float(mfe_usd) > 0.5 * risk:
            tags.append("stop_too_tight")
        elif exit_reason == "time" and abs(r) < 0.4 and float(mae_usd) < 0.3 * risk:
            tags.append("stop_too_wide")
        if not tags:
            tags.append("good_trade_bad_outcome")
    else:
        if r >= 0.8 and not tags:
            tags.append("clean_win")
        elif not tags:
            tags.append("clean_win")

    outcome = "WIN" if r > 0.05 else ("LOSS" if r < -0.05 else "SCRATCH")
    lesson = (
        f"{outcome} ({r:+.2f}R) on {direction.upper()} exit={exit_reason}: "
        f"tags={','.join(tags)}. "
        f"Entry RSI={rsi:.1f}, HTF bias={htf_bias}. "
    )
    if "against_regime" in tags:
        lesson += "Avoid counter-HTF entries unless F2 filter confirms alignment."
    elif "late_entry" in tags:
        lesson += "Entry chased extended RSI; tighten RSI overextension threshold."
    elif "stop_too_tight" in tags:
        lesson += "Stopped out quickly despite favorable excursion; consider wider ATR multiple."
    elif "news_event" in tags:
        lesson += "Trade overlapped macro event volatility; enforce F3 news blackout window."
    elif "clean_win" in tags:
        lesson += "Setup followed trend + RSI resumption cleanly within session rules."
    else:
        lesson += "Standard execution within rules; variance outcome."

    return tags, lesson
