"""V3 S&P 100 buy-the-dip worker: 12:30 PM America/Denver, trading days.

Read-only Google Sheet signal: column D (Price - SMA200) < 0.
Alpaca orders use 2% of current buying_power, including any margin.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.parse import quote
from zoneinfo import ZoneInfo

DENVER = ZoneInfo("America/Denver")
DEFAULT_SHEET_ID = "1DEqMtRcCWBA_-HhXiQyUXkWoiC91q146_AkqS_hd4Gk"
LOG = logging.getLogger("v3-dip-buyer")


@dataclass(frozen=True)
class Settings:
    key: str
    secret: str
    service_account_json: str
    sheet_id: str
    tab: str
    paper: bool
    dry_run: bool
    buy_fraction: Decimal


def boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw not in ("true", "false", "1", "0"):
        raise ValueError(f"{name} must be true or false")
    return raw in ("true", "1")


def load_settings() -> Settings:
    cfg = Settings(
        key=os.getenv("APCA_API_KEY_ID", "").strip(),
        secret=os.getenv("APCA_API_SECRET_KEY", "").strip(),
        service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip(),
        sheet_id=os.getenv("GOOGLE_SPREADSHEET_ID", DEFAULT_SHEET_ID).strip(),
        tab=os.getenv("GOOGLE_SHEET_TAB", "Screener").strip(),
        paper=boolean("ALPACA_PAPER", True),
        dry_run=boolean("DRY_RUN", True),
        buy_fraction=Decimal(os.getenv("BUY_FRACTION", "0.02")),
    )
    if not all((cfg.key, cfg.secret, cfg.service_account_json, cfg.sheet_id, cfg.tab)):
        raise ValueError("Set APCA_API_KEY_ID, APCA_API_SECRET_KEY and GOOGLE_SERVICE_ACCOUNT_JSON")
    if not cfg.buy_fraction.is_finite() or not Decimal("0") < cfg.buy_fraction <= Decimal("0.02"):
        raise ValueError("BUY_FRACTION must be greater than zero and no more than 0.02")
    # Avoid accidental real-money orders caused by changing only one environment setting.
    if not cfg.paper and not cfg.dry_run and os.getenv("LIVE_TRADING_ACK") != "I_ACCEPT_LIVE_MARGIN_RISK":
        raise ValueError("Live trading also requires LIVE_TRADING_ACK=I_ACCEPT_LIVE_MARGIN_RISK")
    return cfg


def next_cycle(now: datetime) -> datetime:
    """Next weekday at 12:30 Denver time (local clock, DST-aware)."""
    local = now.astimezone(DENVER)
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        if day.weekday() > 4:
            continue
        due = datetime.combine(day, clock_time(12, 30), tzinfo=DENVER)
        if due > local:
            return due
    raise RuntimeError("No upcoming weekday")


def parse_decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ValueError, InvalidOperation, TypeError):
        return None


def ticker(value: object) -> str:
    return str(value or "").strip().upper().replace("/", ".")


def negative_dips(rows: list[list]) -> list[tuple[str, Decimal]]:
    """Preserve sheet row order; ignore headers, empty rows and invalid D results."""
    if not rows or len(rows[0]) < 3:
        raise ValueError("V3 Sheet missing expected header row")
    h = [str(c).strip().lower() for c in rows[0]]
    if h[:3] != ["symbol", "price", "sma200"]:
        raise ValueError("V3 columns must be A=Symbol, B=Price, C=SMA200")
    found: list[tuple[str, Decimal]] = []
    seen: set[str] = set()
    for row in rows[1:]:
        if len(row) < 4:
            continue
        symbol = ticker(row[0])
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", symbol) or symbol in seen:
            continue
        price, sma, delta = (parse_decimal(row[i]) for i in (1, 2, 3))
        if price is None or sma is None or delta is None or price <= 0 or sma <= 0:
            continue
        if delta < 0:
            seen.add(symbol)
            found.append((symbol, delta))
    return found


def order_dollars(buying_power: object, fraction: Decimal) -> Decimal:
    available = parse_decimal(buying_power)
    if available is None or available <= 0:
        return Decimal("0")
    return (available * fraction).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def order_client_id(day: str, symbol: str) -> str:
    """Stable per trading day, even if Railway restarts."""
    return f"v3dip-{day.replace('-', '')}-{symbol.replace('.', '-')}"[:48]


class Alpaca:
    def __init__(self, key: str, secret: str, paper: bool):
        import requests
        self.session = requests.Session()
        self.session.headers.update({
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        })
        self.base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"

    def call(self, method: str, path: str, *, params=None, payload=None, allow_404=False):
        response = self.session.request(
            method, self.base + path, params=params, json=payload, timeout=(5, 30)
        )
        if allow_404 and response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def market_open(self) -> bool:
        return self.call("GET", "/v2/clock")["is_open"] is True

    def positions(self) -> set[str]:
        return {ticker(p["symbol"]) for p in self.call("GET", "/v2/positions")}

    def pending_symbols(self) -> set[str]:
        orders = self.call("GET", "/v2/orders", params={"status": "open", "limit": 500})
        if len(orders) >= 500:
            raise RuntimeError("500+ open orders: cannot verify duplicate protection safely")
        return {ticker(o["symbol"]) for o in orders}

    def bought_today(self, symbol: str, trading_day: str) -> bool:
        # Account-wide check, including filled and canceled buy orders by other bots.
        start = datetime.combine(date.fromisoformat(trading_day), clock_time.min, tzinfo=DENVER)
        orders = self.call("GET", "/v2/orders", params={
            "status": "all", "symbols": symbol, "side": "buy",
            "after": start.isoformat(), "limit": 500,
        })
        if len(orders) >= 500:
            raise RuntimeError(f"500+ buy orders for {symbol} today; skipping safely")
        return any(ticker(o.get("symbol")) == symbol and o.get("side") == "buy" for o in orders)

    def already_attempted(self, client_id: str) -> bool:
        result = self.call("GET", "/v2/orders:by_client_order_id",
                           params={"client_order_id": client_id}, allow_404=True)
        return result is not None

    def fractionable_asset(self, symbol: str) -> bool:
        asset = self.call("GET", "/v2/assets/" + quote(symbol, safe=""), allow_404=True)
        return bool(asset and asset.get("tradable") and asset.get("fractionable")
                    and asset.get("status") == "active")

    def buying_power(self) -> str:
        account = self.call("GET", "/v2/account")
        if any(account.get(f) for f in ("account_blocked", "trading_blocked", "trade_suspended_by_user")):
            raise RuntimeError("Alpaca account cannot trade")
        return account["buying_power"]

    def submit(self, symbol: str, amount: Decimal, client_id: str) -> dict:
        return self.call("POST", "/v2/orders", payload={
            "symbol": symbol, "notional": str(amount), "side": "buy",
            "type": "market", "time_in_force": "day", "client_order_id": client_id,
        })


def read_v3(session, cfg: Settings) -> list[tuple[str, Decimal]]:
    # Google Sheets values endpoint returns the calculated numeric value of D.
    a1 = quote(f"'{cfg.tab.replace(chr(39), chr(39) * 2)}'!A:D", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{cfg.sheet_id}/values/{a1}"
    response = session.get(url, params={"valueRenderOption": "UNFORMATTED_VALUE"}, timeout=30)
    response.raise_for_status()
    data = response.json()
    return negative_dips(data.get("values", []))


def buy_cycle(session, alpaca: Alpaca, cfg: Settings, today: str) -> dict:
    if not alpaca.market_open():
        LOG.info("Market closed: no orders today")
        return {"status": "market_closed"}
    signals = read_v3(session, cfg)
    LOG.info("V3 candidates with D<0: %d", len(signals))
    results = {"candidates": len(signals), "submitted": [], "simulated": [], "skipped": []}

    for symbol, delta in signals:
        try:
            if not alpaca.market_open():
                LOG.warning("Market closed during evaluation; stopping")
                break
            # Recheck at order time because other Railway bots share this Alpaca account.
            if symbol in alpaca.positions():
                results["skipped"].append((symbol, "already held"))
                continue
            if symbol in alpaca.pending_symbols():
                results["skipped"].append((symbol, "existing open order"))
                continue
            if alpaca.bought_today(symbol, today):
                results["skipped"].append((symbol, "already bought today in account"))
                continue
            cid = order_client_id(today, symbol)
            if alpaca.already_attempted(cid):
                results["skipped"].append((symbol, "V3 order already attempted today"))
                continue
            if not alpaca.fractionable_asset(symbol):
                results["skipped"].append((symbol, "asset not fractionable/tradable"))
                continue
            amount = order_dollars(alpaca.buying_power(), cfg.buy_fraction)
            if amount < Decimal("1.00"):
                LOG.info("Buying power insufficient; stopping")
                break
            if cfg.dry_run:
                LOG.info("DRY RUN %s D=%s buy $%s", symbol, delta, amount)
                results["simulated"].append((symbol, str(amount)))
                continue
            order = alpaca.submit(symbol, amount, cid)
            LOG.info("BUY %s D=%s $%s order_id=%s", symbol, delta, amount, order.get("id"))
            results["submitted"].append((symbol, str(amount), order.get("id")))
        except Exception:
            # Do not automatically retry an order whose submission status is uncertain.
            LOG.exception("Stopped during %s; checking logs and Alpaca orders is required", symbol)
            break
    return results


def run() -> None:
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_settings()
    service_account_data = json.loads(cfg.service_account_json)
    creds = service_account.Credentials.from_service_account_info(
        service_account_data,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    sheets = AuthorizedSession(creds)
    alpaca = Alpaca(cfg.key, cfg.secret, cfg.paper)
    LOG.info("V3 buyer online: 12:30 America/Denver weekdays, paper=%s dry_run=%s", cfg.paper, cfg.dry_run)
    while True:
        due = next_cycle(datetime.now(DENVER))
        LOG.info("Next cycle: %s", due.isoformat())
        while True:
            remaining = (due - datetime.now(DENVER)).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 60))
        try:
            report = buy_cycle(sheets, alpaca, cfg, due.date().isoformat())
            LOG.info("Cycle complete: %s", report)
        except Exception:
            LOG.exception("Cycle failed; will wait until next scheduled trading weekday")


if __name__ == "__main__":
    run()
