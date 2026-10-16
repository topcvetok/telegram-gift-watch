import sqlite3
import unittest

from special_traits import apply_special_bases, beautiful_number, cached_monochrome_score


class SpecialTraitsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.item = {"name": "Scared Cat", "gift_num": 12121}
        self.result = {
            "basis": "модель: Weedcat",
            "basis_scope": "model",
            "basis_trait": "Weedcat",
        }

    def tearDown(self):
        self.db.close()

    def test_beautiful_number_uses_distinct_digits(self):
        config = {
            "beautiful_number_enabled": True,
            "beautiful_number_max_distinct_digits": 3,
        }
        self.assertEqual(beautiful_number(self.item, config), (12121, 2))
        self.assertIsNone(beautiful_number(
            {"gift_num": 12345}, config
        ))

    def test_beautiful_number_can_be_disabled(self):
        self.assertIsNone(beautiful_number(
            self.item,
            {"beautiful_number_enabled": False, "beautiful_number_max_distinct_digits": 3},
        ))

    def test_monochrome_score_is_cached_by_gift_slug(self):
        calls = []

        def fetch(slug):
            calls.append(slug)
            return 86

        config = {"monochrome_cache_seconds": 1000}
        self.assertEqual(cached_monochrome_score(self.item, self.db, config, fetch), 86)
        self.assertEqual(cached_monochrome_score(self.item, self.db, config, fetch), 86)
        self.assertEqual(calls, ["scaredcat-12121"])

    def test_enabled_reasons_are_added_without_losing_model_floor_scope(self):
        config = {
            "beautiful_number_enabled": True,
            "beautiful_number_max_distinct_digits": 3,
            "monochrome_enabled": True,
            "monochrome_min_score": 80,
        }
        enriched = apply_special_bases(
            self.item, self.result, config, self.db, fetch=lambda slug: 86
        )
        self.assertIn("красивый номер #12121", enriched["basis"])
        self.assertIn("монохром 86%", enriched["basis"])
        self.assertIn("floor: модель: Weedcat", enriched["basis"])
        self.assertEqual(enriched["basis_scope"], "model")
        self.assertEqual(enriched["monochrome_score"], 86)

    def test_partial_monochrome_is_recorded_but_not_used_as_basis(self):
        config = {
            "beautiful_number_enabled": False,
            "monochrome_enabled": True,
            "monochrome_min_score": 80,
        }
        enriched = apply_special_bases(
            self.item, self.result, config, self.db, fetch=lambda slug: 79
        )
        self.assertEqual(enriched["basis"], "модель: Weedcat")
        self.assertEqual(enriched["monochrome_score"], 79)


if __name__ == "__main__":
    unittest.main()
