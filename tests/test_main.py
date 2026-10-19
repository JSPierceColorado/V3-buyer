import os
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


class FakeResponse:
    def __init__(self, data):
        self.data = data
    def raise_for_status(self):
        pass
    def json(self):
        return self.data


class FakeSheets:
    def __init__(self, values):
        self.values = values
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse({"values": self.values})


class FakeAlpaca:
    def __init__(self, held=(), pending=(), attempted=(), bought=(), assets=None, powers=None):
        self.held = set(held)
        self.pending = set(pending)
        self.attempted = set(attempted)
        self.bought = set(bought)
        self.assets = assets or {}
        self.powers = list(powers or ["10000"])
        self.calls = 0
        self.orders = []
        self.open = True
    def market_open(self):
        return self.open
    def positions(self):
        return set(self.held)
    def pending_symbols(self):
        return set(self.pending)
    def bought_today(self, symbol, trading_day):
        return symbol in self.bought
    def already_attempted(self, cid):
        return cid in self.attempted
    def fractionable_asset(self, symbol):
        return self.assets.get(symbol, True)
    def buying_power(self):
        p = self.powers[min(self.calls, len(self.powers)-1)]
        self.calls += 1
        return p
    def submit(self, symbol, amount, client_id):
        self.orders.append((symbol, amount, client_id))
        self.pending.add(symbol)
        self.attempted.add(client_id)
        self.bought.add(symbol)
        return {"id": f"order-{len(self.orders)}"}


CFG = main.Settings("key", "secret", "{}", main.DEFAULT_SHEET_ID,
                    "Screener", True, False, Decimal("0.02"))
DRY = main.Settings("key", "secret", "{}", main.DEFAULT_SHEET_ID,
                    "Screener", True, True, Decimal("0.02"))
HEADER = ["Symbol", "Price", "SMA200"]


def sheet(*rows):
    return FakeSheets([HEADER, *rows])


class LogicTests(unittest.TestCase):
    def test_negative_cells_selected_no_positive_or_zero(self):
        rows = [HEADER, ["ABT", 100, 105, -5], ["AAPL", 101, 90, 11],
                ["WMT", 100, 100, 0], ["ACN", 70, 80, -10]]
        self.assertEqual(main.negative_dips(rows), [("ABT", Decimal("-5")), ("ACN", Decimal("-10"))])

    def test_blank_formulas_errors_and_missing_tickers_skipped(self):
        rows = [HEADER, ["", 2, 3, -1], ["GE", 2, 3, "#REF!"],
                ["MSFT", None, 30, -2], ["AAPL", 100, 110, -10],
                ["AAPL", 100, 110, -10], ["JPM", 100, 120, ""],
                ["BAD TICKER", 100, 120, -20]]
        self.assertEqual(main.negative_dips(rows), [("AAPL", Decimal("-10"))])

    def test_wrong_headers_stop(self):
        with self.assertRaises(ValueError):
            main.negative_dips([["Ticker", "Price", "SMA200"], ["MSFT", 1, 2, -1]])

    def test_size_is_exact_two_percent_rounded_down(self):
        self.assertEqual(main.order_dollars("50123.47", Decimal("0.02")), Decimal("1002.46"))
        self.assertEqual(main.order_dollars("100", Decimal("0.02")), Decimal("2.00"))
        self.assertEqual(main.order_dollars("0", Decimal("0.02")), Decimal("0"))

    def test_client_order_id_stable_and_daily(self):
        self.assertEqual(main.order_client_id("2026-10-19", "BRK.B"), "v3dip-20261019-BRK-B")
        self.assertNotEqual(main.order_client_id("2026-10-19", "AAPL"), main.order_client_id("2026-10-20", "AAPL"))

    def test_timezone_daylight_saving(self):
        before = main.next_cycle(datetime(2026, 10, 30, 13, tzinfo=ZoneInfo("America/Denver")))
        after = main.next_cycle(datetime(2026, 11, 2, 8, tzinfo=ZoneInfo("America/Denver")))
        self.assertEqual(before.isoformat(), "2026-11-02T12:30:00-07:00")
        self.assertEqual(after.isoformat(), "2026-11-02T12:30:00-07:00")
        july = main.next_cycle(datetime(2026, 7, 7, 8, tzinfo=ZoneInfo("America/Denver")))
        self.assertEqual(july.isoformat(), "2026-07-07T12:30:00-06:00")

    def test_next_cycle_skips_weekend(self):
        self.assertEqual(main.next_cycle(datetime(2026, 10, 18, 12, tzinfo=ZoneInfo("America/Denver"))).weekday(), 0)

    def test_read_sheet_is_get_and_no_sheet_writes(self):
        s = sheet(["ABT", 105, 110, -5])
        signals = main.read_v3(s, DRY)
        self.assertEqual(signals, [("ABT", Decimal("-5"))])
        self.assertEqual(len(s.calls), 1)
        url, kwargs = s.calls[0]
        self.assertIn("v4/spreadsheets/", url)
        self.assertEqual(kwargs["params"]["valueRenderOption"], "UNFORMATTED_VALUE")
        self.assertEqual(kwargs["timeout"], 30)

    def test_skips_held_open_and_attempted(self):
        a = FakeAlpaca(held=["AAPL"], pending=["ABT"], attempted=[main.order_client_id("2026-10-19", "ACN")])
        s = sheet(["AAPL", 100, 120, -20], ["ABT", 100, 120, -20],
                  ["ACN", 100, 120, -20], ["WMT", 100, 120, -20])
        report = main.buy_cycle(s, a, CFG, "2026-10-19")
        self.assertEqual([o[0] for o in a.orders], ["WMT"])
        self.assertEqual(len(report["skipped"]), 3)

    def test_skips_prior_buy_from_other_bot_same_day(self):
        a = FakeAlpaca(bought=["AAPL"])
        main.buy_cycle(sheet(["AAPL", 100, 120, -20], ["ABT", 100, 120, -20]), a, CFG, "2026-10-19")
        self.assertEqual([o[0] for o in a.orders], ["ABT"])

    def test_rechecks_buying_power_for_each_order(self):
        a = FakeAlpaca(powers=["10000", "9000"])
        s = sheet(["ABT", 100, 120, -20], ["WMT", 100, 120, -20])
        main.buy_cycle(s, a, CFG, "2026-10-19")
        self.assertEqual([o[1] for o in a.orders], [Decimal("200.00"), Decimal("180.00")])

    def test_market_closed_skips(self):
        a = FakeAlpaca()
        a.open = False
        self.assertEqual(main.buy_cycle(sheet(["ABT", 1, 2, -1]), a, CFG, "2026-10-19"), {"status": "market_closed"})
        self.assertFalse(a.orders)

    def test_dry_run_never_submits(self):
        a = FakeAlpaca()
        result = main.buy_cycle(sheet(["ABT", 1, 2, -1]), a, DRY, "2026-10-19")
        self.assertEqual(result["simulated"], [("ABT", "200.00")])
        self.assertFalse(a.orders)

    def test_invalid_asset_is_skipped(self):
        a = FakeAlpaca(assets={"ABT": False})
        main.buy_cycle(sheet(["ABT", 1, 2, -1]), a, CFG, "2026-10-19")
        self.assertFalse(a.orders)

    def test_invalid_buy_fraction_rejected(self):
        env = {"APCA_API_KEY_ID": "x", "APCA_API_SECRET_KEY": "y", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}", "BUY_FRACTION": "0.5"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                main.load_settings()

    def test_live_trade_requires_explicit_ack(self):
        env = {"APCA_API_KEY_ID": "x", "APCA_API_SECRET_KEY": "y", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}", "ALPACA_PAPER": "false", "DRY_RUN": "false"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                main.load_settings()

    def test_stop_after_ambiguous_order_error(self):
        a = FakeAlpaca()
        def fail(*args, **kwargs):
            raise RuntimeError("uncertain")
        a.submit = fail
        report = main.buy_cycle(sheet(["ABT", 1, 2, -1], ["WMT", 1, 2, -1]), a, CFG, "2026-10-19")
        self.assertEqual(report["submitted"], [])
        self.assertEqual(a.calls, 1)


if __name__ == "__main__":
    unittest.main()
