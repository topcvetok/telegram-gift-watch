import sqlite3
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

import market_data


class MarketDataTests(unittest.TestCase):
    def test_collection_floor_contains_every_market_and_preserves_direct_floor(self):
        item = {"name": "Evil Eye"}
        result = {
            "basis_scope": "collection",
            "market_floors": [{"market": "Tonnel", "floor": Decimal("7.1")}],
        }
        catalog = {
            "gifts": [{
                "name": "Evil Eye",
                "tonnel_floor_ton": "7",
                "portals_floor_ton": "7.2",
                "mrkt_floor_ton": "7.4",
            }]
        }
        with sqlite3.connect(":memory:") as db, \
                patch.object(market_data, "_cached_catalog", return_value=catalog):
            enriched = market_data.complete_market_floors(item, result, db, {})
        self.assertEqual(
            [(row["market"], row["floor"]) for row in enriched["market_floors"]],
            [("Tonnel", Decimal("7.1")), ("Portals", Decimal("7.2")),
             ("MRKT", Decimal("7.4"))],
        )
        self.assertTrue(enriched["market_floors"][0]["verified"])
        self.assertFalse(enriched["market_floors"][2]["verified"])

    def test_trait_floor_marks_missing_market_instead_of_using_collection_floor(self):
        item = {"name": "Evil Eye"}
        result = {
            "basis_scope": "model",
            "market_floors": [{"market": "Portals", "floor": Decimal("9")}],
        }
        with sqlite3.connect(":memory:") as db, \
                patch.object(market_data, "_cached_catalog") as catalog:
            enriched = market_data.complete_market_floors(item, result, db, {})
        catalog.assert_not_called()
        self.assertEqual(
            [(row["market"], row["floor"]) for row in enriched["market_floors"]],
            [("Tonnel", None), ("Portals", Decimal("9")), ("MRKT", None)],
        )

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
