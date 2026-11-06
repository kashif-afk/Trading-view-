"""Phase 1 research: XAUUSD regime read, stop-distance / sizing feasibility, prototype backtest.

Methodology mirrors (not copies) these skills:
  - technical-analyst: trend / MA alignment / S-R read, done numerically instead of from chart images
  - backtest-expert:   pessimistic fills, parameter-sensitivity grid, sample-size warnings
  - position-sizer:    qty = equity*risk / |entry-stop|, floored to the lot step, rejected below the minimum

Research only. Places no orders. Run:  .venv\\Scripts\\python research\\phase1_research.py
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

CACHE = Path(__file__).parent / "cache"
OUT = Path(__file__).parent / "out"
CACHE.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)

TICKERS = ["XAUUSD=X", "GC=F"]  # spot first; COMEX futures as fallback proxy (has basis/roll)
EQUITY = 2000.0
RISK_PCT = 0.5


# ---------------------------------------------------------------- data
def fetch(interval: str, period: str) -> tuple[pd.DataFrame, str]:
    import yfinance as yf

    for t in TICKERS:  # cache first (offline re-runs), in ticker preference order
        f = CACHE / f"{t.replace('=', '_')}_{interval}.csv"
        if f.exists():
            df = pd.read_csv(f, index_col=0, parse_dates=True)
            if len(df) > 100:
                return df, t
    for t in TICKERS:
        f = CACHE / f"{t.replace('=', '_')}_{interval}.csv"
        df = yf.download(t, interval=interval, period=period, progress=False, auto_adjust=False)
        if df is None or df.empty or len(df) < 100:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close"]].dropna()
        df.index = pd.to_datetime(df.index, utc=True)
        df.to_csv(f)
        return df, t
    raise RuntimeError(f"no data for {interval}")


# ---------------------------------------------------------------- indicators (TradingView-equivalent)
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rma(s: pd.Series, n: int) -> pd.Series:  # Wilder, = ta.rma
    return s.ewm(alpha=1 / n, adjust=False).mean()


def rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up, dn = rma(d.clip(lower=0), n), rma((-d).clip(lower=0), n)
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df.Close.shift()
    tr = pd.concat([df.High - df.Low, (df.High - pc).abs(), (df.Low - pc).abs()], axis=1).max(axis=1)
    return rma(tr, n)


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    upm, dnm = df.High.diff(), -df.Low.diff()
    pdm = np.where((upm > dnm) & (upm > 0), upm, 0.0)
    ndm = np.where((dnm > upm) & (dnm > 0), dnm, 0.0)
    a = atr(df, n)
    pdi = 100 * rma(pd.Series(pdm, df.index), n) / a
    ndi = 100 * rma(pd.Series(ndm, df.index), n) / a
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return rma(dx, n)


# ---------------------------------------------------------------- regime
def regime(df: pd.DataFrame, label: str) -> dict:
    c = df.Close
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    a = atr(df)
    atrp = (a / c * 100)
    last = -1
    slope50 = (e50.iloc[last] / e50.iloc[last - 10] - 1) * 100
    hi, lo = df.High.tail(100).max(), df.Low.tail(100).min()
    return {
        "tf": label,
        "last_bar": str(df.index[last]),
        "close": round(float(c.iloc[last]), 2),
        "ema20": round(float(e20.iloc[last]), 2),
        "ema50": round(float(e50.iloc[last]), 2),
        "ema200": round(float(e200.iloc[last]), 2),
        "alignment": "bull" if e20.iloc[last] > e50.iloc[last] > e200.iloc[last]
        else "bear" if e20.iloc[last] < e50.iloc[last] < e200.iloc[last] else "mixed",
        "ema50_slope_10bars_pct": round(float(slope50), 3),
        "rsi14": round(float(rsi(c).iloc[last]), 1),
        "adx14": round(float(adx(df).iloc[last]), 1),
        "atr14": round(float(a.iloc[last]), 2),
        "atr_pct": round(float(atrp.iloc[last]), 3),
        "atr_pct_percentile_1y": round(float((atrp.tail(min(len(atrp), 252 * 24)) < atrp.iloc[last]).mean() * 100), 0),
        "range100_high": round(float(hi), 2),
        "range100_low": round(float(lo), 2),
    }


# ---------------------------------------------------------------- strategy (prototype of strategy-spec v0.1)
@dataclass
class P:
    fast: int = 20
    slow: int = 50
    rsi_len: int = 14
    rsi_mid: float = 50
    rsi_ob: float = 70
    rsi_os: float = 30
    atr_len: int = 14
    atr_mult: float = 1.5
    rr: float = 2.0
    max_bars: int = 48
    sess_start: int = 7   # UTC hour, inclusive
    sess_end: int = 20    # UTC hour, exclusive
    cost_per_oz: float = 0.50  # round-trip spread+slippage, USD per oz (pessimistic for OANDA XAU)


def backtest(df: pd.DataFrame, p: P) -> pd.DataFrame:
    c = df.Close
    f, s = ema(c, p.fast), ema(c, p.slow)
    r = rsi(c, p.rsi_len)
    a = atr(df, p.atr_len)
    hr = df.index.hour
    dow = df.index.dayofweek
    in_sess = (hr >= p.sess_start) & (hr < p.sess_end) & ~((dow == 4) & (hr >= 18))
    long_sig = (f > s) & (c > s) & (r > p.rsi_mid) & (r.shift() <= p.rsi_mid) & (r < p.rsi_ob) & in_sess
    short_sig = (f < s) & (c < s) & (r < p.rsi_mid) & (r.shift() >= p.rsi_mid) & (r > p.rsi_os) & in_sess
    trend_flip_long = (f < s)   # exit long when fast crosses below slow (evaluated on close)
    trend_flip_short = (f > s)

    O, H, L, C = df.Open.values, df.High.values, df.Low.values, c.values
    trades, pos = [], None
    n = len(df)
    for i in range(max(p.slow, 200), n - 1):
        if pos is not None:
            d = pos["dir"]
            # weekend flatten: Friday >= 20:45 UTC close at this bar's close
            stop_hit = L[i] <= pos["stop"] if d == 1 else H[i] >= pos["stop"]
            tgt_hit = H[i] >= pos["tgt"] if d == 1 else L[i] <= pos["tgt"]
            exit_px = reason = None
            if stop_hit:  # pessimistic: stop first if both inside one bar; gap-through fills at open
                exit_px = min(O[i], pos["stop"]) if d == 1 else max(O[i], pos["stop"])
                reason = "stop"
            elif tgt_hit:
                exit_px, reason = pos["tgt"], "target"
            elif (d == 1 and trend_flip_long.iloc[i]) or (d == -1 and trend_flip_short.iloc[i]):
                exit_px, reason = C[i], "trend_flip"
            elif i - pos["i"] >= p.max_bars:
                exit_px, reason = C[i], "time"
            elif dow[i] == 4 and (hr[i] > 20 or (hr[i] == 20 and df.index[i].minute >= 45)):
                exit_px, reason = C[i], "weekend"
            if exit_px is not None:
                risk = abs(pos["entry"] - pos["stop0"])
                pnl_oz = (exit_px - pos["entry"]) * d - p.cost_per_oz
                trades.append({"entry_time": df.index[pos["i"]], "exit_time": df.index[i], "dir": d,
                               "entry": pos["entry"], "exit": exit_px, "risk_oz": risk,
                               "R": pnl_oz / risk, "reason": reason})
                pos = None
                continue
        if pos is None and (long_sig.iloc[i] or short_sig.iloc[i]) and not np.isnan(a.iloc[i]):
            d = 1 if long_sig.iloc[i] else -1
            entry = O[i + 1]  # signal on confirmed close, fill next bar open (TV default)
            stop = entry - d * p.atr_mult * a.iloc[i]
            pos = {"dir": d, "entry": entry, "stop": stop, "stop0": stop,
                   "tgt": entry + d * p.rr * abs(entry - stop), "i": i + 1}
    return pd.DataFrame(trades)


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"trades": 0}
    R = t.R
    eq = R.cumsum()
    gw, gl = R[R > 0].sum(), -R[R < 0].sum()
    return {
        "trades": int(len(t)),
        "win_rate": round(float((R > 0).mean() * 100), 1),
        "avg_R": round(float(R.mean()), 3),
        "profit_factor": round(float(gw / gl), 2) if gl > 0 else None,
        "total_R": round(float(R.sum()), 2),
        "max_dd_R": round(float((eq.cummax() - eq).max()), 2),
        "longs": int((t.dir == 1).sum()),
        "shorts": int((t.dir == -1).sum()),
        "exit_reasons": t.reason.value_counts().to_dict(),
    }


def sizing_feasibility(df: pd.DataFrame, p: P) -> dict:
    stop_d = (p.atr_mult * atr(df, p.atr_len)).dropna().tail(len(df) - 200)
    risk_usd = EQUITY * RISK_PCT / 100
    q = risk_usd / stop_d
    out = {"risk_usd": risk_usd,
           "stop_dist_usd_p10_p50_p90": [round(float(x), 2) for x in stop_d.quantile([.1, .5, .9])],
           "ideal_qty_oz_p10_p50_p90": [round(float(x), 3) for x in q.quantile([.1, .5, .9])]}
    for min_lot in (1.0, 0.1, 0.01):
        out[f"pct_bars_tradeable_min_{min_lot}oz"] = round(float((q >= min_lot).mean() * 100), 1)
        floored = np.floor(q / min_lot) * min_lot
        eff = (floored * stop_d / risk_usd)[floored > 0]
        out[f"avg_realised_risk_pct_of_budget_min_{min_lot}oz"] = round(float(eff.mean() * 100), 1) if len(eff) else 0
    return out


def main() -> None:
    report: dict = {"source": {}, "regime": [], "sizing": {}, "backtests": {}}
    frames = {}
    for interval, period, label in [("15m", "60d", "15m"), ("1h", "730d", "1h"), ("1d", "10y", "1D")]:
        df, t = fetch(interval, period)
        frames[label] = df
        report["source"][label] = {"ticker": t, "bars": len(df), "from": str(df.index[0]), "to": str(df.index[-1])}
    # 4h from 1h
    h4 = frames["1h"].resample("4h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    frames["4h"] = h4
    for lbl in ("15m", "1h", "4h", "1D"):
        report["regime"].append(regime(frames[lbl], lbl))

    base = P()
    report["sizing"]["15m"] = sizing_feasibility(frames["15m"], base)

    # baseline on 15m (only ~60 days: too short), and on 1h over 2y as a longer-sample proxy
    t15 = backtest(frames["15m"], base)
    report["backtests"]["15m_baseline_60d"] = stats(t15)
    t1h = backtest(frames["1h"], P(max_bars=12))
    report["backtests"]["1h_proxy_2y"] = stats(t1h)
    if not t1h.empty:
        by_q = t1h.groupby(t1h.entry_time.dt.to_period("Q")).R.agg(["count", "sum"]).round(2)
        report["backtests"]["1h_proxy_2y_by_quarter"] = {str(k): v for k, v in by_q.to_dict("index").items()}

    # parameter sensitivity per backtest-expert (stop 50-150%, rr 80-120%), on 1h proxy
    grid = []
    for am in (0.75, 1.125, 1.5, 1.875, 2.25):
        for rr in (1.6, 1.8, 2.0, 2.2, 2.4):
            s = stats(backtest(frames["1h"], P(atr_mult=am, rr=rr, max_bars=12)))
            grid.append({"atr_mult": am, "rr": rr, "trades": s.get("trades"), "avg_R": s.get("avg_R"),
                         "pf": s.get("profit_factor")})
    report["backtests"]["1h_sensitivity"] = grid
    # friction stress: 2x cost
    report["backtests"]["1h_proxy_2x_cost"] = stats(backtest(frames["1h"], P(max_bars=12, cost_per_oz=1.0)))
    report["backtests"]["15m_2x_cost"] = stats(backtest(frames["15m"], P(cost_per_oz=1.0)))

    (OUT / "phase1_report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
