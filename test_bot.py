import copy
import unittest
from unittest.mock import patch
from decimal import Decimal
import bot


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.date = "2026-10-11T12:00:00+00:00"
        self.now = bot.timestamp(self.date) + 1
        self.config = {
            "budget_rub": 5000, "reserve_rub": 1000, "max_lot_rub": 1000,
            "min_scenario_profit_rub": 100, "min_roi_fraction": 0.15,
            "rub_per_ton": 100, "rub_rate_checked_at": self.date,
            "costs": {"tonnel": {"checked_at": self.date, "sell_fee_fraction": 0.02,
                                  "extra_buy_rub": 0, "transfer_and_withdraw_rub": 50}}}
        self.rule = {"collection": "Demo", "sale_scenario_ton": 10, "checked_at": self.date}
        self.item = {"provider": "tonnel", "price": 7, "on_sale": True, "premarket": False,
                     "transferable_at": self.date, "resale_available_at": self.date}

    def evaluate(self):
        return bot.evaluate(self.item, self.rule, self.config, self.now)

    def test_costs_and_breakeven(self):
        result = self.evaluate()
        self.assertEqual(result["profit"], Decimal("230"))
        self.assertAlmostEqual(float(result["breakeven_ton"]), 7.653061224)

    def test_costs_can_remove_apparent_profit(self):
        self.config["costs"]["tonnel"]["transfer_and_withdraw_rub"] = 290
        self.assertIsNone(self.evaluate())

    def test_unknown_or_future_lock_is_rejected(self):
        for value in (None, "2026-10-12T12:00:00Z"):
            self.item["transferable_at"] = value
            self.assertIsNone(self.evaluate())

    def test_old_rate_is_rejected(self):
        self.now += 86400
        self.assertIsNone(self.evaluate())

    def test_budget_includes_expenses(self):
        self.config["max_lot_rub"] = 740
        self.assertIsNone(self.evaluate())

    def test_unknown_fee_is_not_zero(self):
        self.config["costs"]["tonnel"]["sell_fee_fraction"] = None
        with self.assertRaises(ValueError):
            self.evaluate()

    def test_partial_source_is_error(self):
        with patch.object(bot, "request_json", return_value={"items": [], "meta": {"partial": True}}):
            with self.assertRaises(RuntimeError):
                bot.fetch(self.rule, "test-key", 2)

    def test_pagination_and_dedup(self):
        item = dict(self.item, giftId="one")
        pages = [{"items": [item], "has_more": True},
                 {"items": [item, dict(item, giftId="two")], "has_more": False}]
        with patch.object(bot, "request_json", side_effect=pages), patch.object(bot.time, "sleep"):
            rows, complete = bot.fetch(self.rule, "test-key", 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(complete)

    def test_demo_cannot_send_or_fetch(self):
        with patch.object(bot, "request_json", side_effect=AssertionError("network forbidden")), patch("builtins.print"):
            bot.demo()

    def test_telegram_enables_large_gift_preview(self):
        preview = "https://t.me/nft/PlushPepe-1098"
        offer = "https://t.me/portals/market?startapp=gift_test"
        with patch.object(bot, "request_json", return_value={"ok": True}) as request:
            bot.telegram(
                {"bot_token": "1:test", "chat_id": "42"},
                "offer",
                preview_url=preview,
                button_url=offer,
            )
        payload = request.call_args.args[1]
        self.assertEqual(payload["link_preview_options"], {
            "is_disabled": False,
            "url": preview,
            "prefer_large_media": True,
            "show_above_text": True,
        })
        self.assertEqual(payload["reply_markup"], {
            "inline_keyboard": [[{"text": "Открыть оффер", "url": offer}]]
        })


if __name__ == "__main__":
    unittest.main()
