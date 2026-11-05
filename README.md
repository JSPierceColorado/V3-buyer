# V3.1 — 10-minute trailing-rebound entry (staging candidate)

**Not deployed.** This is a replacement for `JSPierceColorado/V3-buyer/main.py`, designed to be reviewed and tested on paper trading before enabling live orders. The existing `v3-screen` service stays unchanged.

## Behavior

- Once per weekday, after 12:30 America/Denver and only while Alpaca says the market is open, read the existing `Screener` tab (`A:D`) and add new `D < 0` candidates to PostgreSQL. The candidate's initial low is the first **fresh Alpaca trade price**, not the potentially older Sheet price.
- Every 600 seconds, while the market is open, request recent Alpaca snapshot trades (default IEX). A price must be positive, at most 15 minutes old, and timestamped within regular trading hours. Update each candidate's lowest observed price and calculate `trigger = low * 1.05`.
- On an eligible rebound, run existing account-wide held/open-order/same-day-buy checks; also skip recently sold symbols for 24 hours by default. Keep the existing 2%-of-buying-power cap, fractionability check, market order, and live-trading acknowledgment.
- Persist order intent before submitting. Reconcile status on later cycles; remove a symbol **only after a confirmed fill**. Missing/uncertain order states stay pending for manual investigation. Rejected/canceled orders become blocked until reviewed.
- A symbol may remain watched even if the Sheet signal disappears on later days. A symbol may be added again after an eventual sale once the cooldown has expired.

## Required Railway setup

- Set `DATABASE_URL` on **V3-buyer** as a Railway reference to the existing Postgres service's `DATABASE_URL`. The code automatically creates `v3_trailing_watchlist` in the referenced database.
- Existing variables: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SPREADSHEET_ID`, `GOOGLE_SHEET_TAB`, `BUY_FRACTION`, `ALPACA_PAPER`, `DRY_RUN`, `LIVE_TRADING_ACK`.
- Optional: `ALPACA_DATA_FEED=iex` (or `sip` if entitled), `REENTRY_COOLDOWN_HOURS=24`.
- Start with `DRY_RUN=true` and ideally `ALPACA_PAPER=true`. Do not enable live until testing and approval.
- Replace the buyer's requirements with the supplied `requirements.txt` **only after confirming there are no other dependencies**.

## Known design limitations / review points

- **Screener freshness is not cryptographically or transactionally verified.** The source sheet currently has no successful-refresh timestamp for the buyer to validate. Add a screener completion marker and fail-closed check before production.
- Snapshot trades sampled every 10 minutes can miss intraperiod lows or rebounds. This is the *lowest observed price*, not the actual session low.
- This design intentionally retains the original margin-capable `buying_power` basis. It does **not** eliminate margin risk. If desired, change sizing to `cash` after separate approval.
- Existing seller bot's exit rules are unchanged. The 24-hour post-sale cooldown mitigates, but does not eliminate, repeated losses.
- When `DRY_RUN=true`, watchlist lows still evolve and triggered symbols stay watching. Test with a dedicated paper database before live deployment.
- Confirm the trading account's snapshot data subscription, rate limits, and order API behavior under paper trading.
- Existing V3 service has no `DATABASE_URL` or persistent volume configured as of inspection; deployment requires wiring the Postgres reference.

## Validation

`python -m py_compile main.py` checks syntax. Add integration tests against a paper Alpaca account and a staging Postgres database before deployment. Do not run this simultaneously with the original live V3 buyer against the same account without planning for duplicate protection.
