import io
import json
import sqlite3
import tempfile
import unittest
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
        )
        self.assertEqual(watch.parse_telegram_details(page), {
            "model": "Pumpkin",
            "backdrop": "Onyx Black",
            "symbol": "Illuminati",
        })

    def test_scan_advances_head_and_keeps_only_target_backdrops(self):
        existence = {11: True, 12: True, 13: False}
        images = {
            11: jpeg((14, 15, 15)),
            12: jpeg((80, 120, 180)),
        }
        with patch.object(watch, "gift_exists", side_effect=lambda name, n: existence[n]), \
                patch.object(watch, "gift_image", side_effect=lambda name, n: images[n]), \
                patch.object(watch, "telegram_details", return_value={
                    "model": "Dark Model", "backdrop": "Black", "symbol": "Star",
                }):
            name, head, candidates = watch.scan_collection("Test Gift", 10, 20)
        self.assertEqual((name, head), ("Test Gift", 12))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["number"], 11)
        self.assertEqual(candidates[0]["backdrop"], "Black")

    def test_alert_is_independent_per_chat_and_deduplicated(self):
        candidate = {
            "collection": "Plush Pepe",
            "number": 123,
            "backdrop": "Onyx Black",
            "model": "Pumpkin",
            "symbol": "Illuminati",
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


if __name__ == "__main__":
    unittest.main()
