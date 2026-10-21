import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

import history_store


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.secrets = {
            "history_api_url": "https://history.example.test",
            "history_writer_token": "writer-secret-which-is-long-enough",
        }

    def test_access_tokens_are_stable_and_private_per_chat(self):
        first = history_store.access_token(self.secrets, "42")
        self.assertEqual(first, history_store.access_token(self.secrets, "42"))
        self.assertNotEqual(first, history_store.access_token(self.secrets, "99"))
        self.assertNotIn("42", first)

    def test_failed_delivery_stays_in_outbox_and_retries(self):
        with sqlite3.connect(":memory:") as db, \
                patch.object(history_store, "_post", side_effect=[False, True]) as post:
            self.assertTrue(history_store.queue_history(db, self.secrets, "42", {
                "kind": "offer", "event_key": "Tonnel:1", "title": "Gift #1",
            }))
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM history_outbox").fetchone()[0], 1
            )
            self.assertEqual(history_store.flush_outbox(db, self.secrets), 1)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM history_outbox").fetchone()[0], 0
            )
        payload = post.call_args_list[0].args[1]
        self.assertEqual(payload["chat_id"], "42")
        self.assertGreaterEqual(len(payload["access_token"]), 32)

    def test_post_uses_writer_header_without_putting_it_in_body(self):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(history_store.urllib.request, "urlopen", return_value=response) as open_url:
            self.assertTrue(history_store._post(self.secrets, {"title": "Gift"}))
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("X-writer-token"), self.secrets["history_writer_token"])
        self.assertNotIn(self.secrets["history_writer_token"], request.data.decode())

    def test_sold_update_waits_behind_original_offer(self):
        with sqlite3.connect(":memory:") as db, \
                patch.object(history_store, "_post", side_effect=[False, False, True, True]) as post:
            history_store.queue_history(db, self.secrets, "42", {
                "kind": "offer", "event_key": "Tonnel:1", "title": "Gift #1",
            })
            history_store.mark_offer_sold(db, self.secrets, "42", "Tonnel", "1")
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM history_outbox").fetchone()[0], 2
            )
            self.assertEqual(history_store.flush_outbox(db, self.secrets), 2)
        self.assertEqual(post.call_args_list[-1].args[1]["action"], "sold")


if __name__ == "__main__":
    unittest.main()
