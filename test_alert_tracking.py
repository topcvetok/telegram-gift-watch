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
            db, "Portals", item, {"price": Decimal("8")}, 321
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
        self.assertNotIn("Устойчивые floor", text)
        self.assertNotIn("Ликвидность", text)

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


if __name__ == "__main__":
    unittest.main()
