import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import patch

import alert_tracking


class AlertTrackingTests(unittest.TestCase):
    def test_sold_offer_replaces_original_message_once(self):
        db = sqlite3.connect(":memory:")
        item = {
            "gift_id": "gift-1", "gift_num": 42, "name": "Evil Eye",
            "model": "Watcher",
        }
        self.assertTrue(alert_tracking.remember_alert(
            db,
            "Portals",
            item,
            {"price": Decimal("8")},
            321,
            preview_url="https://t.me/nft/evileye-42",
        ))
        with patch.object(alert_tracking, "telegram_edit", return_value=True) as edit:
            self.assertTrue(alert_tracking.mark_offer_sold(
                db, "Portals", "gift-1", {}, sale_price=Decimal("8")
            ))
            self.assertFalse(alert_tracking.mark_offer_sold(
                db, "Portals", "gift-1", {}, sale_price=Decimal("8")
            ))
        edit.assert_called_once()
        self.assertEqual(edit.call_args.args[1], 321)
        text = edit.call_args.args[2]
        self.assertIn("<b>ПРОДАНО</b>", text)
        self.assertIn("Evil Eye #42", text)
        self.assertIn("Модель: Watcher", text)
        self.assertIn("https://t.me/nft/evileye-42", text)
        self.assertNotIn("Устойчивые floor", text)
        self.assertNotIn("Ликвидность", text)
        self.assertEqual(
            edit.call_args.kwargs["preview_url"],
            "https://t.me/nft/evileye-42",
        )

    def test_existing_alert_table_is_migrated_and_preview_is_reconstructed(self):
        db = sqlite3.connect(":memory:")
        db.execute(
            "CREATE TABLE offer_messages ("
            "market TEXT NOT NULL, gift_id TEXT NOT NULL, message_id INTEGER NOT NULL, "
            "offer_price TEXT NOT NULL, gift_name TEXT, gift_num TEXT, model TEXT, "
            "sent REAL NOT NULL, sold INTEGER NOT NULL DEFAULT 0, "
            "checked REAL NOT NULL DEFAULT 0, sold_at REAL, "
            "PRIMARY KEY (market, gift_id))"
        )
        db.execute(
            "INSERT INTO offer_messages VALUES "
            "('Tonnel', 'gift-2', 322, '4', 'Plush Pepe', '1098', 'Green', 1, 0, 0, NULL)"
        )
        with patch.object(alert_tracking, "telegram_edit", return_value=True) as edit:
            self.assertTrue(alert_tracking.mark_offer_sold(
                db, "Tonnel", "gift-2", {}, sale_price=Decimal("4")
            ))
        self.assertEqual(
            edit.call_args.kwargs["preview_url"],
            "https://t.me/nft/plushpepe-1098",
        )
        self.assertIn("https://t.me/nft/plushpepe-1098", edit.call_args.args[2])

    def test_pending_checks_are_rate_limited(self):
        db = sqlite3.connect(":memory:")
        alert_tracking.remember_alert(
            db, "Portals", {"gift_id": "one", "name": "Gift"}, {"price": 1}, 7
        )
        self.assertEqual(alert_tracking.pending_offer_ids(
            db, "Portals", interval=30, now=100
        ), ["one"])
        self.assertEqual(alert_tracking.pending_offer_ids(
            db, "Portals", interval=30, now=110
        ), [])

    def test_sold_offer_is_updated_for_each_recipient(self):
        db = sqlite3.connect(":memory:")
        item = {
            "gift_id": "gift-shared", "gift_num": 12,
            "name": "Shared Gift", "model": "Blue",
        }
        result = {"price": Decimal("5")}
        alert_tracking.remember_alert(
            db, "Tonnel", item, result, 100, chat_id="42"
        )
        alert_tracking.remember_alert(
            db, "Tonnel", item, result, 200, chat_id="99"
        )
        with patch.object(alert_tracking, "telegram_edit", return_value=True) as edit:
            with patch.object(alert_tracking, "mark_history_offer_sold") as history:
                self.assertTrue(alert_tracking.mark_offer_sold(
                    db, "Tonnel", "gift-shared", {"chat_id": "42"}
                ))
        recipients = {call.args[0]["chat_id"] for call in edit.call_args_list}
        self.assertEqual(recipients, {"42", "99"})
        self.assertEqual({call.args[2] for call in history.call_args_list}, {"42", "99"})


if __name__ == "__main__":
    unittest.main()
