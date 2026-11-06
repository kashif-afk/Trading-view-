# Strategy Spec — XAUUSD 15m "EMA Trend + RSI Resumption + ATR Stop"

| Field | Value |
|---|---|
| Spec version | **v0.1** (draft, not approved) |
| Instrument | XAUUSD (signal chart `OANDA:XAUUSD`, mapped by engine to broker symbol, see §8) |
| Timeframe | 15m, signals on **confirmed bar close only** |
| Status | **Research. Prototype backtest shows no edge after costs (§6). Not fit for live money.** |
| Skills mirrored | `technical-analyst` (trend/MA read), `backtest-expert` (test plan, bias checks), `position-sizer` (sizing), `economic-calendar-fetcher` (news blackout), `pre-trade-discipline-gate` / `drawdown-circuit-breaker` (engine-side filters) |

## 1. Hypothesis (one sentence, per backtest-expert)
When gold's 15m trend is up (EMA20 > EMA50), a momentum resumption (RSI crossing back above 50 after a pullback) during the liquid London/NY sessions continues far enough to pay 2R more often than 1 in 3 times after costs. Shorts are the mirror image.

## 2. Current regime read (as of 2026-11-06 15:30 UTC, GC=F proxy)
Method: `technical-analyst` framework, applied numerically to MA alignment, slope, ADX and ATR percentile, because no chart images were provided.

| TF | Close | EMA20 / 50 / 200 | Alignment | RSI | ADX | ATR (pctile 1y) |
|---|---|---|---|---|---|---|
| 1D | 4060 | 4081 / 4179 / 4281 | **bear** (EMA50 slope −1.8%/10d) | 46 | 28 | \$86.6 (88th) |
| 4h | 4060 | 4055 / 4037 / 4132 | mixed (rebound below EMA200) | 53 | 30 | \$39.2 (78th) |
| 1h | 4060 | 4075 / 4072 / 4042 | bull, but rolling over | 42 | 26 | \$20.0 (63rd) |
| 15m | 4060 | 4063 / 4070 / 4072 | bear | 46 | 31 | \$10.7 (90th) |

**Summary:** the daily trend is a correction from the highs (100-day range 3879–4755). The 4h is a counter-trend bounce under its EMA200, and volatility is **high** (ATR in the 78th–90th percentile). That means wide stops and smaller position sizes, and timeframes often disagree, which is the condition where a 15m trend-follower whipsaws.

`market-breadth-analyzer` / `uptrend-analyzer` were **not used**: they measure US-equity breadth and don't apply to a single metal. Macro drivers for gold (USD, real yields, CPI/NFP/FOMC) are handled by the news blackout filter (F3). A live calendar needs an `FMP_API_KEY` (free tier), which isn't set yet.

## 3. Exact rules (v0.1)
All values are evaluated on the **close of a confirmed 15m bar**. Orders fill at the **next bar's open**.

**Indicators:** `EMAf = EMA(close,20)`, `EMAs = EMA(close,50)`, `RSI = RSI(close,14)` (Wilder), `ATR = ATR(14)` (Wilder/RMA).

| Rule | Long | Short |
|---|---|---|
| Trend | `EMAf > EMAs` **and** `close > EMAs` | `EMAf < EMAs` **and** `close < EMAs` |
| Trigger | `RSI` crosses **above** 50 (`RSI[1] ≤ 50 < RSI`) | `RSI` crosses **below** 50 |
| Overextension filter | `RSI < 70` | `RSI > 30` |
| Stop | `entry − 1.5 × ATR(signal bar)` | `entry + 1.5 × ATR` |
| Target | `entry + 2.0 × (entry − stop)` (2R) | mirror |
| Trend exit | close with `EMAf < EMAs` → exit at close | `EMAf > EMAs` |
| Time stop | 48 bars (12 h) → exit at close | same |
| Weekend flatten | Friday bar closing ≥ 20:45 UTC → exit at close | same |
| Position | One per symbol, no pyramiding, no reversal on the same bar | |

**Filters**
- **F1 Session:** entries only 07:00–20:00 UTC, Mon–Fri, and no new entries Friday ≥ 18:00 UTC. *Default ON; you haven't confirmed this yet.*
- **F2 HTF regime (optional, default OFF):** longs only if the last **completed** 4h close > 4h EMA200, shorts only if below (`request.security` with `lookahead_off` on `close[1]`).
- **F3 News blackout (engine-side):** no entries from 30 min before to 30 min after high-impact USD events (CPI, NFP, FOMC, PCE). Source: `economic-calendar-fetcher` (FMP). If the calendar is unavailable, **fail closed** (no entries) in a ±30 min window around 12:30, 14:00 and 18:00 UTC on known release days. Your approval needed.
- **F4 Engine risk gate:** the circuit breaker, max positions, revenge block (30 min after a stop-out, same symbol and direction) and exposure ceiling come from the risk gate (Phase 3), not from Pine.

**Sizing** (`position-sizer` logic): `qty = floor_to_step( equity × 0.5% / |entry − stop| )`. If `qty < broker_min_qty`, the trade is **rejected, never rounded up**. If notional exceeds the margin or exposure ceiling, qty is reduced.

## 4. Sizing feasibility at \$2,000 / 0.5% (\$10 risk)
15m, last 60 days:

| | p10 | median | p90 |
|---|---|---|---|
| Stop distance (1.5 ATR) | \$8.62 | \$11.42 | \$16.29 |
| Ideal qty (oz) | 0.61 | 0.88 | 1.16 |

| Broker min size | % of bars tradeable | Avg. realized risk vs budget |
|---|---|---|
| 1 oz (typical CFD 0.01 lot, e.g. MT5) | **27%** | 87% |
| 0.1 oz | 100% | 94% |
| 0.01 oz (OANDA units / XAUTUSDT) | 100% | 99% |

**Implication:** with a broker whose minimum is 1 oz, about 3 out of 4 signals would be rejected for size at the current volatility. Use a broker with fractional units, or accept the reduced trade count.

## 5. Backtest plan (`backtest-expert`)
1. **Data:** OANDA:XAUUSD 15m in TradingView Strategy Tester (Deep Backtesting needs Premium), plus a Python replica on downloaded data. Minimum 5 years; 15m history from free sources covers only about 60 days.
2. **Costs:** spread + slippage \$0.50/oz round-trip baseline, \$1.00/oz stress. Overnight financing modelled once the broker is known.
3. **Fills:** next-bar open. If stop and target are both touched in one bar, assume the **stop** first. Gaps through the stop fill at the open.
4. **Sensitivity:** ATR mult {0.75, 1.125, 1.5, 1.875, 2.25} × RR {1.6 … 2.4}. Look for a plateau, not a peak. EMA lengths ±25%.
5. **Time robustness:** per-quarter and per-year R. Require positive expectancy in most periods.
6. **Out-of-sample:** walk-forward with a 3-year train and 1-year test, rolled forward. Out-of-sample must be ≥ 50% of in-sample.
7. **Sample size:** ≥ 100 trades for a decision, ≥ 200 for confidence.
8. **Deploy criteria:** avg R ≥ +0.10 at 2× costs, PF ≥ 1.2, max DD ≤ 15R, positive in ≥ 60% of quarters.

**Overfitting and bias risks flagged**
- *Look-ahead:* HTF data must use `lookahead_off` + `[1]`. TradingView fills `strategy.exit` stops intrabar, which the Python replica must match.
- *Repainting:* `calc_on_every_tick=false`, signals gated by `barstate.isconfirmed`.
- *Parameter snooping:* the sensitivity grid has already been looked at, so any parameter chosen *from* it is in-sample. A new candidate must be validated on data not yet examined.
- *Too few trades:* 60 days of 15m gives only about 73 trades, below the 100-trade decision threshold.
- *Proxy data:* GC=F futures differ from spot XAUUSD by basis, roll jumps and session times, so the TradingView OANDA:XAUUSD run is authoritative.
- *Regime dependence:* the 2-year results swing from +9R to −10R per quarter.

## 6. Prototype results (Python, GC=F proxy, `research/phase1_research.py`)
| Run | Trades | Win% | Avg R | PF | Total R | Max DD (R) |
|---|---|---|---|---|---|---|
| **15m baseline, 60d** | 73 | 31.5 | **−0.146** | 0.79 | −10.7 | 21.1 |
| 15m, 2× cost | 73 | 31.5 | −0.187 | 0.75 | −13.7 | 23.4 |
| 1h proxy, 2y | 294 | 40.8 | −0.030 | 0.95 | −8.8 | 23.7 |
| 1h proxy, 2y, 2× cost | 294 | 40.5 | −0.059 | 0.90 | −17.4 | 26.2 |
| 15m + F2 (4h EMA200), 60d | 38 | 28.9 | −0.147 | 0.79 | −5.6 | 13.5 |
| 1h + F2, 2y | 195 | 43.1 | −0.005 | 0.99 | −1.0 | 10.4 |

**Sensitivity (1h, 2y):** all 25 cells have negative avg R (−0.19 to −0.01). There is no plateau, just a slope that flattens towards wider stops. F2 was the **one variant declared in advance**. It halves the drawdown, but expectancy is still ≈ 0.

**Verdict (`backtest-expert` criteria): ❌ Abandon or refine.** As specified, the idea has no demonstrated edge after costs. This does **not** block building the system: the plumbing (webhook → risk gate → paper broker → ledger → dashboard) is strategy-agnostic, and paper mode costs nothing. It **does** block live trading with this rule set.

## 7. Options (your decision)
1. **Build the system with v0.1 (+F2) in paper mode as a test signal source**, while researching a better edge in parallel. Recommended.
2. Replace the idea before Phase 2. Candidates to evaluate *out of sample*: London-open range breakout, or daily-trend pullback on 1h. Run through `edge-strategy-designer` / `backtest-expert`.
3. Move to 1h execution, where costs are a smaller share of R.

## 8. Symbol mapping
`OANDA:XAUUSD` → broker symbol (`XAU_USD` OANDA / `XAUUSD+` Bybit MT5 / `XAUTUSDT` Bybit perp). The engine re-checks `price` against the broker quote. If they differ by more than 0.3 × ATR, the signal is rejected as `stale_or_basis_gap`.

## Change log
| Version | Date | Change | Evidence |
|---|---|---|---|
| v0.1 | 2026-11-06 | Initial spec | Prototype backtest §6 |
