import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import patch

import settings_bot
from runtime_settings import effective_config, is_authorized, set_setting


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
        self.assertNotIn("GiftChangesAPI", text)

    def test_keyboard_has_price_and_market_buttons(self):
        rows = settings_bot.settings_keyboard(self.config, self.db)["inline_keyboard"]
        self.assertTrue(all(len(row) == 1 for row in rows))
        labels = [row[0]["text"] for row in rows]
        self.assertIn("💰 Ввести максимальную стоимость", labels)
        self.assertIn("📊 Ввести минимальный floor", labels)
        self.assertIn("✅ Tonnel", labels)
        self.assertIn("✅ Portals", labels)
        self.assertIn("✅ MRKT", labels)
        self.assertIn("✅ Floor коллекции", labels)
        self.assertIn("✅ Floor модели", labels)
        self.assertIn("✅ Цвет фона", labels)
        self.assertIn("✅ Красивый номер", labels)
        self.assertIn("🔢 Разных цифр: 3", labels)
        self.assertIn("✅ Монохром", labels)
        self.assertIn("🎨 Совпадение: от 80%", labels)
        self.assertIn("📈 Монохром до +15% к floor", labels)
        self.assertIn("✅ Улучшения Black / Onyx", labels)
        self.assertNotIn("2 000 ₽", " ".join(labels))

    def test_market_button_toggles_source(self):
        self.assertEqual(
            settings_bot.apply_callback("settings:toggle:portals", self.config, self.db),
            ("disabled", "portals"),
        )
        labels = [
            row[0]["text"]
            for row in settings_bot.settings_keyboard(self.config, self.db)["inline_keyboard"]
        ]
        self.assertIn("❌ Portals", labels)
        self.assertEqual(
            settings_bot.apply_callback("settings:toggle:portals", self.config, self.db),
            ("enabled", "portals"),
        )

    def test_special_basis_buttons_toggle_and_accept_free_values(self):
        self.assertEqual(
            settings_bot.apply_callback(
                "settings:feature:model_basis", self.config, self.db
            ),
            ("feature_disabled", "model_basis"),
        )
        self.assertEqual(
            settings_bot.apply_callback(
                "settings:feature:monochrome", self.config, self.db
            ),
            ("feature_disabled", "monochrome"),
        )
        self.assertEqual(
            settings_bot.apply_callback(
                "settings:number:monochrome_min_score", self.config, self.db
            ),
            ("input", "monochrome_min_score"),
        )
        self.assertEqual(
            settings_bot.apply_callback(
                "settings:number:monochrome_max_floor_premium_percent",
                self.config,
                self.db,
            ),
            ("input", "monochrome_max_floor_premium_percent"),
        )
        settings_bot.begin_input(self.db, "42", "monochrome_min_score")
        update = {"message": {"chat": {"id": 42}, "text": "85%"}}
        with patch.object(settings_bot, "show_settings") as show:
            settings_bot.handle_update(
                update, {"chat_id": 42, "bot_token": "token"}, self.config, self.db
            )
        self.assertEqual(
            effective_config(self.config, self.db, "42")["monochrome_min_score"],
            85,
        )
        show.assert_called_once()

    def test_dark_upgrade_notifications_can_be_disabled(self):
        self.assertEqual(
            settings_bot.apply_callback(
                "settings:feature:dark_upgrades", self.config, self.db
            ),
            ("feature_disabled", "dark_upgrades"),
        )
        self.assertFalse(
            effective_config(self.config, self.db)["dark_upgrades_enabled"]
        )

    def test_mini_app_payload_updates_only_submitted_settings_and_filters(self):
        payload = {
            "type": "save_settings",
            "settings": {
                "sources": {"portals": False},
                "numbers": {"daily_report_hour": 19},
                "filters": {
                    "collections": {"allow": ["Plush Pepe"], "deny": []},
                    "models": {"allow": [], "deny": ["Pumpkin"]},
                    "backdrops": {"allow": [], "deny": []},
                },
            },
        }
        settings_bot.apply_web_app_payload(
            __import__("json").dumps(payload), self.db, "42"
        )
        current = effective_config(self.config, self.db, "42")
        self.assertEqual(current["max_lot_rub"], 2000)
        self.assertFalse(current["portals_enabled"])
        self.assertEqual(current["daily_report_hour"], 19)
        self.assertEqual(
            current["filter_lists"]["collections"]["allow"], ["Plush Pepe"]
        )

    def test_keyboard_opens_configured_mini_app(self):
        config = dict(self.config, mini_app_url="https://example.com/app")
        rows = settings_bot.settings_keyboard(config, self.db)["inline_keyboard"]
        self.assertEqual(rows[0][0]["callback_data"], "settings:app")
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.send_app_launcher(
                {"chat_id": "42", "bot_token": "token"}, config
            )
        button = telegram.call_args.args[2]["reply_markup"]["keyboard"][0][0]
        self.assertEqual(button["web_app"]["url"], "https://example.com/app")

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
        settings_bot.begin_input(self.db, "42", "max_lot_rub", 77)
        update = {
            "message": {
                "chat": {"id": 42}, "message_id": 88, "text": "25 000 ₽"
            }
        }
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.handle_update(
                update, {"chat_id": 42, "bot_token": "token"}, self.config, self.db
            )
        self.assertEqual(
            effective_config(self.config, self.db, "42")["max_lot_rub"],
            25000,
        )
        self.assertIsNone(settings_bot.pending_input(self.db, "42"))
        methods = [call.args[1] for call in telegram.call_args_list]
        self.assertEqual(methods, ["deleteMessage", "editMessageText"])
        self.assertEqual(telegram.call_args_list[0].args[2]["message_id"], 88)
        self.assertEqual(telegram.call_args_list[1].args[2]["message_id"], 77)

    def test_callback_updates_the_same_settings_message(self):
        update = {
            "callback_query": {
                "id": "callback-1",
                "data": "settings:toggle:portals",
                "message": {"chat": {"id": 42}, "message_id": 77},
            }
        }
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.handle_update(
                update, {"chat_id": 42, "bot_token": "token"}, self.config, self.db
            )
        methods = [call.args[1] for call in telegram.call_args_list]
        self.assertEqual(methods, ["answerCallbackQuery", "editMessageText"])
        self.assertEqual(telegram.call_args_list[1].args[2]["message_id"], 77)

    def test_input_prompt_reuses_settings_message_and_remembers_it(self):
        update = {
            "callback_query": {
                "id": "callback-2",
                "data": "settings:input:max_lot_rub",
                "message": {"chat": {"id": 42}, "message_id": 77},
            }
        }
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.handle_update(
                update, {"chat_id": 42, "bot_token": "token"}, self.config, self.db
            )
        methods = [call.args[1] for call in telegram.call_args_list]
        self.assertEqual(methods, ["answerCallbackQuery", "editMessageText"])
        self.assertEqual(settings_bot.pending_input_state(self.db, "42"), ("max_lot_rub", 77))
        self.assertEqual(telegram.call_args_list[1].args[2]["message_id"], 77)

    def test_existing_input_table_is_migrated_for_message_reuse(self):
        self.db.execute(
            "CREATE TABLE settings_input "
            "(chat_id TEXT PRIMARY KEY, setting_key TEXT NOT NULL)"
        )
        self.db.commit()
        settings_bot.begin_input(self.db, "42", "max_lot_rub", 77)
        self.assertEqual(
            settings_bot.pending_input_state(self.db, "42"),
            ("max_lot_rub", 77),
        )

    def test_settings_view_is_remembered_for_later_updates(self):
        secrets = {"chat_id": 42, "bot_token": "token"}
        with patch.object(
            settings_bot,
            "api",
            side_effect=[{"message_id": 77}, {"message_id": 77}],
        ) as telegram:
            settings_bot.show_settings(secrets, self.config, self.db)
            settings_bot.show_settings(secrets, self.config, self.db)
        self.assertEqual(telegram.call_args_list[0].args[1], "sendMessage")
        self.assertEqual(telegram.call_args_list[1].args[1], "editMessageText")
        self.assertEqual(telegram.call_args_list[1].args[2]["message_id"], 77)

    def test_join_code_authorizes_guest_with_independent_settings(self):
        set_setting(self.db, "max_lot_rub", 3000, chat_id="42")
        secrets = {
            "chat_id": 42,
            "bot_token": "token",
            "access_code": "family-code",
        }
        join = {
            "message": {
                "chat": {"id": 99},
                "message_id": 7,
                "text": "/join family-code",
            }
        }
        with patch.object(
            settings_bot, "api", side_effect=[True, {"message_id": 8}]
        ):
            settings_bot.handle_update(join, secrets, self.config, self.db)
        self.assertTrue(is_authorized(self.db, "99", "42"))
        self.assertEqual(
            effective_config(self.config, self.db, "99")["max_lot_rub"], 2000
        )

        change = {
            "message": {
                "chat": {"id": 99},
                "message_id": 9,
                "text": "/maxprice 9000",
            }
        }
        with patch.object(settings_bot, "api", return_value={"message_id": 8}):
            settings_bot.handle_update(change, secrets, self.config, self.db)
        self.assertEqual(
            effective_config(self.config, self.db, "99")["max_lot_rub"], 9000
        )
        self.assertEqual(
            effective_config(self.config, self.db, "42")["max_lot_rub"], 3000
        )

    def test_wrong_join_code_does_not_authorize_guest(self):
        update = {
            "message": {
                "chat": {"id": 99},
                "message_id": 7,
                "text": "/join wrong",
            }
        }
        secrets = {
            "chat_id": 42,
            "bot_token": "token",
            "access_code": "right",
        }
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.handle_update(update, secrets, self.config, self.db)
        self.assertFalse(is_authorized(self.db, "99", "42"))
        self.assertEqual(telegram.call_args.args[1], "sendMessage")


if __name__ == "__main__":
    unittest.main()
