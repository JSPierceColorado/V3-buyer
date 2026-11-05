"""V3.1: Google Sheet dip discovery + persistent 5% trailing-rebound entry.

Safe staging release: DRY_RUN defaults true. Requires PostgreSQL DATABASE_URL.
Discovery at/after 12:30 America/Denver once per trading day; monitoring every
10 minutes while Alpaca says the market is open. Existing V3 screener unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from urllib.parse import quote
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

LOG = logging.getLogger("v3-trailing-entry")
DENVER = ZoneInfo("America/Denver")
NEW_YORK = ZoneInfo("America/New_York")
D = Decimal
TRIGGER = D("1.05")
INTERVAL_SECONDS = 600


def flag(name, default):
    value = os.getenv(name, str(default)).strip().lower()
    if value not in ("true", "false", "1", "0"):
        raise ValueError(f"{name} must be boolean")
    return value in ("true", "1")


def settings():
    cfg = {
        "key": os.environ["APCA_API_KEY_ID"],
        "secret": os.environ["APCA_API_SECRET_KEY"],
        "service_json": os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"],
        "sheet_id": os.environ["GOOGLE_SPREADSHEET_ID"],
        "tab": os.getenv("GOOGLE_SHEET_TAB", "Screener"),
        "database_url": os.environ["DATABASE_URL"],
        "paper": flag("ALPACA_PAPER", True),
        "dry_run": flag("DRY_RUN", True),
        "fraction": D(os.getenv("BUY_FRACTION", "0.02")),
        "feed": os.getenv("ALPACA_DATA_FEED", "iex").lower(),
        "cooldown_hours": int(os.getenv("REENTRY_COOLDOWN_HOURS", "24")),
    }
    if not cfg["fraction"].is_finite() or not D("0") < cfg["fraction"] <= D("0.02"):
        raise ValueError("BUY_FRACTION must be in (0, 0.02]")
    if cfg["feed"] not in ("iex", "sip"):
        raise ValueError("ALPACA_DATA_FEED must be iex or sip")
    if cfg["cooldown_hours"] < 0:
        raise ValueError("REENTRY_COOLDOWN_HOURS must be nonnegative")
    if not cfg["paper"] and not cfg["dry_run"] and os.getenv("LIVE_TRADING_ACK") != "I_ACCEPT_LIVE_MARGIN_RISK":
        raise ValueError("Live orders require LIVE_TRADING_ACK=I_ACCEPT_LIVE_MARGIN_RISK")
    return cfg


def sym(value):
    return str(value or "").strip().upper().replace("/", ".")


def number(value):
    try:
        d = D(str(value))
        return d if d.is_finite() else None
    except (ValueError, TypeError, ArithmeticError):
        return None


class Alpaca:
    def __init__(self, cfg):
        self.http = requests.Session()
        self.http.headers.update({"APCA-API-KEY-ID": cfg["key"], "APCA-API-SECRET-KEY": cfg["secret"]})
        self.trading = "https://paper-api.alpaca.markets" if cfg["paper"] else "https://api.alpaca.markets"
        self.feed = cfg["feed"]

    def call(self, method, path, *, data=False, params=None, payload=None, missing_ok=False):
        base = "https://data.alpaca.markets" if data else self.trading
        r = self.http.request(method, base + path, params=params, json=payload, timeout=(5, 30))
        if missing_ok and r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def is_open(self):
        return self.call("GET", "/v2/clock").get("is_open") is True

    def account(self):
        a = self.call("GET", "/v2/account")
        if any(a.get(k) for k in ("account_blocked", "trading_blocked", "trade_suspended_by_user")):
            raise RuntimeError("Account trading blocked")
        return a

    def positions(self):
        return {sym(p["symbol"]) for p in self.call("GET", "/v2/positions")}

    def open_orders(self):
        o = self.call("GET", "/v2/orders", params={"status": "open", "limit": 500})
        if len(o) >= 500:
            raise RuntimeError("Open orders pagination safety limit reached")
        return {sym(x["symbol"]) for x in o}

    def recent_orders(self, symbol, since):
        orders = self.call("GET", "/v2/orders", params={
            "status": "all", "symbols": symbol, "after": since.isoformat(),
            "limit": 500, "direction": "desc"})
        if len(orders) >= 500:
            raise RuntimeError("Recent order pagination safety limit reached")
        return [x for x in orders if sym(x.get("symbol")) == symbol]

    def by_client_id(self, cid):
        return self.call("GET", "/v2/orders:by_client_order_id",
                         params={"client_order_id": cid}, missing_ok=True)

    def fractionable(self, symbol):
        a = self.call("GET", "/v2/assets/" + quote(symbol, safe=""), missing_ok=True)
        return bool(a and a.get("tradable") and a.get("fractionable") and a.get("status") == "active")

    def latest_prices(self, symbols):
        """Latest regular-session trades only; do not use stale quotes or historical bars."""
        now = datetime.now(timezone.utc)
        prices = {}
        for i in range(0, len(symbols), 40):
            batch = symbols[i:i+40]
            snapshots = self.call("GET", "/v2/stocks/snapshots", data=True,
                                  params={"symbols": ",".join(batch), "feed": self.feed})
            for symbol, snap in snapshots.items():
                if symbol not in batch or not isinstance(snap, dict):
                    continue
                trade = snap.get("latestTrade") or {}
                try:
                    stamp = datetime.fromisoformat(trade["t"].replace("Z", "+00:00"))
                    price = number(trade["p"])
                except (KeyError, ValueError, TypeError):
                    continue
                age = (now - stamp).total_seconds()
                et = stamp.astimezone(NEW_YORK)
                if (price is not None and price > 0 and 0 <= age <= 900
                    and et.date() == now.astimezone(NEW_YORK).date()
                    and (9, 30) <= (et.hour, et.minute) < (16, 0)):
                    prices[symbol] = price
        return prices

    def submit(self, symbol, amount, cid):
        return self.call("POST", "/v2/orders", payload={
            "symbol": symbol, "notional": str(amount), "side": "buy",
            "type": "market", "time_in_force": "day", "client_order_id": cid})


def sheet_signals(session, cfg):
    tab = cfg["tab"].replace("'", "''")
    path = quote(f"'{tab}'!A:D", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{cfg['sheet_id']}/values/{path}"
    r = session.get(url, params={"valueRenderOption": "UNFORMATTED_VALUE"}, timeout=30)
    r.raise_for_status()
    rows = r.json().get("values", [])
    if not rows or [str(x).strip().lower() for x in rows[0][:3]] != ["symbol", "price", "sma200"]:
        raise RuntimeError("Unexpected V3 Sheet header; refusing discovery")
    result = set()
    for row in rows[1:]:
        if len(row) < 4:
            continue
        s = sym(row[0]); price = number(row[1]); sma = number(row[2]); delta = number(row[3])
        if (re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", s) and price is not None
            and sma is not None and delta is not None and price > 0 and sma > 0 and delta < 0):
            result.add(s)
    return result


def connect(url):
    conn = psycopg2.connect(url, connect_timeout=10, sslmode="prefer")
    conn.autocommit = False
    return conn


def init_db(conn):
    with conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS v3_trailing_watchlist (
            symbol TEXT PRIMARY KEY,
            first_seen TIMESTAMPTZ NOT NULL,
            low_price NUMERIC(22,8) NOT NULL CHECK (low_price > 0),
            last_price NUMERIC(22,8) NOT NULL CHECK (last_price > 0),
            last_seen TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL DEFAULT 'watching' CHECK (status IN ('watching','pending','blocked')),
            client_order_id TEXT UNIQUE,
            alpaca_order_id TEXT,
            note TEXT
        )""")
    LOG.info("Persistent watchlist ready")


def list_watch(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM v3_trailing_watchlist ORDER BY first_seen, symbol")
        return list(cur.fetchall())


def update_low(conn, symbol, price):
    with conn, conn.cursor() as cur:
        cur.execute("""UPDATE v3_trailing_watchlist SET low_price=LEAST(low_price,%s),
            last_price=%s,last_seen=now() WHERE symbol=%s AND status='watching'
            RETURNING low_price""", (price, price, symbol))
        row = cur.fetchone()
        return row[0] if row else None


def set_status(conn, symbol, status, *, cid=None, oid=None, note=None):
    with conn, conn.cursor() as cur:
        cur.execute("""UPDATE v3_trailing_watchlist SET status=%s,
            client_order_id=COALESCE(%s,client_order_id),
            alpaca_order_id=COALESCE(%s,alpaca_order_id),note=%s WHERE symbol=%s""",
            (status, cid, oid, note, symbol))


def remove(conn, symbol):
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM v3_trailing_watchlist WHERE symbol=%s", (symbol,))


def discover(conn, alpaca, session, cfg):
    signals = sheet_signals(session, cfg)
    held = alpaca.positions(); pending = alpaca.open_orders()
    existing = {r["symbol"] for r in list_watch(conn)}
    new = sorted(signals - held - pending - existing)
    prices = alpaca.latest_prices(new)
    now = datetime.now(timezone.utc)
    count = 0
    for symbol in new:
        if symbol not in prices:
            continue
        # A recently sold stock is not immediately eligible to re-enter the queue.
        orders = alpaca.recent_orders(symbol, now - timedelta(hours=max(24, cfg["cooldown_hours"])))
        if any(o.get("side") == "sell" and o.get("status") in ("filled", "partially_filled")
               and datetime.fromisoformat(o["created_at"].replace("Z", "+00:00")) >=
               now - timedelta(hours=cfg["cooldown_hours"]) for o in orders):
            continue
        # Avoid re-adding symbols bought earlier today by any buyer.
        start = datetime.combine(now.astimezone(DENVER).date(), datetime.min.time(), tzinfo=DENVER)
        if any(o.get("side") == "buy" and
               datetime.fromisoformat(o["created_at"].replace("Z", "+00:00")) >= start for o in orders):
            continue
        with conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO v3_trailing_watchlist
                (symbol,first_seen,low_price,last_price,last_seen)
                VALUES (%s,now(),%s,%s,now()) ON CONFLICT (symbol) DO NOTHING""",
                (symbol, prices[symbol], prices[symbol]))
            count += cur.rowcount
    LOG.info("Discovery: %d signals; %d new watched; %d total", len(signals), count, len(list_watch(conn)))


def reconcile(conn, alpaca, cfg):
    for row in list_watch(conn):
        if row["status"] != "pending":
            continue
        cid = row["client_order_id"]
        try:
            order = alpaca.by_client_id(cid)
        except Exception:
            LOG.exception("Order lookup failed for %s; preserving pending", row["symbol"])
            continue
        if order is None:
            # Never automatically retry an uncertain submission.
            LOG.warning("Unresolved order %s for %s; manual review needed", cid, row["symbol"])
        elif order.get("status") == "filled":
            LOG.info("Confirmed fill %s; removing from watchlist", row["symbol"])
            remove(conn, row["symbol"])
        elif order.get("status") in ("canceled", "expired", "rejected"):
            set_status(conn, row["symbol"], "blocked", note="Order not filled; manual review")
        else:
            LOG.info("Order for %s remains %s", row["symbol"], order.get("status"))


def monitor(conn, alpaca, cfg):
    reconcile(conn, alpaca, cfg)
    watching = [r for r in list_watch(conn) if r["status"] == "watching"]
    if not watching:
        return
    prices = alpaca.latest_prices([r["symbol"] for r in watching])
    held = alpaca.positions(); pending = alpaca.open_orders()
    for row in watching:
        symbol = row["symbol"]
        price = prices.get(symbol)
        if price is None:
            LOG.warning("No fresh price for %s; skipping", symbol)
            continue
        # Update the low before evaluating trigger; no retroactive price-history claims.
        low = update_low(conn, symbol, price)
        if low is None:
            continue
        trigger = low * TRIGGER
        LOG.info("WATCH %s price=%s low=%s trigger=%s", symbol, price, low, trigger)
        if price < trigger:
            continue
        if symbol in held or symbol in pending:
            LOG.info("Trigger skipped for %s: already held or open order", symbol)
            continue
        now = datetime.now(timezone.utc)
        orders = alpaca.recent_orders(symbol, now - timedelta(hours=max(24, cfg["cooldown_hours"])))
        start = datetime.combine(now.astimezone(DENVER).date(), datetime.min.time(), tzinfo=DENVER)
        if any(o.get("side") == "buy" and datetime.fromisoformat(
                o["created_at"].replace("Z", "+00:00")) >= start for o in orders):
            LOG.info("Trigger skipped %s: already bought today", symbol)
            continue
        if any(o.get("side") == "sell" and o.get("status") in ("filled", "partially_filled")
               and datetime.fromisoformat(o["created_at"].replace("Z", "+00:00")) >=
               now - timedelta(hours=cfg["cooldown_hours"]) for o in orders):
            LOG.info("Trigger skipped %s: post-sale cooldown", symbol)
            continue
        if not alpaca.fractionable(symbol):
            LOG.warning("Trigger skipped %s: not fractionable/tradable", symbol)
            continue
        available = number(alpaca.account().get("buying_power"))
        amount = ((available or D("0")) * cfg["fraction"]).quantize(D("0.01"), rounding=ROUND_DOWN)
        if amount < D("1"):
            LOG.info("Insufficient buying power; no orders")
            break
        if cfg["dry_run"]:
            LOG.info("DRY RUN TRIGGER %s price=%s low=%s amount=%s", symbol, price, low, amount)
            continue
        # Persist the intent BEFORE sending the order. A timeout must never
        # result in automatic resubmission on the next 10-minute tick.
        cid = "v3trail-" + uuid.uuid4().hex[:24]
        set_status(conn, symbol, "pending", cid=cid, note="Intent persisted; reconcile Alpaca")
        try:
            order = alpaca.submit(symbol, amount, cid)
            set_status(conn, symbol, "pending", oid=order.get("id"), note="Submitted; awaiting fill")
            LOG.info("SUBMITTED %s amount=%s cid=%s", symbol, amount, cid)
        except Exception:
            LOG.exception("Uncertain submission for %s; pending retained, NO automatic retry", symbol)


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = settings()
    creds = service_account.Credentials.from_service_account_info(
        json.loads(cfg["service_json"]), scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    sheets = AuthorizedSession(creds)
    alpaca = Alpaca(cfg)
    conn = connect(cfg["database_url"])
    init_db(conn)
    discovered_date = None
    LOG.info("V3.1 online; 10-minute monitoring; paper=%s dry_run=%s", cfg["paper"], cfg["dry_run"])
    while True:
        try:
            local = datetime.now(DENVER)
            if local.weekday() < 5 and alpaca.is_open():
                # Discovery after the morning screener, once per day.
                if (local.hour, local.minute) >= (12, 30) and discovered_date != local.date():
                    discover(conn, alpaca, sheets, cfg)
                    discovered_date = local.date()
                monitor(conn, alpaca, cfg)
        except (psycopg2.Error, requests.RequestException, Exception):
            LOG.exception("Cycle failed; no automatic order retry")
            try:
                conn.rollback()
                if conn.closed:
                    conn = connect(cfg["database_url"])
            except Exception:
                LOG.exception("Database reconnect failed")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
