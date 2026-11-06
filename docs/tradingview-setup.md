# TradingView Setup Guide (`tradingview/strategy.pine`)

## 1. Platform Requirements
- **Plan:** TradingView **Essential, Plus, or Premium** (free Basic plans do not support webhook alerts).
- **2FA:** Two-Factor Authentication **must be enabled** on your TradingView account or webhooks will not fire.
- **Ports & Protocol:** TradingView only sends webhooks to **port 80 (HTTP)** or **port 443 (HTTPS)** over **IPv4**. Custom ports (e.g. `:8000`) are rejected.
- **Source IPs (for firewall/proxy allowlisting):**
  - `52.89.214.238`
  - `34.212.75.30`
  - `54.218.53.128`
  - `52.32.178.7`
- **Timeout:** TradingView cancels requests taking longer than **3 seconds**. Our `/webhook` endpoint writes to the append-only raw log, validates, enqueues, and replies within milliseconds.

## 2. Adding the Strategy to the Chart
1. Open TradingView and select the **`OANDA:XAUUSD`** chart on the **15m** timeframe.
2. Open the **Pine Editor** tab at the bottom.
3. Copy the entire contents of `tradingview/strategy.pine` into the editor.
4. Click **Save** (keep it **private** — never publish a script containing your secret) and click **Add to chart**.
5. Open the strategy **Settings (gear icon) → Inputs**:
   - Set **Webhook Secret** to the exact value of `WEBHOOK_SECRET` in your `.env` file.
   - Verify **Use HTF EMA Regime Filter (F2)** is checked (default ON).
   - Click **OK**.

## 3. Exposing Your Local Webhook via Cloudflared
When running locally on your PC, start a tunnel that serves HTTPS on port 443 and forwards to port 8000:
```powershell
cloudflared tunnel --url http://localhost:8000
```
Copy the generated `https://<random-words>.trycloudflare.com` URL.

## 4. Creating the Webhook Alert
1. Click the **Alert (clock) icon** in TradingView.
2. **Condition:** Select `XAUUSD EMA-RSI-ATR v0.1`, and underneath choose **"alert() function calls only"**.
3. **Expiration:** Open-ended (or longest available on your plan).
4. **Notifications tab:**
   - Check **Webhook URL** and paste `https://<your-tunnel-host>/webhook`.
   - Also enable **Email** or **App notification** as your secondary backup channel so no alert goes unseen if the tunnel is down.
5. Click **Create**.

> **Important:** TradingView snapshots script inputs when an alert is created. Whenever you change any strategy input (or rotate `WEBHOOK_SECRET`), **delete and recreate the alert**.

## 5. Testing Without a Paid TradingView Plan
Use the built-in simulator to send signed, timestamped webhook payloads identical to `strategy.pine`:
```powershell
.venv\Scripts\python tools\alert_sim.py --action buy --price 4060.0 --stop 4048.0 --target 4084.0
```
