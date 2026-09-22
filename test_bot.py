import tempfile
import unittest
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import bot


CONFIG = {
    "buy_min_krw": 40000,
    "buy_max_krw": 40500,
    "sell_min_krw": 50000,
    "cash_buffer_percent": Decimal(1),
    "live_trading": False,
}


class FakeAPI:
    def __init__(self, price, held):
        self.price = Decimal(price)
        self.held = Decimal(held)
        self.orders_sent = []

    def open_orders(self):
        return []

    def holding_quantity(self):
        return self.held

    def quote_krw(self):
        return self.price / 1400, Decimal(1400), self.price

    def regular_market_open(self):
        return True

    def buying_power_usd(self):
        return Decimal("100.00")

    def sellable_quantity(self):
        return Decimal("2.5")

    def order(self, *args):
        self.orders_sent.append(args)
        raise AssertionError("Dry run sent an order")


class BotTests(unittest.TestCase):
    def test_live_mode_is_blocked_until_krw_price_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "symbol": "NVDL", "buy_min_krw": 40000, "buy_max_krw": 40500,
                "sell_min_krw": 50000, "live_trading": True
            }), encoding="utf-8")
            with patch.object(bot, "CONFIG_PATH", path):
                with self.assertRaisesRegex(RuntimeError, "Live trading paused"):
                    bot.load_config()

    def test_closed_market_waits_without_reading_stale_price(self):
        class ClosedAPI(FakeAPI):
            def regular_market_open(self):
                return False

            def quote_krw(self):
                raise AssertionError("Should not request a quote outside the session")

        with tempfile.TemporaryDirectory() as directory, patch.object(bot, "STATE_PATH", Path(directory) / "state.json"):
            with patch.object(bot, "log"):
                bot.cycle(ClosedAPI(40000, 0), CONFIG, bot.load_state())

    def test_price_bands(self):
        expected = {
            39999: ("WAIT", "WAIT"),
            40000: ("BUY", "WAIT"),
            40500: ("BUY", "WAIT"),
            40501: ("WAIT", "WAIT"),
            50000: ("WAIT", "SELL"),
            55000: ("WAIT", "SELL"),
            55001: ("WAIT", "SELL"),
        }
        for price, (flat, held) in expected.items():
            self.assertEqual(bot.decision(price, False, CONFIG), flat)
            self.assertEqual(bot.decision(price, True, CONFIG), held)

    def test_all_cash_buy_and_all_holdings_sell_are_dry_run(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, "STATE_PATH", Path(directory) / "state.json"):
            for price, held, expected in [(40000, 0, "$99.00 USD"), (50000, 2.5, "2.500000 shares")]:
                api = FakeAPI(price, held)
                with patch.object(bot, "log") as log:
                    bot.cycle(api, CONFIG, bot.load_state())
                self.assertTrue(any(expected in str(call) for call in log.call_args_list))
                self.assertEqual(api.orders_sent, [])


if __name__ == "__main__":
    unittest.main()
