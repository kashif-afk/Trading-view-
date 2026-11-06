"""TradingView webhook simulator (`python tools/alert_sim.py`).

Generates payloads matching `tradingview/strategy.pine` so you can test the full webhook -> risk gate ->
sizer -> broker -> ledger -> dashboard flow without a paid TradingView webhook plan.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import get_settings


def build_payload(
    *,
    secret: str,
    action: str = "buy",
    symbol: str = "XAUUSD",
    price: float = 4060.0,
    stop: float | None = 4048.0,
    target: float | None = 4084.0,
    signal_id: str | None = None,
    strategy: str = "XAU_EMA_RSI_v0.1",
    timeframe: str = "15m",
    reason: str = "entry",
    ts_iso: str | None = None,
    htf_bias: int = 1,
    rsi: float = 54.2,
    atr: float = 8.0,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    iso = ts_iso or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    sig_id = signal_id or f"{symbol}-{int(now.timestamp() * 1000)}-{action}"
    return {
        "secret": secret,
        "signal_id": sig_id,
        "strategy": strategy,
        "symbol": symbol,
        "exchange": "OANDA",
        "action": action,
        "price": price,
        "stop": None if action.startswith("close") else stop,
        "target": None if action.startswith("close") else target,
        "timeframe": timeframe,
        "time": iso,
        "reason": reason,
        "indicators": {
            "ema_fast": round(price - 1.5, 2),
            "ema_slow": round(price - 4.0, 2),
            "rsi": rsi,
            "atr": atr,
            "htf_bias": htf_bias,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Send a simulated TradingView alert to /webhook")
    ap.add_argument("--url", default="http://127.0.0.1:8000/webhook")
    ap.add_argument("--action", default="buy", choices=["buy", "sell", "close_long", "close_short"])
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--price", type=float, default=4060.0)
    ap.add_argument("--stop", type=float, default=4048.0)
    ap.add_argument("--target", type=float, default=4084.0)
    ap.add_argument("--signal-id", default=None)
    ap.add_argument("--secret", default=None)
    args = ap.parse_args()

    s = get_settings()
    sec = args.secret or s.webhook_secret.get_secret_value() or "dev-webhook-secret"
    body = build_payload(
        secret=sec,
        action=args.action,
        symbol=args.symbol,
        price=args.price,
        stop=args.stop,
        target=args.target,
        signal_id=args.signal_id,
    )
    with httpx.Client(timeout=10.0) as client:
        r = client.post(args.url, json=body)
        print(f"HTTP {r.status_code}: {r.text}")


if __name__ == "__main__":
    main()
