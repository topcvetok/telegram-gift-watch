import io
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import upgrade_watch as watch


def jpeg(color):
    image = Image.new("RGB", (32, 32), color)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


class UpgradeWatchTests(unittest.TestCase):
    def test_zero_head_is_seeded_for_a_collection_without_upgrades(self):
        with tempfile.TemporaryDirectory() as directory, sqlite3.connect(":memory:") as db:
            seed_path = Path(directory) / "heads.json"
            seed_path.write_text(json.dumps({"New Gift": 0}))
            with patch.object(watch, "SEED_PATH", seed_path):
                self.assertEqual(watch.seed_heads(db), 1)
            self.assertEqual(
                db.execute(
                    "SELECT last_number FROM telegram_upgrade_heads WHERE collection=?",
                    ("New Gift",),
                ).fetchone(),
                (0,),
            )

    def test_slugs_match_telegram_and_fragment_formats(self):
        self.assertEqual(watch.cdn_slug("Jack-in-the-Box"), "jackinthebox")
        self.assertEqual(watch.telegram_slug("Durov's Cap"), "DurovsCap")

    def test_only_black_and_onyx_edges_are_classified(self):
        self.assertEqual(watch.classify_backdrop(jpeg((14, 15, 15))), "Black")
        self.assertEqual(watch.classify_backdrop(jpeg((49, 54, 56))), "Onyx Black")
        self.assertIsNone(watch.classify_backdrop(jpeg((80, 120, 180))))

    def test_telegram_metadata_is_parsed(self):
        page = (
            '<meta name="twitter:description" content="Model: Pumpkin&#10;'
            'Backdrop: Onyx Black&#10;Symbol: Illuminati&#10;&#10;">'
            '<tr><th>Owner</th><td><a href="https://t.me/darkcollector">'
            '<span dir="auto">Dark Collector</span></a></td></tr>'
        )
        self.assertEqual(watch.parse_telegram_details(page), {
            "model": "Pumpkin",
            "backdrop": "Onyx Black",
            "symbol": "Illuminati",
            "owner": "Dark Collector",
            "owner_username": "darkcollector",
        })

    def test_telegram_owner_domain_is_converted_to_username(self):
        page = (
            '<tr><th>Owner</th><td><span class="tgme_gift_owner_address">'
            'z5zzz.t.me</span></td></tr>'
        )
        self.assertEqual(watch.parse_telegram_details(page), {
            "owner": "z5zzz.t.me",
            "owner_username": "z5zzz",
        })

    def test_plain_owner_name_is_not_invented_as_a_username(self):
        page = '<tr><th>Owner</th><td><span dir="auto">Ulyana</span></td></tr>'
        self.assertEqual(watch.parse_telegram_details(page), {"owner": "Ulyana"})

    def test_scan_advances_head_and_keeps_every_reported_backdrop(self):
        existence = {11: True, 12: True, 13: False}
        with patch.object(watch, "gift_exists", side_effect=lambda name, n: existence[n]), \
                patch.object(watch, "telegram_details", side_effect=[
                    {"model": "Dark Model", "backdrop": "Black", "symbol": "Star",
                     "owner": "Dark Collector", "owner_username": "darkcollector"},
                    {"model": "Blue Model", "backdrop": "Azure Blue", "symbol": "Moon"},
                ]):
            name, head, candidates = watch.scan_collection("Test Gift", 10, 20)
        self.assertEqual((name, head), ("Test Gift", 12))
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["number"], 11)
        self.assertEqual(candidates[0]["backdrop"], "Black")
        self.assertEqual(candidates[0]["owner_username"], "darkcollector")
        self.assertEqual(candidates[1]["backdrop"], "Azure Blue")

    def test_selected_backdrop_and_collection_floor_are_independent(self):
        candidate = {
            "collection": "Plush Pepe",
            "number": 123,
            "backdrop": "Azure Blue",
            "model": "Pumpkin",
            "collection_floor_ton": Decimal("12"),
        }
        color_only = {
            "dark_upgrades_enabled": True,
            "valuable_upgrades_enabled": False,
            "upgrade_backdrops": ["Azure Blue"],
        }
        floor_only = {
            "dark_upgrades_enabled": False,
            "valuable_upgrades_enabled": True,
            "upgrade_min_floor_ton": Decimal("10"),
            "upgrade_backdrops": [],
        }
        self.assertEqual(len(watch.candidate_reasons(candidate, color_only)), 1)
        self.assertIn("Выбранный фон", watch.candidate_reasons(candidate, color_only)[0])
        self.assertEqual(len(watch.candidate_reasons(candidate, floor_only)), 1)
        self.assertIn("12 TON", watch.candidate_reasons(candidate, floor_only)[0])

    def test_below_floor_and_unselected_backdrop_do_not_trigger(self):
        candidate = {
            "collection": "Plush Pepe", "number": 123,
            "backdrop": "Azure Blue", "collection_floor_ton": Decimal("9.99"),
        }
        config = {
            "dark_upgrades_enabled": True,
            "valuable_upgrades_enabled": True,
            "upgrade_backdrops": ["Black"],
            "upgrade_min_floor_ton": Decimal("10"),
        }
        self.assertEqual(watch.candidate_reasons(candidate, config), [])

    def test_collection_floor_uses_lowest_active_tonnel_listing(self):
        book = [
            {"status": "forsale", "asset": "TON", "auction": False,
             "dutchAuctionData": None, "premarketData": None, "underLoan": False,
             "gift_id": 1, "price": "12.5"},
            {"status": "forsale", "asset": "TON", "auction": False,
             "dutchAuctionData": None, "premarketData": None, "underLoan": False,
             "gift_id": 2, "price": "11.75"},
        ]
        with sqlite3.connect(":memory:") as db, \
                patch.object(watch, "load_collection_book", return_value=book):
            self.assertEqual(
                watch.collection_floor_ton(db, "Plush Pepe", {}), Decimal("11.75")
            )

    def test_alert_is_independent_per_chat_and_deduplicated(self):
        candidate = {
            "collection": "Plush Pepe",
            "number": 123,
            "backdrop": "Onyx Black",
            "model": "Pumpkin",
            "symbol": "Illuminati",
            "owner": "Dark Collector",
            "owner_username": "darkcollector",
        }
        profiles = [
            ("owner", {"dark_upgrades_enabled": True}),
            ("guest", {"dark_upgrades_enabled": True}),
        ]
        with sqlite3.connect(":memory:") as db, \
                patch.object(watch, "active_profiles", return_value=profiles), \
                patch.object(watch, "telegram", side_effect=[101, 202]) as send:
            watch.ensure_tables(db)
            self.assertEqual(
                watch.send_candidate(candidate, {}, {"chat_id": "owner"}, db), 2
            )
            self.assertEqual(
                watch.send_candidate(candidate, {}, {"chat_id": "owner"}, db), 0
            )
        self.assertEqual(send.call_count, 2)
        self.assertEqual(
            [call.args[0]["chat_id"] for call in send.call_args_list],
            ["owner", "guest"],
        )
        self.assertTrue(all(
            call.kwargs["button_text"] == "Открыть подарок"
            for call in send.call_args_list
        ))
        self.assertTrue(all(
            '👤 Улучшил: <a href="https://t.me/darkcollector">@darkcollector</a>'
            in call.args[1]
            for call in send.call_args_list
        ))

    def test_hidden_wallet_owner_is_not_exposed_as_a_nickname(self):
        candidate = {
            "collection": "Test Gift",
            "number": 1,
            "backdrop": "Black",
            "owner": "UQ" + "a" * 46,
        }
        self.assertIn("👤 Улучшил: <b>ник скрыт</b>", watch.format_alert(candidate))


if __name__ == "__main__":
    unittest.main()
