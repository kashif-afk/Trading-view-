"""PaperBroker: deterministic, persistent (SQLite-backed) simulated broker with configurable slippage,
half-spread, per-unit commission, bracket stop/target orders, and idempotent `client_order_id` handling.

Because state is persisted in `paper_broker.sqlite`, both the FastAPI server and the standalone
`tradectl` CLI kill-switch can inspect and flatten positions even if Postgres is down.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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


def _now() -> datetime:
    return datetime.now(UTC)


class PaperBroker(BrokerAdapter):
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.db_path = Path(self.s.paper_state_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.simulated_outage: bool = False
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), timeout=10.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    cash_balance TEXT NOT NULL
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    direction TEXT NOT NULL,
                    qty TEXT NOT NULL,
                    avg_price TEXT NOT NULL,
                    stop TEXT NOT NULL,
                    target TEXT,
                    last_price TEXT NOT NULL
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    client_order_id TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS quotes (
                    symbol TEXT PRIMARY KEY,
                    price TEXT NOT NULL,
                    ts TEXT NOT NULL
                )
                """
            )
            row = c.execute("SELECT cash_balance FROM account WHERE id=1").fetchone()
            if not row:
                c.execute("INSERT INTO account (id, cash_balance) VALUES (1, ?)", (str(self.s.account_size),))

    def _check_online(self) -> None:
        if self.simulated_outage:
            raise BrokerError("PaperBroker simulated outage / timeout")

    def set_quote(self, symbol: str, price: Decimal, ts: datetime | None = None) -> None:
        t = (ts or _now()).isoformat()
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO quotes (symbol, price, ts) VALUES (?, ?, ?)",
                (symbol, str(price), t),
            )
            c.execute("UPDATE positions SET last_price=? WHERE symbol=?", (str(price), symbol))

    def adjust_cash(self, delta: Decimal) -> Decimal:
        """Used when deposits, withdrawals, funding, or adjustments are recorded so broker cash stays synced."""
        with self._conn() as c:
            row = c.execute("SELECT cash_balance FROM account WHERE id=1").fetchone()
            cur = Decimal(row["cash_balance"])
            new_bal = (cur + delta).quantize(Decimal("0.000001"))
            c.execute("UPDATE account SET cash_balance=? WHERE id=1", (str(new_bal),))
            return new_bal

    def quote(self, symbol: str, fallback_price: Decimal | None = None) -> Quote:
        self._check_online()
        with self._conn() as c:
            row = c.execute("SELECT price, ts FROM quotes WHERE symbol=?", (symbol,)).fetchone()
        if row:
            ts = datetime.fromisoformat(row["ts"])
            age = (_now() - ts).total_seconds()
            return Quote(
                symbol=symbol,
                price=Decimal(row["price"]),
                ts=ts,
                source="paper_cache",
                stale=age > self.s.price_stale_sec,
            )
        px = fallback_price if fallback_price is not None else Decimal("4060.00")
        return Quote(symbol=symbol, price=px, ts=_now(), source="fallback", stale=True)

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
        self._check_online()
        # Idempotency check on client_order_id
        with self._conn() as c:
            existing = c.execute(
                "SELECT result_json FROM orders WHERE client_order_id=?", (client_order_id,)
            ).fetchone()
            if existing:
                d = json.loads(existing["result_json"])
                return BrokerOrderResult(
                    client_order_id=d["client_order_id"],
                    broker_order_id=d["broker_order_id"],
                    symbol=d["symbol"],
                    side=d["side"],
                    qty=Decimal(d["qty"]),
                    fill_price=Decimal(d["fill_price"]),
                    slippage=Decimal(d["slippage"]),
                    fee=Decimal(d["fee"]),
                    realized_pnl=Decimal(d["realized_pnl"]),
                    status=d["status"],
                    stop=Decimal(d["stop"]) if d["stop"] is not None else None,
                    target=Decimal(d["target"]) if d["target"] is not None else None,
                    filled_at=datetime.fromisoformat(d["filled_at"]),
                    broker_fill_id=d["broker_fill_id"],
                )

        if side in ("buy", "sell") and stop is None:
            raise NakedEntryError("Every entry order must attach a stop-loss (no naked entries).")

        half_spread = self.s.paper_spread / Decimal(2)
        slip = self.s.paper_slippage + half_spread
        is_buy_leg = side in ("buy", "close_short")
        fill_px = (price_hint + slip) if is_buy_leg else (price_hint - slip)
        fill_px = fill_px.quantize(Decimal("0.0001"))

        now = _now()
        realized = Decimal(0)
        with self._conn() as c:
            pos = c.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
            cash_row = c.execute("SELECT cash_balance FROM account WHERE id=1").fetchone()
            cash = Decimal(cash_row["cash_balance"])

            if side in ("buy", "sell"):
                direction = "long" if side == "buy" else "short"
                if pos is not None:
                    raise BrokerError(f"Position already open for {symbol}")
                fee = (qty * self.s.paper_fee_per_unit).quantize(Decimal("0.000001"))
                cash -= fee
                c.execute(
                    """
                    INSERT INTO positions (symbol, direction, qty, avg_price, stop, target, last_price)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol,
                        direction,
                        str(qty),
                        str(fill_px),
                        str(stop),
                        str(target) if target is not None else None,
                        str(price_hint),
                    ),
                )
            else:
                if pos is None:
                    qty = Decimal(0)
                    fee = Decimal(0)
                else:
                    pos_qty = Decimal(pos["qty"])
                    qty = pos_qty if qty <= 0 or qty > pos_qty else qty
                    avg_px = Decimal(pos["avg_price"])
                    sign = Decimal(1) if pos["direction"] == "long" else Decimal(-1)
                    realized = ((fill_px - avg_px) * sign * qty).quantize(Decimal("0.000001"))
                    fee = (qty * self.s.paper_fee_per_unit).quantize(Decimal("0.000001"))
                    cash = cash + realized - fee
                    if qty >= pos_qty:
                        c.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
                    else:
                        c.execute(
                            "UPDATE positions SET qty=? WHERE symbol=?",
                            (str(pos_qty - qty), symbol),
                        )

            c.execute("UPDATE account SET cash_balance=? WHERE id=1", (str(cash.quantize(Decimal("0.000001"))),))
            c.execute(
                "INSERT OR REPLACE INTO quotes (symbol, price, ts) VALUES (?, ?, ?)",
                (symbol, str(price_hint), now.isoformat()),
            )

            res = BrokerOrderResult(
                client_order_id=client_order_id,
                broker_order_id=f"PB-ORD-{client_order_id}",
                symbol=symbol,
                side=side,
                qty=qty,
                fill_price=fill_px,
                slippage=(slip * qty).quantize(Decimal("0.000001")),
                fee=fee,
                realized_pnl=realized,
                status="filled",
                stop=stop,
                target=target,
                filled_at=now,
                broker_fill_id=f"PB-FILL-{client_order_id}",
            )
            c.execute(
                "INSERT INTO orders (client_order_id, result_json) VALUES (?, ?)",
                (
                    client_order_id,
                    json.dumps(
                        {
                            "client_order_id": res.client_order_id,
                            "broker_order_id": res.broker_order_id,
                            "symbol": res.symbol,
                            "side": res.side,
                            "qty": str(res.qty),
                            "fill_price": str(res.fill_price),
                            "slippage": str(res.slippage),
                            "fee": str(res.fee),
                            "realized_pnl": str(res.realized_pnl),
                            "status": res.status,
                            "stop": str(res.stop) if res.stop is not None else None,
                            "target": str(res.target) if res.target is not None else None,
                            "filled_at": res.filled_at.isoformat(),
                            "broker_fill_id": res.broker_fill_id,
                        }
                    ),
                ),
            )
            return res

    def cancel_all_orders(self, symbol: str | None = None) -> int:
        self._check_online()
        return 0

    def flatten_all_positions(self, price_hints: dict[str, Decimal] | None = None) -> list[BrokerOrderResult]:
        self._check_online()
        hints = price_hints or {}
        results: list[BrokerOrderResult] = []
        for p in self.positions():
            px = hints.get(p.symbol) or self.quote(p.symbol, fallback_price=p.avg_price).price
            side = "close_long" if p.direction == "long" else "close_short"
            cid = f"kill-{p.symbol}-{int(_now().timestamp() * 1000)}"
            results.append(
                self.place_order(
                    client_order_id=cid,
                    symbol=p.symbol,
                    side=side,
                    qty=p.qty,
                    price_hint=px,
                    stop=None,
                    target=None,
                )
            )
        return results

    def positions(self) -> list[BrokerPosition]:
        self._check_online()
        with self._conn() as c:
            rows = c.execute("SELECT * FROM positions").fetchall()
        out: list[BrokerPosition] = []
        for r in rows:
            qty = Decimal(r["qty"])
            avg = Decimal(r["avg_price"])
            last = Decimal(r["last_price"])
            sign = Decimal(1) if r["direction"] == "long" else Decimal(-1)
            upnl = ((last - avg) * sign * qty).quantize(Decimal("0.000001"))
            out.append(
                BrokerPosition(
                    symbol=r["symbol"],
                    direction=r["direction"],
                    qty=qty,
                    avg_price=avg,
                    stop=Decimal(r["stop"]) if r["stop"] else None,
                    target=Decimal(r["target"]) if r["target"] else None,
                    unrealized_pnl=upnl,
                )
            )
        return out

    def balances(self) -> BrokerBalance:
        self._check_online()
        with self._conn() as c:
            row = c.execute("SELECT cash_balance FROM account WHERE id=1").fetchone()
        cash = Decimal(row["cash_balance"])
        upnl = sum((p.unrealized_pnl for p in self.positions()), Decimal(0))
        return BrokerBalance(
            currency=self.s.base_currency,
            cash_balance=cash,
            equity=cash + upnl,
            unrealized_pnl=upnl,
        )
