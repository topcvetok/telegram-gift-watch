import sqlite3
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import daily_report
from runtime_settings import activate_settings


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        self.settings = sqlite3.connect(":memory:")
        self.state = sqlite3.connect(":memory:")
        activate_settings(self.settings, "owner")
        daily_report.ensure_tables(self.state)
        self.now = datetime(2026, 10, 20, 21, 15, tzinfo=ZoneInfo("Europe/Moscow")).timestamp()
        self.state.execute(
            "INSERT INTO signal_log VALUES "
            "('owner','Tonnel','1','Plush Pepe','Pumpkin','Black','4',"
            "'модель: Pumpkin','5',120,?,1,?)",
            (self.now - 100, self.now - 20),
        )
        self.state.execute(
            "CREATE TABLE market_activity "
            "(event_id TEXT PRIMARY KEY, market TEXT, gift_name TEXT, kind TEXT, occurred REAL)"
        )
        self.state.execute(
            "INSERT INTO market_activity VALUES ('sale-1','tonnel','Plush Pepe','sale',?)",
            (self.now - 50,),
        )
        self.state.commit()

    def tearDown(self):
        activate_settings(None)
        self.settings.close()
        self.state.close()

    def test_daily_report_is_sent_once_per_local_day(self):
        config = {
            "max_lot_rub": 2000,
            "min_floor_rub": 1000,
            "daily_report_enabled": True,
            "daily_report_hour": 21,
            "daily_report_timezone": "Europe/Moscow",
        }
        with patch.object(daily_report, "telegram", return_value=77) as send:
            self.assertEqual(daily_report.run_cycle(
                config, {"chat_id": "owner", "bot_token": "x"}, self.state,
                now=self.now,
            ), 1)
            self.assertEqual(daily_report.run_cycle(
                config, {"chat_id": "owner", "bot_token": "x"}, self.state,
                now=self.now,
            ), 0)
        text = send.call_args.args[1]
        self.assertIn("Подходящих офферов: <b>1</b>", text)
        self.assertIn("Plush Pepe", text)
        self.assertIn("Сделки: 1", text)


if __name__ == "__main__":
    unittest.main()
