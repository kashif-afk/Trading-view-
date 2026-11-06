# Go-Live Checklist (`docs/go-live-checklist.md`)

> **STOP:** Never switch to `live` mode automatically. Going live is exclusively your decision after at least **2–4 weeks of continuous paper trading** and a full strategy edge review.

## 1. Strategy Edge & Paper-Run Verification (2–4 Weeks Minimum)
- [ ] **Strategy Edge Proven:** Note that `strategy-spec.md` v0.1 showed negative expectancy after costs on the 15m/1h historical proxy. Do **not** trade v0.1 with real money unless you have replaced or refined the entry/exit rules, validated out-of-sample via `backtest-expert`, and achieved positive expectancy across >= 100 trades.
- [ ] **Paper vs Backtest Comparison:** Run `python -m cli.tradectl reconcile` and click **Run Weekly Learning Digest** on `/performance` to compare 2–4 weeks of live-paper fills, slippage, and win rate against the TradingView Strategy Tester report (`docs/strategy-tester-comparison.md`).
- [ ] **Minimum Order Size Check:** Confirm your broker supports fractional gold units (e.g. OANDA `XAU_USD` or `XAUTUSDT`) so a $10 risk budget (`0.5%` of `$2,000`) is not rejected on wide ATR bars.

## 2. Broker API Key & Account Safety
- [ ] **Trade-Only Key:** Generate a fresh broker API key with **Trade permissions ONLY** and **Withdrawals explicitly DISABLED**.
- [ ] **IP Restriction:** Restrict the API key at the broker to your server/VPS outbound IPv4 address.
- [ ] **Secrets Isolation:** Confirm `.env` is not tracked by git (`git status --ignored`) and contains strong random values for `WEBHOOK_SECRET`, `SESSION_SECRET`, and `ADMIN_PASSWORD`.

## 3. Infrastructure & Fallback Readiness
- [ ] **HTTPS & 2FA:** TradingView account has **2FA enabled**, and your webhook URL uses HTTPS on port 443 (`cloudflared` tunnel or reverse proxy).
- [ ] **Secondary Alert Channel:** Telegram bot (`TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`) and TradingView email/app alerts are active.
- [ ] **Kill Switch Drill:** Verify both the dashboard **KILL SWITCH** button and `python -m cli.tradectl kill` flatten a practice position within 2 seconds.
- [ ] **Backup & Restore Drill:** Run `tools/backup_restore.py` and verify the restored ledger waterfall matches your broker balance to the cent.

## 4. Explicit Two-Step Live Activation (Manual Only)
1. Set `EXECUTION_MODE=live` and `OANDA_ENV=live` in `.env`.
2. Run the interactive confirmation command in your terminal and type the exact confirmation phrase:
   ```powershell
   .venv\Scripts\python -m cli.tradectl set-live
   ```
   *(Any later change to risk parameters changes the risk fingerprint hash and automatically invalidates live confirmation until you re-confirm.)*
