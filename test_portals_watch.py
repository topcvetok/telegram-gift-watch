import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

import portals_watch as watch


def raw_gift(gift_id="p1", price="8", **changes):
    value = {
        "id": gift_id,
        "tg_id": "EvilEye-42",
        "collection_id": "collection-evil-eye",
        "external_collection_number": 42,
        "name": "Evil Eye",
        "price": price,
        "status": "listed",
        "listed_at": "2026-10-13T09:16:27Z",
        "unlocks_at": "2025-01-01T00:00:00Z",
        "attributes": [
            {"type": "model", "value": "Watcher"},
            {"type": "backdrop", "value": "Onyx Black"},
            {"type": "symbol", "value": "Moon"},
        ],
    }
    value.update(changes)
    return value


class PortalsTests(unittest.TestCase):
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
            "portals_round_trip_cost_fraction": 0.02,
            "portals_book_cache_seconds": 20,
            "sales_confirmation_min_count": 3,
            "liquidity_medium_sales_24h": 3,
            "liquidity_high_sales_24h": 8,
            "strong_signal_min_basis_sales_24h": 5,
            "strong_signal_min_discount_fraction": 0.25,
            "strong_signal_min_roi_fraction": 0.25,
        }

    def test_parses_public_listing_and_traits(self):
        item = watch.portals_item(raw_gift())
        self.assertEqual(item["gift_id"], "p1")
        self.assertEqual(item["gift_num"], 42)
        self.assertEqual(item["collection_id"], "collection-evil-eye")
        self.assertEqual(item["price"], Decimal("8"))
        self.assertEqual(item["model"], "Watcher")
        self.assertEqual(item["backdrop"], "Onyx Black")

    def test_future_locked_and_unlisted_gifts_are_rejected(self):
        self.assertIsNone(watch.portals_item(raw_gift(status="sold")))
        self.assertIsNone(watch.portals_item(raw_gift(unlocks_at="2999-01-01T00:00:00Z")))

    def test_public_search_does_not_require_token(self):
        session = Mock()
        response = Mock(status_code=200)
        response.json.return_value = {"results": [raw_gift()]}
        session.get.return_value = response
        items = watch.fetch_listings(session, collection="Evil Eye")
        self.assertEqual(items[0]["price"], Decimal("8"))
        call = session.get.call_args
        self.assertNotIn("authorization", call.kwargs["headers"])
        self.assertEqual(call.kwargs["params"]["filter_by_collections"], "Evil Eye")

    def test_current_api_filters_books_by_collection_id(self):
        params = watch.search_params(
            collection="Evil Eye", collection_id="collection-evil-eye", model="Watcher"
        )
        self.assertEqual(params["collection_ids"], "collection-evil-eye")
        self.assertNotIn("filter_by_collections", params)
        self.assertEqual(params["filter_by_models"], "Watcher")

    def test_stable_floor_requires_a_tight_cluster(self):
        item = watch.portals_item(raw_gift())
        stable = [watch.portals_item(raw_gift(f"p{i}", str(12 + i / 100))) for i in range(2, 7)]
        unstable = [watch.portals_item(raw_gift(f"u{i}", str(p))) for i, p in enumerate([12, 12.1, 14], 2)]
        self.assertEqual(watch.stable_floor(item, stable, 5, 0.08), Decimal("12.02"))
        self.assertIsNone(watch.stable_floor(item, unstable, 3, 0.08))

    def test_cached_book_restores_decimal_prices(self):
        db = sqlite3.connect(":memory:")
        session = Mock()
        with patch.object(watch, "resolve_collection_id", return_value="collection-evil-eye"), \
                patch.object(watch, "fetch_listings", return_value=[
                    watch.portals_item(raw_gift(price="12.34"))
                ]) as fetch:
            first = watch.cached_book(
                db, session, "", collection="Evil Eye", config=self.config
            )
            second = watch.cached_book(
                db, session, "", collection="Evil Eye", config=self.config
            )
        self.assertEqual(first[0]["price"], Decimal("12.34"))
        self.assertEqual(second[0]["price"], Decimal("12.34"))
        self.assertEqual(fetch.call_count, 1)

    def test_alert_uses_requested_format_without_visible_links_or_history(self):
        item = watch.portals_item(raw_gift())
        result = {
            "quality": "нормальный", "price": Decimal("8"), "rate": Decimal("100"),
            "basis": "модель: Watcher", "next_floor": Decimal("12"),
            "market_floors": [
                {"market": "Tonnel", "floor": Decimal("11.5")},
                {"market": "Portals", "floor": Decimal("12")},
            ],
            "liquidity": "средняя", "collection_sales_24h": 4,
            "basis_sales_24h": 3,
            "recent_sales": [
                {"source": "tonnel", "price": Decimal("11")},
                {"source": "portals", "price": Decimal("11.2")},
            ],
            "resale": Decimal("11.99"), "profit_rub": Decimal("350"),
        }
        text = watch.format_alert(item, result, self.config)
        self.assertIn("СРЕДНИЙ РИСК: оффер на Portals", text)
        self.assertIn("<b>Устойчивые floor:</b>", text)
        self.assertIn("Tonnel — 11.500 TON", text)
        self.assertNotIn("История продаж:", text)
        self.assertNotIn("https://t.me/nft/evileye-42", text)
        self.assertNotIn("Открыть оффер", text)
        self.assertEqual(
            watch.listing_link(item),
            "https://t.me/portals/market?startapp=gift_p1",
        )

    def test_same_or_higher_price_is_not_sent_twice(self):
        db = sqlite3.connect(":memory:")
        item = watch.portals_item(raw_gift())
        result = {
            "quality": "рискованный", "price": Decimal("8"), "rate": Decimal("100"),
            "basis": "вся коллекция", "next_floor": Decimal("12"),
            "liquidity": "низкая", "collection_sales_24h": 0,
            "basis_sales_24h": 0, "recent_sales": [],
            "resale": Decimal("11.99"), "profit_rub": Decimal("300"),
        }
        with patch.object(watch, "telegram") as send:
            self.assertTrue(watch.send_if_new_price(item, result, self.config, {}, db, True))
            self.assertFalse(watch.send_if_new_price(item, result, self.config, {}, db, True))
            self.assertFalse(watch.send_if_new_price(
                item, dict(result, price=Decimal("9")), self.config, {}, db, True
            ))
            self.assertTrue(watch.send_if_new_price(
                item, dict(result, price=Decimal("7")), self.config, {}, db, True
            ))
        self.assertEqual(send.call_count, 2)
        self.assertEqual(
            send.call_args.kwargs["preview_url"],
            "https://t.me/nft/evileye-42",
        )
        self.assertEqual(
            send.call_args.kwargs["button_url"],
            "https://t.me/portals/market?startapp=gift_p1",
        )

    def test_listing_is_scored_with_model_and_allowed_backdrop(self):
        db = sqlite3.connect(":memory:")
        item = watch.portals_item(raw_gift())
        collection = [
            watch.portals_item(raw_gift(f"c{i}", str(12 + i / 100))) for i in range(5)
        ]

        def book(*args, **kwargs):
            return collection[:3] if kwargs.get("model") or kwargs.get("backdrop") else collection

        with patch.object(watch, "load_rate", return_value=Decimal("100")), \
                patch.object(watch, "cached_book", side_effect=book), \
                patch.object(watch, "tonnel_comparable_floor", return_value=Decimal("11")), \
                patch.object(watch, "collection_stats", return_value={"sales_24h_count": 82}), \
                patch.object(watch, "complete_market_floors", side_effect=lambda _i, r, *_: r), \
                patch.object(watch, "telegram") as send:
            self.assertTrue(watch.process_listing(
                item, self.config, {}, db, Mock(), "", send=True
            ))
        self.assertEqual(send.call_count, 1)
        message = send.call_args.args[1]
        self.assertIn("оффер на Portals", message)
        self.assertIn("Tonnel — 11.000 TON", message)
        self.assertIn("активность за 24 ч: 82", message)

    def test_feed_records_listing_and_price_change_once(self):
        db = sqlite3.connect(":memory:")
        original = watch.portals_item(raw_gift(price="8"))
        raised = watch.portals_item(raw_gift(price="9"))
        with patch.object(watch, "fetch_listings", side_effect=[
                    [original], [original], [raised]
                ]), patch.object(watch, "process_listing", return_value=False) as process, \
                patch("builtins.print"):
            watch.run_cycle(self.config, {}, db, Mock(), send=False)
            watch.run_cycle(self.config, {}, db, Mock(), send=False)
            watch.run_cycle(self.config, {}, db, Mock(), send=False)
        rows = db.execute(
            "SELECT kind, COUNT(*) FROM market_activity GROUP BY kind ORDER BY kind"
        ).fetchall()
        self.assertEqual(rows, [("listing", 1), ("price", 1)])
        self.assertEqual(process.call_count, 1)

    def test_fail_fast_run_reports_unavailable_portals(self):
        db = sqlite3.connect(":memory:")
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        with patch.object(watch.requests, "Session", return_value=session), \
                patch.object(watch, "run_cycle", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                watch.run(
                    self.config, {}, db, send=False, duration=1, fail_fast=True
                )


if __name__ == "__main__":
    unittest.main()
