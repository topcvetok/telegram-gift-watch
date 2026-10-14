import sqlite3
import unittest

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

    def test_buttons_update_setting(self):
        self.assertTrue(settings_bot.apply_callback(
            "setting:max_lot_rub:3000", self.config, self.db
        ))
        self.assertEqual(effective_config(self.config, self.db)["max_lot_rub"], 3000)

    def test_settings_text_shows_live_values(self):
        settings_bot.apply_command("/minfloor 1800", self.config, self.db)
        text = settings_bot.settings_text(self.config, self.db)
        self.assertIn("2000 ₽", text)
        self.assertIn("1800 ₽", text)


if __name__ == "__main__":
    unittest.main()
