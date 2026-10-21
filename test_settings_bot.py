import json
import sqlite3
import unittest
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
            "mini_app_url": "https://example.com/app",
        }
        self.secrets = {"chat_id": 42, "bot_token": "token"}

    def tearDown(self):
        self.db.close()

    def test_chat_commands_only_open_mini_app(self):
        for command in ("/settings", "/app", "/start", "/help"):
            self.assertEqual(
                settings_bot.apply_command(command, self.config, self.db, "42"),
                "app",
            )

        self.assertIsNone(
            settings_bot.apply_command(
                "/maxprice 9000", self.config, self.db, "42"
            )
        )
        self.assertIsNone(
            settings_bot.apply_command(
                "/minfloor 5000", self.config, self.db, "42"
            )
        )
        current = effective_config(self.config, self.db, "42")
        self.assertEqual(current["max_lot_rub"], 2000)
        self.assertEqual(current["min_floor_rub"], 1000)

    def test_settings_command_sends_only_mini_app_launcher(self):
        update = {
            "message": {"chat": {"id": 42}, "message_id": 7, "text": "/settings"}
        }
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.handle_update(update, self.secrets, self.config, self.db)

        self.assertEqual(
            [call.args[1] for call in telegram.call_args_list],
            ["deleteMessage", "sendMessage"],
        )
        payload = telegram.call_args_list[1].args[2]
        self.assertNotIn("inline_keyboard", payload["reply_markup"])
        button = payload["reply_markup"]["keyboard"][0][0]
        self.assertEqual(button["web_app"]["url"], "https://example.com/app")
        self.assertNotIn("Максимальная стоимость", payload["text"])

    def test_show_settings_is_a_mini_app_launcher_for_compatibility(self):
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.show_settings(self.secrets, self.config, self.db)
        self.assertEqual(telegram.call_args.args[1], "sendMessage")
        self.assertIn("keyboard", telegram.call_args.args[2]["reply_markup"])

    def test_mini_app_payload_updates_settings_without_chat_panel(self):
        payload = {
            "type": "save_settings",
            "settings": {
                "prices": {
                    "max_lot": {"value": "25", "currency": "ton"},
                    "min_floor": {"value": "7", "currency": "ton"},
                    "upgrade_min_floor": {"value": "10.5", "currency": "ton"},
                },
                "sources": {"portals": False},
                "features": {"valuable_upgrades": True},
                "upgrade_backdrops": ["Azure Blue", "Coral Red"],
                "numbers": {"daily_report_hour": 19},
                "filters": {
                    "collections": {"allow": ["Plush Pepe"], "deny": []},
                    "models": {"allow": [], "deny": ["Pumpkin"]},
                    "backdrops": {"allow": [], "deny": []},
                },
            },
        }
        update = {
            "message": {
                "chat": {"id": 42},
                "message_id": 8,
                "web_app_data": {"data": json.dumps(payload)},
            }
        }
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.handle_update(update, self.secrets, self.config, self.db)

        current = effective_config(self.config, self.db, "42")
        self.assertEqual(str(current["max_lot_ton"]), "25")
        self.assertEqual(str(current["min_floor_ton"]), "7")
        self.assertEqual(str(current["upgrade_min_floor_ton"]), "10.5")
        self.assertEqual(current["upgrade_backdrops"], ["Azure Blue", "Coral Red"])
        self.assertTrue(current["valuable_upgrades_enabled"])
        self.assertFalse(current["portals_enabled"])
        self.assertEqual(current["daily_report_hour"], 19)
        self.assertEqual(
            current["filter_lists"]["collections"]["allow"], ["Plush Pepe"]
        )
        self.assertEqual(
            [call.args[1] for call in telegram.call_args_list], ["deleteMessage"]
        )

    def test_old_inline_buttons_no_longer_change_settings(self):
        update = {
            "callback_query": {
                "id": "callback-1",
                "data": "settings:toggle:portals",
                "message": {"chat": {"id": 42}, "message_id": 77},
            }
        }
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.handle_update(update, self.secrets, self.config, self.db)

        self.assertTrue(effective_config(self.config, self.db, "42")["portals_enabled"])
        self.assertEqual(
            [call.args[1] for call in telegram.call_args_list],
            ["answerCallbackQuery", "sendMessage"],
        )
        self.assertEqual(
            telegram.call_args_list[0].args[2]["text"],
            "Настройки перенесены в Mini App",
        )

    def test_hide_callback_deletes_the_alert(self):
        update = {
            "callback_query": {
                "id": "callback-hide",
                "data": "hide:message",
                "message": {"chat": {"id": 42}, "message_id": 91},
            }
        }
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.handle_update(update, self.secrets, self.config, self.db)

        self.assertEqual(
            [call.args[1] for call in telegram.call_args_list],
            ["answerCallbackQuery", "deleteMessage"],
        )
        self.assertEqual(telegram.call_args_list[1].args[2]["message_id"], 91)

    def test_plain_chat_message_does_not_complete_old_input(self):
        settings_bot.ensure_input_table(self.db)
        self.db.execute(
            "INSERT INTO settings_input (chat_id, setting_key, message_id) "
            "VALUES (?, ?, ?)",
            ("42", "max_lot_rub", 77),
        )
        self.db.commit()
        update = {
            "message": {"chat": {"id": 42}, "message_id": 88, "text": "9000"}
        }
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.handle_update(update, self.secrets, self.config, self.db)
        self.assertEqual(effective_config(self.config, self.db, "42")["max_lot_rub"], 2000)
        telegram.assert_not_called()

    def test_legacy_settings_panels_and_pending_inputs_are_removed(self):
        settings_bot.remember_settings_message(self.db, "42", 77)
        settings_bot.remember_settings_message(self.db, "99", 88)
        settings_bot.ensure_input_table(self.db)
        self.db.execute(
            "INSERT INTO settings_input (chat_id, setting_key, message_id) "
            "VALUES (?, ?, ?)",
            ("42", "max_lot_rub", 77),
        )
        self.db.commit()
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.remove_legacy_settings_views(self.secrets, self.db)

        self.assertEqual(
            [call.args[2]["message_id"] for call in telegram.call_args_list],
            [77, 88],
        )
        self.assertIsNone(settings_bot.remembered_settings_message(self.db, "42"))
        self.assertIsNone(
            self.db.execute(
                "SELECT setting_key FROM settings_input WHERE chat_id=?", ("42",)
            ).fetchone()
        )

    def test_run_removes_all_commands_from_bot_menu(self):
        with (
            patch.object(settings_bot, "api") as telegram,
            patch.object(settings_bot.time, "monotonic", side_effect=[0, 2]),
            patch.object(settings_bot, "remove_legacy_settings_views") as cleanup,
        ):
            settings_bot.run(self.config, self.secrets, self.db, duration=1)

        self.assertEqual(telegram.call_args_list[0].args[1], "deleteMyCommands")
        self.assertEqual(telegram.call_args_list[1].args[1], "setChatMenuButton")
        self.assertEqual(
            telegram.call_args_list[1].args[2]["menu_button"], {"type": "default"}
        )
        cleanup.assert_called_once_with(self.secrets, self.db)

    def test_join_code_authorizes_guest_and_opens_mini_app(self):
        set_setting(self.db, "max_lot_rub", 3000, chat_id="42")
        secrets = dict(self.secrets, access_code="family-code")
        join = {
            "message": {
                "chat": {"id": 99},
                "message_id": 7,
                "text": "/join family-code",
            }
        }
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.handle_update(join, secrets, self.config, self.db)

        self.assertTrue(is_authorized(self.db, "99", "42"))
        self.assertEqual(
            [call.args[1] for call in telegram.call_args_list],
            ["deleteMessage", "sendMessage"],
        )
        self.assertEqual(
            effective_config(self.config, self.db, "99")["max_lot_rub"], 2000
        )

    def test_wrong_join_code_does_not_authorize_guest(self):
        secrets = dict(self.secrets, access_code="right")
        update = {
            "message": {
                "chat": {"id": 99},
                "message_id": 7,
                "text": "/join wrong",
            }
        }
        with patch.object(settings_bot, "api") as telegram:
            settings_bot.handle_update(update, secrets, self.config, self.db)
        self.assertFalse(is_authorized(self.db, "99", "42"))
        self.assertEqual(telegram.call_args.args[1], "sendMessage")


if __name__ == "__main__":
    unittest.main()
