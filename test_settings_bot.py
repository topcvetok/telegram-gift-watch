import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import patch

import settings_bot
from runtime_settings import effective_config


class SettingsBotTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.config = {
            "budget_rub": 5000,
            "reserve_rub": 1000,
            "max_lot_rub": 2000,
            "min_floor_rub": 1000,
        }

    def tearDown(self):
        self.db.close()

    def test_exact_commands_update_both_thresholds(self):
        self.assertEqual(
            settings_bot.apply_command("/maxprice@mybot 25000", self.config, self.db),
            "settings",
        )
        self.assertEqual(
            settings_bot.apply_command("/minfloor 1250", self.config, self.db),
            "settings",
        )
        current = effective_config(self.config, self.db)
        self.assertEqual(current["max_lot_rub"], 25000)
        self.assertEqual(current["min_floor_rub"], 1250)

    def test_buttons_start_free_price_input(self):
        self.assertEqual(
            settings_bot.apply_callback(
                "settings:input:max_lot_rub", self.config, self.db
            ),
            ("input", "max_lot_rub"),
        )
        settings_bot.begin_input(self.db, "42", "max_lot_rub")
        self.assertEqual(settings_bot.pending_input(self.db, "42"), "max_lot_rub")
        settings_bot.clear_input(self.db, "42")
        self.assertIsNone(settings_bot.pending_input(self.db, "42"))

    def test_settings_text_shows_live_values(self):
        settings_bot.apply_command("/minfloor 1800", self.config, self.db)
        text = settings_bot.settings_text(self.config, self.db)
        self.assertIn("2 000 ₽", text)
        self.assertIn("1 800 ₽", text)

    def test_keyboard_has_two_free_input_buttons(self):
        rows = settings_bot.settings_keyboard(self.config, self.db)["inline_keyboard"]
        self.assertTrue(all(len(row) == 1 for row in rows))
        labels = [row[0]["text"] for row in rows]
        self.assertIn("💰 Ввести максимальную стоимость", labels)
        self.assertIn("📊 Ввести минимальный floor", labels)
        self.assertNotIn("2 000 ₽", " ".join(labels))

    def test_price_text_accepts_spaces_and_ruble_sign(self):
        self.assertEqual(settings_bot.normalize_price_text("2 750 ₽"), ("2750", "rub"))
        with self.assertRaisesRegex(ValueError, "Введи цену"):
            settings_bot.normalize_price_text("две тысячи")

    def test_price_text_keeps_ton_without_conversion(self):
        self.assertEqual(settings_bot.normalize_price_text("12.5 TON"), ("12.5", "ton"))

    def test_command_accepts_ton_without_budget_cap(self):
        settings_bot.apply_command("/maxprice 25 TON", self.config, self.db)
        current = effective_config(self.config, self.db)
        self.assertEqual(current["max_lot_ton"], Decimal("25"))
        self.assertIn("25 TON", settings_bot.settings_text(self.config, self.db))

    def test_next_plain_message_saves_requested_price(self):
        settings_bot.begin_input(self.db, "42", "max_lot_rub")
        update = {"message": {"chat": {"id": 42}, "text": "25 000 ₽"}}
        with patch.object(settings_bot, "show_settings") as show:
            settings_bot.handle_update(
                update, {"chat_id": 42, "bot_token": "token"}, self.config, self.db
            )
        self.assertEqual(effective_config(self.config, self.db)["max_lot_rub"], 25000)
        self.assertIsNone(settings_bot.pending_input(self.db, "42"))
        show.assert_called_once()


if __name__ == "__main__":
    unittest.main()
