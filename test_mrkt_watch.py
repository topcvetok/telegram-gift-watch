import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

import mrkt_watch as watch


def raw_gift(gift_id="g1", price=10_000_000_000, **changes):
    value = {
        "id": gift_id,
        "number": 42,
        "collectionName": "Evil Eye",
        "modelName": "Watcher",
        "backdropName": "Onyx Black",
        "symbolName": "Moon",
        "salePrice": price,
        "salePriceWithoutFee": int(price / 1.02),
        "isOnSale": True,
        "isOnAuction": False,
        "isMine": False,
        "isLocked": False,
        "isLockedForSale": False,
        "premarketStatus": None,
        "nextResaleDate": None,
    }
    value.update(changes)
    return value


class MrktTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "max_lot_rub": 2000,
            "min_floor_rub": 1000,
            "min_scenario_profit_rub": 100,
            "min_roi_fraction": 0.15,
            "min_discount_fraction": 0.15,
            "undercut_ton": 0.01,
            "fixed_cost_ton": 0,
            "floor_limit": 12,
            "variant_floor_limit": 10,
            "min_comparables": 5,
            "floor_cluster_max_spread_fraction": 0.08,
            "variant_min_comparables": 3,
            "variant_floor_cluster_max_spread_fraction": 0.2,
            "mrkt_round_trip_cost_fraction": 0.0196078431,
            "sales_confirmation_min_count": 3,
            "liquidity_medium_sales_24h": 3,
            "liquidity_high_sales_24h": 8,
            "strong_signal_min_basis_sales_24h": 5,
            "strong_signal_min_discount_fraction": 0.25,
            "strong_signal_min_roi_fraction": 0.25,
        }

    def test_current_api_contract(self):
        query = watch.listing_query(collection="Evil Eye", model="Watcher")
        self.assertEqual(query["collectionNames"], ["Evil Eye"])
        self.assertEqual(query["modelNames"], ["Watcher"])
        self.assertTrue(query["lowToHigh"])
        self.assertEqual(query["ordering"], "Price")
        self.assertEqual(list(watch.EVENT_TYPES), ["Sale", "Listing", "ChangePrice"])

    def test_parses_listing_price_and_traits(self):
        item = watch.mrkt_item(raw_gift())
        self.assertEqual(item["gift_id"], "g1")
        self.assertEqual(item["price"], Decimal("10"))
        self.assertEqual(item["model"], "Watcher")
        self.assertEqual(item["backdrop"], "Onyx Black")

    def test_event_amount_overrides_stale_gift_price(self):
        item = watch.mrkt_item(raw_gift(price=12_000_000_000), {"amount": 9_000_000_000})
        self.assertEqual(item["price"], Decimal("9"))

    def test_auction_premarket_and_locked_gifts_are_rejected(self):
        self.assertIsNone(watch.mrkt_item(raw_gift(isOnAuction=True)))
        self.assertIsNone(watch.mrkt_item(raw_gift(premarketStatus="Premarket")))
        self.assertIsNone(watch.mrkt_item(raw_gift(isLocked=True)))
        self.assertIsNone(watch.mrkt_item(raw_gift(nextResaleDate="2999-01-01T00:00:00Z")))

    def test_direct_link_is_kept_for_button(self):
        item = watch.mrkt_item(raw_gift())
        result = {
            "quality": "рискованный", "price": Decimal("10"), "rate": Decimal("100"),
            "basis": "модель: Watcher", "next_floor": Decimal("13"),
            "discount": Decimal("0.23"), "liquidity": "низкая",
            "collection_sales_24h": 0, "basis_sales_24h": 0,
            "median_sale": None, "resale": Decimal("12.99"),
            "profit_ton": Decimal("2.7"), "profit_rub": Decimal("270"),
        }
        text = watch.format_alert(item, result, self.config)
        self.assertIn("оффер на MRKT", text)
        self.assertNotIn("Открыть оффер", text)
        self.assertEqual(watch.listing_link(item), "https://t.me/mrkt/app?startapp=giftg1")

    def test_sale_is_recorded_once(self):
        db = sqlite3.connect(":memory:")
        event = {
            "id": "sale-1", "type": "Sale", "amount": 11_000_000_000,
            "date": "2026-10-13T00:00:00Z", "gift": raw_gift(),
        }
        self.assertTrue(watch.record_sale(event, db))
        self.assertFalse(watch.record_sale(event, db))
        row = db.execute("SELECT source, price FROM tonnel_sales").fetchone()
        self.assertEqual(row, ("mrkt", "11"))

    def test_same_or_higher_price_is_not_sent_twice(self):
        db = sqlite3.connect(":memory:")
        item = watch.mrkt_item(raw_gift())
        result = {
            "quality": "рискованный", "price": Decimal("10"), "rate": Decimal("100"),
            "basis": "вся коллекция", "next_floor": Decimal("13"),
            "discount": Decimal("0.23"), "liquidity": "низкая",
            "collection_sales_24h": 0, "basis_sales_24h": 0,
            "median_sale": None, "resale": Decimal("12.99"),
            "profit_ton": Decimal("2.7"), "profit_rub": Decimal("270"),
        }
        with patch.object(watch, "telegram") as send:
            self.assertTrue(watch.send_if_new_price(item, result, self.config, {}, db, True))
            self.assertFalse(watch.send_if_new_price(item, result, self.config, {}, db, True))
            self.assertFalse(watch.send_if_new_price(
                item, dict(result, price=Decimal("11")), self.config, {}, db, True
            ))
            self.assertTrue(watch.send_if_new_price(
                item, dict(result, price=Decimal("9")), self.config, {}, db, True
            ))
        self.assertEqual(send.call_count, 2)
        self.assertEqual(
            send.call_args.kwargs["preview_url"],
            "https://t.me/nft/evileye-42",
        )

    def test_fetch_book_maps_api_response(self):
        session = Mock()
        response = Mock(status_code=200)
        response.json.return_value = {"gifts": [raw_gift()]}
        session.request.return_value = response
        book = watch.fetch_book(session, "token", collection="Evil Eye")
        self.assertEqual(book[0]["price"], Decimal("10"))
        body = session.request.call_args.kwargs["json"]
        self.assertEqual(body["collectionNames"], ["Evil Eye"])
        self.assertEqual(session.request.call_args.args[1], watch.API_URL + "/gifts/saling")

    def test_listing_event_is_scored_sent_and_deduplicated(self):
        db = sqlite3.connect(":memory:")
        event = {
            "id": "listing-1", "type": "Listing", "amount": 8_000_000_000,
            "date": "2026-10-13T00:00:00Z", "gift": raw_gift(price=8_000_000_000),
        }
        collection_book = [
            watch.mrkt_item(raw_gift(f"c{i}", int((12 + i / 100) * 1_000_000_000)))
            for i in range(5)
        ]
        model_book = collection_book[:3]

        def book(*args, **kwargs):
            return model_book if kwargs.get("model") or kwargs.get("backdrop") else collection_book

        with patch.object(watch, "load_rate", return_value=Decimal("100")), \
                patch.object(watch, "cached_book", side_effect=book), \
                patch.object(
                    watch, "attach_cross_market_floors",
                    side_effect=lambda *args, **kwargs: args[1],
                ), \
                patch.object(watch, "telegram") as send:
            self.assertTrue(watch.process_event(
                event, self.config, {}, db, Mock(), "token", send=True
            ))
            self.assertFalse(watch.process_event(
                event, self.config, {}, db, Mock(), "token", send=True
            ))
        self.assertEqual(send.call_count, 1)
        self.assertIn("оффер на MRKT", send.call_args.args[1])
        self.assertEqual(
            send.call_args.kwargs["button_url"],
            "https://t.me/mrkt/app?startapp=feedlisting1",
        )

    def test_missing_token_does_not_break_tonnel_process(self):
        db = sqlite3.connect(":memory:")
        with patch("builtins.print"):
            self.assertEqual(watch.run(self.config, {}, db, duration=1), 0)

    def test_fail_fast_requires_token(self):
        db = sqlite3.connect(":memory:")
        with self.assertRaisesRegex(RuntimeError, "MRKT_TOKEN"):
            watch.run(self.config, {}, db, duration=1, fail_fast=True)


if __name__ == "__main__":
    unittest.main()
