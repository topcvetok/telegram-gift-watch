import sqlite3
import tempfile
import unittest
from pathlib import Path

from runtime_settings import effective_config, max_purchase_limit, set_setting


class RuntimeSettingsTests(unittest.TestCase):
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

    def test_overrides_are_persistent_and_leave_source_config_unchanged(self):
        set_setting(self.db, "max_lot_rub", 3000, maximum=4000)
        set_setting(self.db, "min_floor_rub", 1500)
        current = effective_config(self.config, self.db)
        self.assertEqual(current["max_lot_rub"], 3000)
        self.assertEqual(current["min_floor_rub"], 1500)
        self.assertEqual(self.config["max_lot_rub"], 2000)

    def test_maximum_purchase_respects_budget_reserve(self):
        self.assertEqual(max_purchase_limit(self.config), 4000)
        with self.assertRaisesRegex(ValueError, "4000"):
            set_setting(self.db, "max_lot_rub", 4001, maximum=4000)

    def test_floor_can_be_disabled_with_zero(self):
        set_setting(self.db, "min_floor_rub", 0)
        self.assertEqual(effective_config(self.config, self.db)["min_floor_rub"], 0)

    def test_first_read_releases_schema_lock_for_other_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with sqlite3.connect(path, timeout=0.1) as first:
                effective_config(self.config, first)
                with sqlite3.connect(path, timeout=0.1) as second:
                    set_setting(second, "min_floor_rub", 1500)
            with sqlite3.connect(path) as reopened:
                self.assertEqual(effective_config(self.config, reopened)["min_floor_rub"], 1500)


if __name__ == "__main__":
    unittest.main()
