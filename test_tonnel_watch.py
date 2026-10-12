import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import patch

import tonnel_watch as watch


def gift(gift_id, price, **extra):
    value = {
        "gift_id": gift_id,
        "gift_num": gift_id,
        "name": "Chill Flame",
        "price": price,
        "asset": "TON",
        "status": "forsale",
        "auction": None,
        "dutchAuctionData": None,
        "premarketData": None,
        "underLoan": False,
    }
    value.update(extra)
    return value


class TonnelScoringTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "max_lot_rub": 2000,
            "min_comparables": 5,
            "floor_cluster_max_spread_fraction": 0.08,
            "variant_min_comparables": 3,
            "variant_floor_cluster_max_spread_fraction": 0.2,
            "min_discount_fraction": 0.15,
            "undercut_ton": 0.01,
            "tonnel_round_trip_cost_fraction": 0.02,
            "fixed_cost_ton": 0,
            "min_scenario_profit_rub": 100,
            "min_roi_fraction": 0.15,
        }
        self.floor = [gift(i, price) for i, price in enumerate([3.57, 3.57, 3.58, 3.59, 3.60, 3.62], 2)]

    def test_clear_discount_passes(self):
        result = watch.score(gift(1, 2.50), self.floor, Decimal("124.42"), self.config)
        self.assertIsNotNone(result)
        self.assertGreater(result["profit_rub"], Decimal("100"))

    def test_small_discount_fails(self):
        self.assertIsNone(watch.score(gift(1, 3.30), self.floor, Decimal("124.42"), self.config))

    def test_floor_below_one_thousand_rubles_fails(self):
        config = dict(self.config, min_floor_rub=1000)
        floor = [gift(i, price) for i, price in enumerate([7.0, 7.1, 7.2, 7.3, 7.4], 2)]
        self.assertIsNone(watch.score(gift(1, 5.0), floor, Decimal("100"), config))

    def test_floor_of_one_thousand_rubles_can_pass(self):
        config = dict(self.config, min_floor_rub=1000)
        floor = [gift(i, price) for i, price in enumerate([10.0, 10.1, 10.2, 10.3, 10.4], 2)]
        self.assertIsNotNone(watch.score(gift(1, 7.0), floor, Decimal("100"), config))

    def test_unstable_floor_fails(self):
        floor = [gift(i, p) for i, p in enumerate([3.57, 3.58, 3.90, 4.10, 5.00], 2)]
        self.assertIsNone(watch.score(gift(1, 2.50), floor, Decimal("124.42"), self.config))

    def test_auction_and_premarket_fail(self):
        self.assertIsNone(watch.score(gift(1, 2.0, auction={"active": True}), self.floor, Decimal("124.42"), self.config))
        self.assertIsNone(watch.score(gift(1, 2.0, premarketData={"active": True}), self.floor, Decimal("124.42"), self.config))

    def test_empty_market_metadata_is_regular_listing(self):
        item = gift(1, 2.0, auction={}, dutchAuctionData={}, premarketData={})
        self.assertTrue(watch.eligible(item))

    def test_model_floor_can_trigger_when_collection_floor_does_not(self):
        item = gift(1, 5.0, model="Rare Model (1%)")
        collection = [gift(i, 4.0) for i in range(2, 8)]
        model_book = [
            gift(i, price, model="Rare Model (1%)")
            for i, price in enumerate([7.0, 7.1, 7.2, 7.3], 20)
        ]
        result = watch.best_score(
            item,
            collection,
            {("model", "Rare Model (1%)"): model_book},
            Decimal("124.42"),
            self.config,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["basis"], "модель: Rare Model (1%)")

    def test_backdrop_floor_can_trigger(self):
        item = gift(1, 5.0, backdrop="Onyx Black (1%)")
        backdrop_book = [
            gift(i, price, backdrop="Onyx Black (1%)")
            for i, price in enumerate([7.0, 7.1, 7.2, 7.3], 20)
        ]
        result = watch.best_score(
            item,
            self.floor,
            {("backdrop", "Onyx Black (1%)"): backdrop_book},
            Decimal("124.42"),
            self.config,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["basis"], "фон: Onyx Black (1%)")

    def test_other_backdrop_cannot_be_purchase_basis(self):
        item = gift(1, 5.0, backdrop="Rare Blue (1%)")
        backdrop_book = [
            gift(i, price, backdrop="Rare Blue (1%)")
            for i, price in enumerate([7.0, 7.1, 7.2, 7.3], 20)
        ]
        result = watch.best_score(
            item,
            self.floor,
            {("backdrop", "Rare Blue (1%)"): backdrop_book},
            Decimal("124.42"),
            self.config,
        )
        self.assertIsNone(result)

    def test_allowed_backdrop_names_ignore_case_and_rarity_suffix(self):
        for backdrop in ("Black", "ONYX BLACK (0.5%)", "Gunmetal", "Midnight Blue (2%)"):
            with self.subTest(backdrop=backdrop):
                self.assertTrue(watch.allowed_backdrop(backdrop))

    def test_rate_limit_is_not_retried_immediately(self):
        class Response:
            status_code = 429
            headers = {"retry-after": "123"}

        with patch.object(watch.requests, "request", return_value=Response()) as request:
            with self.assertRaises(watch.SourceRateLimited) as raised:
                watch.request("GET", "https://example.test", attempts=3)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(raised.exception.retry_after, 123)

    def test_same_or_higher_price_is_not_sent_again(self):
        config = dict(self.config)
        config.update({
            "variant_query_budget_per_cycle": 0,
            "tonnel_collections": ["Chill Flame"],
            "request_pause_seconds": 0,
            "latest_limit": 30,
        })
        item = gift(1, 2.5)
        results = [
            {"price": Decimal("2.5")},
            {"price": Decimal("2.5")},
            {"price": Decimal("2.6")},
        ]
        with sqlite3.connect(":memory:") as db, \
                patch.object(watch, "load_rate", return_value=Decimal("100")), \
                patch.object(watch, "load_collection_book", return_value=[]), \
                patch.object(watch, "fetch_collection", return_value=[item]), \
                patch.object(watch, "load_variant_books", return_value={}), \
                patch.object(watch, "best_score", side_effect=results), \
                patch.object(watch, "format_alert", return_value="offer"), \
                patch.object(watch, "telegram") as send:
            for _ in results:
                watch.run_cycle(config, {}, db, send=True)
            self.assertEqual(send.call_count, 1)
            saved = db.execute("SELECT price FROM tonnel_alerts WHERE id='1'").fetchone()
            self.assertEqual(saved[0], "2.5")

    def test_lower_price_is_sent_again(self):
        config = dict(self.config)
        config.update({
            "variant_query_budget_per_cycle": 0,
            "tonnel_collections": ["Chill Flame"],
            "request_pause_seconds": 0,
            "latest_limit": 30,
        })
        item = gift(1, 2.5)
        results = [
            {"price": Decimal("2.5")},
            {"price": Decimal("2.4")},
        ]
        with sqlite3.connect(":memory:") as db, \
                patch.object(watch, "load_rate", return_value=Decimal("100")), \
                patch.object(watch, "load_collection_book", return_value=[]), \
                patch.object(watch, "fetch_collection", return_value=[item]), \
                patch.object(watch, "load_variant_books", return_value={}), \
                patch.object(watch, "best_score", side_effect=results), \
                patch.object(watch, "format_alert", return_value="offer"), \
                patch.object(watch, "telegram") as send:
            for _ in results:
                watch.run_cycle(config, {}, db, send=True)
            self.assertEqual(send.call_count, 2)
            saved = db.execute("SELECT price FROM tonnel_alerts WHERE id='1'").fetchone()
            self.assertEqual(saved[0], "2.4")

    def test_budget_is_enforced(self):
        self.assertIsNone(watch.score(gift(1, 20.0), self.floor, Decimal("124.42"), self.config))

    def test_slug(self):
        self.assertEqual(watch.slug(gift(123, 2.0)), "chillflame-123")

    def test_alert_contains_direct_tonnel_listing_link(self):
        item = gift(-660800007, 2.50)
        result = watch.score(item, self.floor, Decimal("124.42"), self.config)
        message = watch.format_alert(item, result, self.config)
        self.assertIn(
            "https://t.me/tonnel_network_bot/gift?startapp=-660800007",
            message,
        )


if __name__ == "__main__":
    unittest.main()
