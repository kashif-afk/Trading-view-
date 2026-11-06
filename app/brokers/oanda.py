"""OANDA v20 REST adapter for `XAU_USD` (practice and live environments).

Enforces:
- Server-side `stopLossOnFill` and `takeProfitOnFill` attached to every entry order.
- Deterministic `clientExtensions.id` (`client_order_id`) so retries never double-order.
- Exponential backoff via `tenacity` on transient HTTP/network failures.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.brokers.base import (
    BrokerAdapter,
    BrokerBalance,
    BrokerError,
    BrokerOrderResult,
    BrokerPosition,
    NakedEntryError,
    Quote,
)
from app.core.config import Settings, get_settings


class OandaBroker(BrokerAdapter):
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        host = (
            "https://api-fxpractice.oanda.com"
            if self.s.oanda_env == "practice"
            else "https://api-fxtrade.oanda.com"
        )
        self.base_url = f"{host}/v3/accounts/{self.s.oanda_account_id}"
        self.headers = {
            "Authorization": f"Bearer {self.s.oanda_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        @retry(
            reraise=True,
            stop=stop_after_attempt(self.s.broker_max_retries),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type(BrokerError),
        )
        def _call() -> dict[str, Any]:
            try:
                with httpx.Client(timeout=self.s.broker_timeout_sec) as client:
                    resp = client.request(method, f"{self.base_url}{path}", headers=self.headers, json=json_body)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise BrokerError(f"OANDA transient HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    raise ValueError(f"OANDA HTTP {resp.status_code}: {resp.text[:200]}")
                return resp.json()
            except httpx.HTTPError as e:
                raise BrokerError(f"OANDA transport error: {e}") from e

        try:
            return _call()
        except ValueError as e:
            raise BrokerError(str(e)) from e

    def place_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: Decimal,
        price_hint: Decimal,
        stop: Decimal | None,
        target: Decimal | None,
    ) -> BrokerOrderResult:
        if side in ("buy", "sell") and stop is None:
            raise NakedEntryError("OANDA entry requires stopLossOnFill")

        if side in ("close_long", "close_short"):
            body = {"longUnits": "ALL"} if side == "close_long" else {"shortUnits": "ALL"}
            data = self._request("PUT", f"/positions/{symbol}/close", body)
            tx = data.get("longOrderFillTransaction") or data.get("shortOrderFillTransaction") or {}
            px = Decimal(str(tx.get("price", price_hint)))
            pnl = Decimal(str(tx.get("pl", "0")))
            fee = abs(Decimal(str(tx.get("commission", "0"))))
            return BrokerOrderResult(
                client_order_id=client_order_id,
                broker_order_id=str(tx.get("orderID", client_order_id)),
                symbol=symbol,
                side=side,
                qty=qty,
                fill_price=px,
                slippage=abs(px - price_hint) * qty,
                fee=fee,
                realized_pnl=pnl,
                status="filled",
                stop=None,
                target=None,
                filled_at=datetime.now(timezone.utc),
                broker_fill_id=str(tx.get("id", f"OANDA-{client_order_id}")),
            )

        units = str(qty if side == "buy" else -qty)
        order_spec: dict[str, Any] = {
            "type": "MARKET",
            "instrument": symbol,
            "units": units,
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "clientExtensions": {"id": client_order_id[:64]},
            "stopLossOnFill": {"price": f"{stop:.2f}"},
        }
        if target is not None:
            order_spec["takeProfitOnFill"] = {"price": f"{target:.2f}"}

        data = self._request("POST", "/orders", {"order": order_spec})
        fill = data.get("orderFillTransaction") or data.get("orderCreateTransaction") or {}
        px = Decimal(str(fill.get("price", price_hint)))
        fee = abs(Decimal(str(fill.get("commission", "0"))))
        return BrokerOrderResult(
            client_order_id=client_order_id,
            broker_order_id=str(fill.get("orderID", fill.get("id", client_order_id))),
            symbol=symbol,
            side=side,
            qty=qty,
            fill_price=px,
            slippage=abs(px - price_hint) * qty,
            fee=fee,
            realized_pnl=Decimal("0"),
            status="filled",
            stop=stop,
            target=target,
            filled_at=datetime.now(timezone.utc),
            broker_fill_id=str(fill.get("id", f"OANDA-FILL-{client_order_id}")),
        )

    def cancel_all_orders(self, symbol: str | None = None) -> int:
        data = self._request("GET", "/pendingOrders")
        cancelled = 0
        for o in data.get("orders", []):
            if symbol and o.get("instrument") != symbol:
                continue
            oid = o.get("id")
            if oid:
                self._request("PUT", f"/orders/{oid}/cancel")
                cancelled += 1
        return cancelled

    def flatten_all_positions(self, price_hints: dict[str, Decimal] | None = None) -> list[BrokerOrderResult]:
        out: list[BrokerOrderResult] = []
        for p in self.positions():
            side = "close_long" if p.direction == "long" else "close_short"
            cid = f"kill-{p.symbol}-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
            out.append(
                self.place_order(
                    client_order_id=cid,
                    symbol=p.symbol,
                    side=side,
                    qty=p.qty,
                    price_hint=p.avg_price,
                    stop=None,
                    target=None,
                )
            )
        return out

    def positions(self) -> list[BrokerPosition]:
        data = self._request("GET", "/openPositions")
        out: list[BrokerPosition] = []
        for p in data.get("positions", []):
            sym = p["instrument"]
            long_u = Decimal(str(p.get("long", {}).get("units", "0")))
            short_u = Decimal(str(p.get("short", {}).get("units", "0")))
            if long_u > 0:
                leg = p["long"]
                out.append(
                    BrokerPosition(
                        symbol=sym,
                        direction="long",
                        qty=long_u,
                        avg_price=Decimal(str(leg.get("averagePrice", "0"))),
                        stop=None,
                        target=None,
                        unrealized_pnl=Decimal(str(leg.get("unrealizedPL", "0"))),
                    )
                )
            elif short_u < 0:
                leg = p["short"]
                out.append(
                    BrokerPosition(
                        symbol=sym,
                        direction="short",
                        qty=abs(short_u),
                        avg_price=Decimal(str(leg.get("averagePrice", "0"))),
                        stop=None,
                        target=None,
                        unrealized_pnl=Decimal(str(leg.get("unrealizedPL", "0"))),
                    )
                )
        return out

    def balances(self) -> BrokerBalance:
        data = self._request("GET", "/summary")
        acc = data.get("account", {})
        return BrokerBalance(
            currency=acc.get("currency", self.s.base_currency),
            cash_balance=Decimal(str(acc.get("balance", "0"))),
            equity=Decimal(str(acc.get("NAV", acc.get("balance", "0")))),
            unrealized_pnl=Decimal(str(acc.get("unrealizedPL", "0"))),
        )

    def quote(self, symbol: str, fallback_price: Decimal | None = None) -> Quote:
        try:
            data = self._request("GET", f"/pricing?instruments={symbol}")
            prices = data.get("prices", [])
            if prices:
                p = prices[0]
                bid = Decimal(str(p["bids"][0]["price"]))
                ask = Decimal(str(p["asks"][0]["price"]))
                mid = ((bid + ask) / Decimal("2")).quantize(Decimal("0.0001"))
                return Quote(symbol=symbol, price=mid, ts=datetime.now(timezone.utc), source="oanda", stale=False)
        except Exception:
            pass
        return Quote(
            symbol=symbol,
            price=fallback_price or Decimal("4060.00"),
            ts=datetime.now(timezone.utc),
            source="fallback",
            stale=True,
        )


_BROKER_SINGLETON: dict[str, BrokerAdapter] = {}


def get_broker(settings: Settings | None = None) -> BrokerAdapter:
    from app.brokers.paper import PaperBroker

    s = settings or get_settings()
    key = f"{s.broker}:{s.paper_state_path}:{s.oanda_account_id}"
    if key not in _BROKER_SINGLETON:
        _BROKER_SINGLETON[key] = OandaBroker(s) if s.broker == "oanda" else PaperBroker(s)
    return _BROKER_SINGLETON[key]
