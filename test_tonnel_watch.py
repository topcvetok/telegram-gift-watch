import unittest
from decimal import Decimal

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

    def test_unstable_floor_fails(self):
        floor = [gift(i, p) for i, p in enumerate([3.57, 3.58, 3.90, 4.10, 5.00], 2)]
        self.assertIsNone(watch.score(gift(1, 2.50), floor, Decimal("124.42"), self.config))

    def test_auction_and_premarket_fail(self):
        self.assertIsNone(watch.score(gift(1, 2.0, auction={}), self.floor, Decimal("124.42"), self.config))
        self.assertIsNone(watch.score(gift(1, 2.0, premarketData={}), self.floor, Decimal("124.42"), self.config))

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
