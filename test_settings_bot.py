import sqlite3
import unittest
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
            settings_bot.apply_command("/maxprice@mybot 2750", self.config, self.db),
            "settings",
        )
        self.assertEqual(
            settings_bot.apply_command("/minfloor 1250", self.config, self.db),
            "settings",
        )
        current = effective_config(self.config, self.db)
        self.assertEqual(current["max_lot_rub"], 2750)
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
        self.assertEqual(settings_bot.normalize_price_text("2 750 ₽"), "2750")
        with self.assertRaisesRegex(ValueError, "целым числом"):
            settings_bot.normalize_price_text("две тысячи")

    def test_next_plain_message_saves_requested_price(self):
        settings_bot.begin_input(self.db, "42", "max_lot_rub")
        update = {"message": {"chat": {"id": 42}, "text": "2 750 ₽"}}
        with patch.object(settings_bot, "show_settings") as show:
            settings_bot.handle_update(
                update, {"chat_id": 42, "bot_token": "token"}, self.config, self.db
            )
        self.assertEqual(effective_config(self.config, self.db)["max_lot_rub"], 2750)
        self.assertIsNone(settings_bot.pending_input(self.db, "42"))
        show.assert_called_once()


if __name__ == "__main__":
    unittest.main()
