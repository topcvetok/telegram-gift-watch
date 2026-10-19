import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from runtime_settings import (
    activate_settings,
    active_config,
    active_profiles,
    authorize_chat,
    effective_config,
    feature_enabled,
    set_feature_enabled,
    set_number_setting,
    set_setting,
    set_source_enabled,
    source_enabled,
)


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
        activate_settings(None)
        self.db.close()

    def test_overrides_are_persistent_and_leave_source_config_unchanged(self):
        set_setting(self.db, "max_lot_rub", 3000)
        set_setting(self.db, "min_floor_rub", 1500)
        current = effective_config(self.config, self.db)
        self.assertEqual(current["max_lot_rub"], 3000)
        self.assertEqual(current["min_floor_rub"], 1500)
        self.assertEqual(self.config["max_lot_rub"], 2000)

    def test_maximum_purchase_has_no_budget_cap(self):
        set_setting(self.db, "max_lot_rub", 25000)
        self.assertEqual(effective_config(self.config, self.db)["max_lot_rub"], 25000)

    def test_ton_threshold_is_stored_without_conversion(self):
        set_setting(self.db, "max_lot_rub", "20.5", currency="ton")
        current = effective_config(self.config, self.db)
        self.assertEqual(current["max_lot_ton"], Decimal("20.5"))

    def test_floor_can_be_disabled_with_zero(self):
        set_setting(self.db, "min_floor_rub", 0)
        self.assertEqual(effective_config(self.config, self.db)["min_floor_rub"], 0)

    def test_market_switches_are_persistent_and_default_on(self):
        current = effective_config(self.config, self.db)
        self.assertTrue(all(source_enabled(current, source) for source in ("tonnel", "portals", "mrkt")))
        set_source_enabled(self.db, "portals", False)
        self.assertFalse(source_enabled(effective_config(self.config, self.db), "portals"))

    def test_special_bases_and_thresholds_are_persistent(self):
        current = effective_config(self.config, self.db)
        self.assertTrue(feature_enabled(current, "collection_basis"))
        self.assertTrue(feature_enabled(current, "model_basis"))
        self.assertTrue(feature_enabled(current, "backdrop_basis"))
        self.assertTrue(feature_enabled(current, "beautiful_number"))
        self.assertTrue(feature_enabled(current, "monochrome"))
        set_feature_enabled(self.db, "monochrome", False)
        set_feature_enabled(self.db, "collection_basis", False)
        set_number_setting(self.db, "beautiful_number_max_distinct_digits", 2)
        set_number_setting(self.db, "monochrome_min_score", 85)
        set_number_setting(self.db, "monochrome_max_floor_premium_percent", 15)
        current = effective_config(self.config, self.db)
        self.assertFalse(feature_enabled(current, "monochrome"))
        self.assertFalse(feature_enabled(current, "collection_basis"))
        self.assertEqual(current["beautiful_number_max_distinct_digits"], 2)
        self.assertEqual(current["monochrome_min_score"], 85)
        self.assertEqual(current["monochrome_max_floor_premium_percent"], 15)

    def test_special_threshold_ranges_are_validated(self):
        with self.assertRaisesRegex(ValueError, "от 1 до 10"):
            set_number_setting(self.db, "beautiful_number_max_distinct_digits", 0)
        with self.assertRaisesRegex(ValueError, "от 0 до 100"):
            set_number_setting(self.db, "monochrome_min_score", 101)
        with self.assertRaisesRegex(ValueError, "от 0 до 100"):
            set_number_setting(self.db, "monochrome_max_floor_premium_percent", 101)

    def test_active_config_reads_dedicated_settings_connection(self):
        set_setting(self.db, "max_lot_rub", 2750)
        activate_settings(self.db)
        self.assertEqual(active_config(self.config)["max_lot_rub"], 2750)

    def test_first_read_releases_schema_lock_for_other_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with sqlite3.connect(path, timeout=0.1) as first:
                effective_config(self.config, first)
                with sqlite3.connect(path, timeout=0.1) as second:
                    set_setting(second, "min_floor_rub", 1500)
            with sqlite3.connect(path) as reopened:
                self.assertEqual(effective_config(self.config, reopened)["min_floor_rub"], 1500)

    def test_authorized_users_have_independent_settings(self):
        activate_settings(self.db, "owner")
        authorize_chat(self.db, "guest")
        set_setting(self.db, "max_lot_rub", 3000, chat_id="owner")
        set_setting(self.db, "max_lot_rub", 9000, chat_id="guest")
        set_source_enabled(self.db, "portals", False, chat_id="guest")

        profiles = dict(active_profiles(self.config))
        self.assertEqual(profiles["owner"]["max_lot_rub"], 3000)
        self.assertEqual(profiles["guest"]["max_lot_rub"], 9000)
        self.assertTrue(source_enabled(profiles["owner"], "portals"))
        self.assertFalse(source_enabled(profiles["guest"], "portals"))

    def test_legacy_settings_are_migrated_only_to_owner(self):
        self.db.execute(
            "CREATE TABLE runtime_settings "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL)"
        )
        self.db.execute(
            "INSERT INTO runtime_settings VALUES ('max_lot_rub', '3500', 1)"
        )
        self.db.commit()
        activate_settings(self.db, "owner")
        authorize_chat(self.db, "guest")
        self.assertEqual(
            effective_config(self.config, self.db, "owner")["max_lot_rub"], 3500
        )
        self.assertEqual(
            effective_config(self.config, self.db, "guest")["max_lot_rub"], 2000
        )


if __name__ == "__main__":
    unittest.main()
