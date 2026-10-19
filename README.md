# V3 S&P 100 Buy-the-Dip Bot — Railway

Always-on Python service for the existing **V3** Google Sheet. Separate from the V3 9:30 AM data refresher.

## Behavior

- Runs once at **12:30 PM America/Denver** every weekday, adjusting for DST. Confirms Alpaca's market clock is open (skips holidays and early closures when the market is closed).
- Reads the `Screener` tab from [V3](https://docs.google.com/spreadsheets/d/1DEqMtRcCWBA_-HhXiQyUXkWoiC91q146_AkqS_hd4Gk/edit). **Read-only; never edits column D or any other cell.**
- Column D is `=B2-C2` (price minus SMA200). Every symbol with numeric **D < 0**, valid price and SMA200, qualifies. Retains sheet order; skips empty/invalid rows.
- Before each order, checks Alpaca market status, positions, open orders, any buy attempted **today across the account**, and whether this bot already submitted a buy for the same symbol *today*. The client order ID is deterministic per day/symbol, so restarts don't repeat an order. This is not a global atomic lock between different bots.
- For each unheld and unordered eligible symbol, requests **2% of current Alpaca `buying_power`** (Alpaca includes margin availability in that field) rounded down to cents. Rechecks buying power for every order. Submits **DAY market buy by dollar notional**. Symbols not supporting fractional orders are skipped.
- Never submits a buy if an existing holding is present, if there is an open Alpaca order for the symbol, or if another account bot already placed a buy for that symbol today (including canceled orders). Skips multiple copies of the same symbol in the sheet.
- On API errors or ambiguous order submission failures, stops that cycle rather than blindly retrying.
- **Starts in paper + DRY RUN**. Both are adjustable after inspecting Railway logs.

## GitHub and Railway

1. Create a **private** GitHub repository named `v3-sp100-buyer-bot` and put these files at the repository root: `main.py`, `requirements.txt`, `Dockerfile`, `railway.json`, `README.md`, `.gitignore`, `.env.example`, `tests/`.
2. Create a new Railway service **from that GitHub repo**. Do **not** enable Railway Cron; the Python process stays alive and handles the 12:30 Denver timezone schedule internally. Keep a single Railway replica.
3. Add Railway variables (raw values, not files):

   | Variable | Value |
   | --- | --- |
   | `APCA_API_KEY_ID` | Alpaca API key |
   | `APCA_API_SECRET_KEY` | Alpaca API secret |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | Full JSON of Google Cloud service account |
   | `GOOGLE_SPREADSHEET_ID` | `1DEqMtRcCWBA_-HhXiQyUXkWoiC91q146_AkqS_hd4Gk` (default) |
   | `GOOGLE_SHEET_TAB` | `Screener` (default) |
   | `ALPACA_PAPER` | `true` by default |
   | `DRY_RUN` | `true` by default |
   | `BUY_FRACTION` | `0.02` (default, and hard-capped at 2%) |

4. **Share the V3 spreadsheet with your Google service account's `client_email` as Viewer**. Google Sheets API must be enabled for the service-account project. Do not commit credentials or service-account JSON to GitHub.
5. Monitor a dry-run log at or after 12:30 MT on a weekday. Set `DRY_RUN=false` for **paper** order execution. To send **live** orders, additionally set `ALPACA_PAPER=false`, `DRY_RUN=false`, use live account keys, and set `LIVE_TRADING_ACK=I_ACCEPT_LIVE_MARGIN_RISK`.

## Duplicate protection and shared-account caution

The bot rechecks held positions, open orders, and same-day account buy order history before each stock and uses a stable Alpaca client order ID per trading date/symbol. This blocks repeat orders from its own restarts. If other bots simultaneously submit orders for a still-unheld symbol, a rare race remains possible because checks across independent Railway services aren't an atomic account-level lock. For a strict account-wide no-double-buy guarantee, all buying bots would need a common locking/order-coordination service. Don't run multiple replicas of this bot.

The requested 2% applies **per symbol to buying power, including margin**. Buying power is not the same as cash, and financing costs, margin calls, and forced sales are possible. There is no portfolio-wide cap in this simple strategy. Market orders can fill above the price observed in the spreadsheet.

If the stock market closes early before 12:30 Denver, the bot skips that day. A missed schedule after a service outage is not retried late; it waits for the next trading weekday.

## Local tests

```bash
python -m unittest discover -s tests -v
```

No trading credentials are needed for unit tests. The service does not execute on deployment startup; it waits for its next scheduled 12:30 MT cycle.
