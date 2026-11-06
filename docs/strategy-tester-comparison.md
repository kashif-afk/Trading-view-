# Strategy Tester vs Phase 1 Spec Comparison

Because TradingView Strategy Tester runs inside your browser session, paste `tradingview/strategy.pine` onto `OANDA:XAUUSD` (15m) and record the Strategy Tester metrics next to the Phase 1 Python prototype below.

## 1. Comparison Table
| Metric | Phase 1 Python (`GC=F` 15m, +F2, 60d) | TradingView Strategy Tester (`OANDA:XAUUSD` 15m) | Notes / Delta |
|---|---|---|---|
| Symbol / Feed | `GC=F` (COMEX Futures proxy) | `OANDA:XAUUSD` (Spot CFD) | |
| Date Range | 2026-08-28 → 2026-11-06 (~60d) | *(fill after running)* | |
| Total Trades | 38 | *(fill)* | |
| Win Rate (%) | 28.9% | *(fill)* | |
| Profit Factor | 0.79 | *(fill)* | |
| Avg Trade (R / $) | −0.147 R | *(fill)* | |
| Max Drawdown | 13.45 R | *(fill)* | |

## 2. Expected Differences & Why They Occur
1. **Futures (`GC=F`) vs Spot (`OANDA:XAUUSD`) Basis & Roll:** COMEX gold futures carry interest-rate basis above spot and experience contract roll gaps, plus a daily maintenance break (21:00–22:00 UTC) that differs slightly from OANDA's daily break.
2. **Stop/Target Anchor Price:** In `strategy.pine`, `activeStop` and `activeTarget` are calculated from the confirmed signal bar's `close` so the webhook alert carries exact levels at bar close; the fill itself occurs at the next bar's `open`. In the Python prototype, stop distance was anchored to `open[i+1]`. On bars with an overnight/intrabar gap between close and next open, effective R differs slightly.
3. **Intrabar Fill Ordering:** Both the Python prototype and TradingView default OHLC synthesis make assumptions when both `stop` and `limit` fall inside a single 15m bar's High–Low range. Enabling **Bar Magnifier** (Premium) uses 1m bars for intrabar sequence; without it, TradingView uses its standard heuristic while Python strictly assumed stop-first (pessimistic).
4. **Sizing Rejections at $10 Risk:** On bars where `1.5 * ATR > $10` and `minLot` is set to `1.0`, trades are skipped. In `strategy.pine`, `minLot` defaults to `0.01` oz (matching OANDA fractional units and `PaperBroker`), allowing 100% of signals to size cleanly.
