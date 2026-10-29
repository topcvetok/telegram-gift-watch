import sqlite3
import unittest
from datetime import datetime, timezone
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
        "listed_at": datetime.now(timezone.utc).isoformat(),
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

    def test_displayed_model_floor_uses_one_active_listing(self):
        item = watch.portals_item(raw_gift())
        book = [
            item,
            watch.portals_item(raw_gift("p2", "12.5")),
            watch.portals_item(raw_gift("p3", "40")),
        ]
        self.assertEqual(
            watch.comparable_model_floor(item, book, self.config),
            Decimal("12.5"),
        )

    def test_cross_market_snapshot_has_only_model_floors(self):
        db = sqlite3.connect(":memory:")
        item = watch.portals_item(raw_gift())
        result = {"rate": Decimal("100")}
        with patch.object(
                    watch, "tonnel_model_floor", return_value=Decimal("11"),
                ), patch.object(
                    watch, "portals_model_floor", return_value=Decimal("12"),
                ), patch.object(
                    watch, "mrkt_model_floor", return_value=Decimal("11.8"),
                ), patch.object(
                    watch, "collection_stats", return_value={"sales_24h_count": 5},
                ):
            enriched = watch.add_portals_floor(
                item, result, self.config, {"swift_api_key": "token"}, db,
                portals_session=Mock(), swift_session=Mock(),
            )
        self.assertEqual(
            [
                (row["market"], row["model_floor"])
                for row in enriched["market_floors"]
            ],
            [("Tonnel", Decimal("11")),
             ("Portals", Decimal("12")),
             ("MRKT", Decimal("11.8"))],
        )

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
            "price": Decimal("8"), "rate": Decimal("100"),
            "basis": "модель: Watcher", "next_floor": Decimal("12"),
            "market_floors": [
                {"market": "Tonnel", "model_floor": Decimal("11.5"),
                 "collection_floor": Decimal("7")},
                {"market": "Portals", "model_floor": Decimal("12"),
                 "collection_floor": Decimal("7.2")},
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
        self.assertIn("<b>оффер на Portals</b>", text)
        self.assertNotIn("РИСК", text)
        self.assertIn("<b>Floor модели по площадкам:</b>", text)
        self.assertIn("<b>Tonnel</b>", text)
        self.assertIn("• модель: 11.500 TON", text)
        self.assertNotIn("• коллекция:", text)
        self.assertNotIn("История продаж:", text)
        self.assertNotIn("https://t.me/nft/evileye-42", text)
        self.assertNotIn("Открыть оффер", text)
        self.assertEqual(
            watch.listing_link(item),
            "https://t.me/portals/market?startapp=gift_p1",
        )

    def test_relaxed_monochrome_alert_shows_floor_tolerance_instead_of_profit(self):
        item = watch.portals_item(raw_gift())
        result = {
            "price": Decimal("11.5"), "rate": Decimal("100"),
            "basis": "монохром 86%; floor: модель: Watcher",
            "basis_scope": "model", "next_floor": Decimal("10"),
            "liquidity": "низкая", "collection_sales_24h": 0,
            "resale": Decimal("9.99"), "profit_rub": Decimal("-170"),
            "monochrome_relaxed": True,
            "monochrome_floor_delta_fraction": Decimal("0.15"),
            "monochrome_max_floor_premium_fraction": Decimal("0.15"),
        }
        text = watch.format_alert(item, result, self.config)
        self.assertIn("Цена относительно floor модели: +15.0%", text)
        self.assertNotIn("Расчётная прибыль", text)

    def test_same_or_higher_price_is_not_sent_twice(self):
        db = sqlite3.connect(":memory:")
        item = watch.portals_item(raw_gift())
        result = {
            "price": Decimal("8"), "rate": Decimal("100"),
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

        def cross_floors(_item, result, *_args, **_kwargs):
            result["market_floors"] = [
                {"market": "Tonnel", "model_floor": Decimal("11"),
                 "collection_floor": Decimal("7")},
                {"market": "Portals", "model_floor": Decimal("12"),
                 "collection_floor": Decimal("7.2")},
                {"market": "MRKT", "model_floor": Decimal("11.8"),
                 "collection_floor": Decimal("7.4")},
            ]
            result["activity_sales_24h"] = 82
            result["activity_24h"] = 82
            result["liquidity"] = "высокая"
            return result

        with patch.object(watch, "load_rate", return_value=Decimal("100")), \
                patch.object(watch, "cached_book", side_effect=book), \
                patch.object(watch, "add_portals_floor", side_effect=cross_floors), \
                patch.object(watch, "telegram") as send:
            self.assertTrue(watch.process_listing(
                item, self.config, {}, db, Mock(), "", send=True
            ))
        self.assertEqual(send.call_count, 1)
        message = send.call_args.args[1]
        self.assertIn("оффер на Portals", message)
        self.assertIn("• модель: 11.000 TON", message)
        self.assertNotIn("• коллекция:", message)
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

    def test_disabled_portals_skips_feed(self):
        db = sqlite3.connect(":memory:")
        config = dict(self.config, portals_enabled=False)
        with patch.object(watch, "fetch_listings") as fetch:
            self.assertEqual(watch.run_cycle(config, {}, db, Mock(), send=False), 0)
        fetch.assert_not_called()

    def test_rate_limit_stops_cycle_after_first_failed_candidate(self):
        db = sqlite3.connect(":memory:")
        items = [
            watch.portals_item(raw_gift("g1")),
            watch.portals_item(raw_gift("g2")),
        ]
        with patch.object(watch, "fetch_listings", return_value=items), \
                patch.object(watch, "refresh_sold_alerts", return_value=0), \
                patch.object(
                    watch, "process_listing",
                    side_effect=watch.PortalsRateLimited(30),
                ) as process:
            with self.assertRaises(watch.PortalsRateLimited):
                watch.run_cycle(self.config, {}, db, Mock(), send=False)
        self.assertEqual(process.call_count, 1)

    def test_sold_listing_edits_its_alert(self):
        db = sqlite3.connect(":memory:")
        with patch.object(watch, "pending_offer_ids", return_value=["p1"]), \
                patch.object(watch, "fetch_nft", return_value=raw_gift(status="sold")), \
                patch.object(watch, "mark_offer_sold", return_value=True) as sold:
            self.assertEqual(
                watch.refresh_sold_alerts(self.config, {}, db, Mock()), 1
            )
        sold.assert_called_once()
        self.assertEqual(sold.call_args.args[1:3], ("Portals", "p1"))

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
