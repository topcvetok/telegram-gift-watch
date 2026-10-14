import sqlite3
import time
import unittest
from decimal import Decimal
import market_data


class MarketDataTests(unittest.TestCase):
    def test_only_model_floors_are_kept_for_every_market(self):
        item = {"name": "Evil Eye"}
        result = {
            "basis_scope": "collection",
            "market_floors": [{
                "market": "Tonnel", "model_floor": Decimal("9.1"),
                "collection_floor": Decimal("7.1"),
            }],
        }
        with sqlite3.connect(":memory:") as db:
            enriched = market_data.complete_market_floors(item, result, db, {})
        self.assertEqual(
            [
                (row["market"], row["model_floor"])
                for row in enriched["market_floors"]
            ],
            [("Tonnel", Decimal("9.1")), ("Portals", None), ("MRKT", None)],
        )
        self.assertTrue(enriched["market_floors"][0]["model_verified"])
        self.assertNotIn("collection_floor", enriched["market_floors"][0])

    def test_legacy_collection_floor_is_discarded(self):
        item = {"name": "Evil Eye"}
        result = {
            "basis_scope": "model",
            "market_floors": [{"market": "Portals", "model_floor": Decimal("9")}],
        }
        with sqlite3.connect(":memory:") as db:
            enriched = market_data.complete_market_floors(item, result, db, {})
        self.assertEqual(
            [
                (row["market"], row["model_floor"])
                for row in enriched["market_floors"]
            ],
            [("Tonnel", None), ("Portals", Decimal("9")), ("MRKT", None)],
        )
        self.assertTrue(all(
            "collection_floor" not in row for row in enriched["market_floors"]
        ))

    def test_liquidity_uses_sales_listings_and_price_changes_for_collection(self):
        now = time.time()
        with sqlite3.connect(":memory:") as db:
            market_data.record_market_activity(
                db, "one", "tonnel", "Evil Eye", "listing", now - 10
            )
            market_data.record_market_activity(
                db, "two", "mrkt", "Evil Eye", "price", now - 9
            )
            market_data.record_market_activity(
                db, "three", "tonnel", "Evil Eye", "sale", now - 8
            )
            market_data.record_market_activity(
                db, "old", "tonnel", "Evil Eye", "listing", now - 90_000
            )
            result = market_data.apply_activity_evidence(
                {"name": "Evil Eye"}, {}, db,
                {"liquidity_medium_activity_24h": 10,
                 "liquidity_high_activity_24h": 30},
                portals_sales_24h=3,
                now=now,
            )
        self.assertEqual(result["activity_sales_24h"], 4)
        self.assertEqual(result["activity_listings_24h"], 1)
        self.assertEqual(result["activity_price_changes_24h"], 1)
        self.assertEqual(result["activity_24h"], 6)
        self.assertEqual(result["activity_score_24h"], 15)
        self.assertEqual(result["liquidity"], "средняя")


if __name__ == "__main__":
    unittest.main()
