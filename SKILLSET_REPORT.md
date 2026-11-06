# Claude Trading Skills: What It Does and How I Use It

Source: github.com/tradermonty/claude-trading-skills (MIT licence, commit ff8ca70). Installed in `.claude/skills/` (74 skills), with 2 agents and 2 slash commands.

## 1. What it is
A set of instruction packs ("skills") plus helper Python scripts. When you ask a trading or investing question, I load the matching skill, follow its workflow, run its scripts on market data, and write you a report. The skills **analyze and plan**. They do not place trades. Nothing in the scripts submits broker orders.

## 2. How I use it
1. You ask something, such as "is the market healthy?" or "screen for VCP setups".
2. I pick the skill whose description matches. If you are unsure where to start, `trading-skills-navigator` recommends a workflow.
3. I run its scripts (fetch data, score, screen) and read its reference notes.
4. I give you a report: scores, ranked candidates, entry/stop/size plans, and the caveats.

## 3. What the skills cover

| Area | Skills | What you get |
|---|---|---|
| **Market health** | market-breadth-analyzer, uptrend-analyzer, sector-analyst, breadth-chart-analyst, macro-regime-detector, market-environment-analysis, crypto-regime-analyzer | A 0-100 health score and a read on whether the market supports adding exposure |
| **Top and bottom timing** | market-top-detector, ibd-distribution-day-monitor, ftd-detector, us-market-bubble-detector | Warning signs of a top, confirmation of a bottom, and bubble risk |
| **Stock screening** | canslim-screener, vcp-screener, pead-screener, earnings-trade-analyzer, dividend and value screeners, finviz-screener, us-undervalued-growth-screener, institutional-flow-tracker | Ranked candidate lists |
| **Stockbee-style setups** | stockbee-momentum-burst-screener, stockbee-episodic-pivot-analyzer, stockbee-exhaustion-hammer-screener, stockbee-20pct-study, stockbee-setup-fluency-trainer | Short-term setup candidates and a study journal |
| **Trade planning and sizing** | breakout-trade-planner, parabolic-short-trade-planner, position-sizer, futures-position-sizer, options-strategy-advisor, pair-trade-screener | Entry, stop and target levels, share or contract counts, options P/L |
| **Contrarian (Jason Shapiro) chain** | cot-contrarian-detector, news-reaction-failure-analyzer, contrarian-setup-gate | Crowded futures positioning, then whether the market ignored news, then a final go/no-go status |
| **Discipline and risk** | pre-trade-discipline-gate, drawdown-circuit-breaker, exposure-coach | Blocks oversized or revenge entries, applies drawdown limits, suggests a net exposure ceiling |
| **Journal and review** | trader-memory-core, signal-postmortem, trade-performance-coach, weekly-performance-digest | Thesis tracking, post-trade analysis, win rate, expectancy and behaviour patterns |
| **Strategy research** | backtest-expert, manifoldbt-backtester, residual-edge-analyzer, strategy-pivot-designer, the edge-* pipeline skills, trade-hypothesis-ideator | Backtest checks, tests of whether returns are real edge, and strategy drafts with reviews |
| **News and calendars** | market-news-analyst, scenario-analyzer, earnings-calendar, economic-calendar-fetcher, fxmacrodata-calendar | News impact reports, 18-month scenarios, event calendars |
| **Portfolio and fundamentals** | portfolio-manager, us-stock-analysis, technical-analyst, theme-detector, stanley-druckenmiller-investment | Portfolio review, single-stock reports, and a combined conviction score |
| **Kanchi dividend workflow** | kanchi-dividend-sop, kanchi-dividend-review-monitor, kanchi-dividend-us-tax-accounting | Dividend screening, cut-warning checks, and account-placement guidance |
| **Tooling** | skill-designer, skill-idea-miner, dual-axis-skill-reviewer, skill-integration-tester, data-quality-checker, mt5-robot-tester | Maintenance of the skills themselves; MT5 robot batch testing |

## 4. Typical workflows
- **Swing trading:** market health (breadth, uptrend) → top/bottom check → exposure-coach → screener (VCP or CANSLIM) → breakout-trade-planner → position-sizer → pre-trade-discipline-gate → trader-memory-core to log the trade.
- **Weekly review:** closed trades → signal-postmortem → trade-performance-coach → weekly-performance-digest.
- **Strategy research:** hypothesis → backtest → backtest-expert and residual-edge-analyzer → strategy-pivot-designer if the results stall.

## 5. What it needs
| Need | Detail |
|---|---|
| **No API key** | market-breadth-analyzer, uptrend-analyzer, sector-analyst, crypto-regime-analyzer, the Kanchi skills, edge-signal-aggregator, and position-sizer |
| **FMP_API_KEY** | Most screeners and calendars. The free tier allows 250 requests a day |
| **FINVIZ_API_KEY** | Optional and paid. Speeds up some screeners |
| **Alpaca key** | portfolio-manager (holdings) and the intraday phase of the parabolic short planner |
| **Python** | `python` works (3.14.3). The packages below are not installed yet, and `uv` is missing |
| **Packages** | pyyaml, requests, pandas, numpy, scipy, statsmodels, yfinance, beautifulsoup4, lxml, jsonschema, pillow, opencv-python-headless, finvizfinance, pandas-market-calendars, manifoldbt |

## 6. Cautions
- The output is analysis, not financial advice. Check data and levels before you act.
- **skill-idea-miner** reads your Claude Code session logs in `~/.claude/projects/` and passes them to `claude -p`. Run it only if you want that.
- **mt5-robot-tester** starts and stops local MetaTrader processes. Windows and MT5 are required.
- Several skills' instructions say `python3`, which does not work on this PC. I run `python` instead.
- The two commands, `/scenario-analyzer` and `/us-undervalued-growth-screen`, had their paths edited to `.claude/skills/`.

## 7. Suggested first run
Run `market-breadth-analyzer` or `uptrend-analyzer` first. They need no API key and show the whole loop of fetching data, scoring and writing a report. Then add an FMP key to unlock the screeners.
