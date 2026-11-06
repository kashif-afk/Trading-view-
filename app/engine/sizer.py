"""Position sizer (Phase 3).
Mirrors `position-sizer` and `futures-position-sizer`:
    qty = floor_to_step((equity * risk_pct / 100) / |entry - stop|, qty_step)
Never rounds up. Rejects any trade where stepped qty < min_qty or notional < min_notional.
Caps size if notional would breach the exposure ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass
class SizeResult:
    allowed: bool
    qty: Decimal
    risk_usd: Decimal
    stop_distance: Decimal
    notional_usd: Decimal
    reason: str


def calculate_position_size(
    *,
    equity: Decimal,
    risk_pct: Decimal,
    entry_price: Decimal,
    stop_price: Decimal | None,
    min_qty: Decimal = Decimal("0.01"),
    qty_step: Decimal = Decimal("0.01"),
    min_notional: Decimal = Decimal("1.0"),
    max_additional_notional: Decimal | None = None,
    size_multiplier: Decimal = Decimal("1.0"),
) -> SizeResult:
    if stop_price is None:
        return SizeResult(False, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), "rejected: missing stop-loss (no naked entries)")
    stop_dist = abs(Decimal(str(entry_price)) - Decimal(str(stop_price)))
    if stop_dist <= Decimal("0"):
        return SizeResult(False, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), "rejected: stop distance must be > 0")

    risk_budget = (Decimal(str(equity)) * (Decimal(str(risk_pct)) / Decimal("100")) * Decimal(str(size_multiplier)))
    raw_qty = risk_budget / stop_dist

    if max_additional_notional is not None and entry_price > 0:
        max_qty_by_exp = max(Decimal("0"), Decimal(str(max_additional_notional)) / Decimal(str(entry_price)))
        raw_qty = min(raw_qty, max_qty_by_exp)

    step = Decimal(str(qty_step))
    stepped_qty = (raw_qty / step).to_integral_value(rounding=ROUND_DOWN) * step
    stepped_qty = stepped_qty.quantize(step)

    if stepped_qty < Decimal(str(min_qty)):
        return SizeResult(
            False,
            Decimal("0"),
            Decimal("0"),
            stop_dist,
            Decimal("0"),
            f"rejected: sized qty {stepped_qty} < min_qty {min_qty} for risk budget ${risk_budget:.2f}",
        )

    notional = (stepped_qty * Decimal(str(entry_price))).quantize(Decimal("0.01"))
    if notional < Decimal(str(min_notional)):
        return SizeResult(
            False,
            Decimal("0"),
            Decimal("0"),
            stop_dist,
            notional,
            f"rejected: notional ${notional} < min_notional ${min_notional}",
        )

    realized_risk = (stepped_qty * stop_dist).quantize(Decimal("0.01"))
    return SizeResult(True, stepped_qty, realized_risk, stop_dist, notional, "ok")
