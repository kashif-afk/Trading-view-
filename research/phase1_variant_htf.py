"""One pre-declared variant (declared before running): add a 4h EMA200 regime filter.
Longs only if the last *completed* 4h close > 4h EMA200; shorts only if below. Shifted by one 4h bar
so there is no look-ahead (equivalent to request.security(..., lookahead_off) on close[1]).
Reported regardless of outcome (backtest-expert: no parameter snooping)."""
import json
import sys

import pandas as pd

sys.path.insert(0, "research")
import phase1_research as r  # noqa: E402

h1, _ = r.fetch("1h", "730d")
m15, _ = r.fetch("15m", "60d")
h4 = h1.resample("4h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
bias4 = (h4.Close > r.ema(h4.Close, 200)).astype(int) * 2 - 1
# a 4h bar labelled T closes at T+4h; make it available only after that
bias4.index = bias4.index + pd.Timedelta(hours=4)

def backtest_masked(df, p):
    import numpy as np
    b = bias4.reindex(df.index, method="ffill").fillna(0).values
    c = df.Close
    f, s = r.ema(c, p.fast), r.ema(c, p.slow)
    rs = r.rsi(c, p.rsi_len)
    hr, dow = df.index.hour, df.index.day_of_week
    in_sess = (hr >= p.sess_start) & (hr < p.sess_end) & ~((dow == 4) & (hr >= 18))
    long_sig = (f > s) & (c > s) & (rs > p.rsi_mid) & (rs.shift() <= p.rsi_mid) & (rs < p.rsi_ob) & in_sess & (b == 1)
    short_sig = (f < s) & (c < s) & (rs < p.rsi_mid) & (rs.shift() >= p.rsi_mid) & (rs > p.rsi_os) & in_sess & (b == -1)
    a = r.atr(df, p.atr_len)
    O, H, L, C = df.Open.values, df.High.values, df.Low.values, c.values
    trades, pos = [], None
    for i in range(200, len(df) - 1):
        if pos is not None:
            d = pos["dir"]
            stop_hit = L[i] <= pos["stop"] if d == 1 else H[i] >= pos["stop"]
            tgt_hit = H[i] >= pos["tgt"] if d == 1 else L[i] <= pos["tgt"]
            ex = rsn = None
            if stop_hit:
                ex, rsn = (min(O[i], pos["stop"]) if d == 1 else max(O[i], pos["stop"])), "stop"
            elif tgt_hit:
                ex, rsn = pos["tgt"], "target"
            elif (d == 1 and f.iloc[i] < s.iloc[i]) or (d == -1 and f.iloc[i] > s.iloc[i]):
                ex, rsn = C[i], "trend_flip"
            elif i - pos["i"] >= p.max_bars:
                ex, rsn = C[i], "time"
            elif dow[i] == 4 and (hr[i] > 20 or (hr[i] == 20 and df.index[i].minute >= 45)):
                ex, rsn = C[i], "weekend"
            if ex is not None:
                risk = abs(pos["entry"] - pos["stop"])
                trades.append({"entry_time": df.index[pos["i"]], "dir": d,
                               "R": ((ex - pos["entry"]) * d - p.cost_per_oz) / risk, "reason": rsn})
                pos = None
                continue
        if pos is None and (long_sig.iloc[i] or short_sig.iloc[i]):
            d = 1 if long_sig.iloc[i] else -1
            e = O[i + 1]
            st = e - d * p.atr_mult * a.iloc[i]
            pos = {"dir": d, "entry": e, "stop": st, "tgt": e + d * p.rr * abs(e - st), "i": i + 1}
    return pd.DataFrame(trades)


out = {
    "15m_htf_filter_60d": r.stats(backtest_masked(m15, r.P())),
    "15m_htf_filter_60d_2x_cost": r.stats(backtest_masked(m15, r.P(cost_per_oz=1.0))),
    "1h_htf_filter_2y": r.stats(backtest_masked(h1, r.P(max_bars=12))),
    "1h_htf_filter_2y_2x_cost": r.stats(backtest_masked(h1, r.P(max_bars=12, cost_per_oz=1.0))),
}
t = backtest_masked(h1, r.P(max_bars=12))
if not t.empty:
    out["1h_htf_by_half_year"] = {str(k): v for k, v in t.groupby(t.entry_time.dt.tz_localize(None).dt.to_period("2Q")).R.agg(["count", "sum"]).round(2).to_dict("index").items()}
print(json.dumps(out, indent=1, default=str))
json.dump(out, open("research/out/phase1_variant_htf.json", "w"), indent=1, default=str)
