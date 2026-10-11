import unittest
from connect_bot import matching_chat


class BindingTests(unittest.TestCase):
    def test_binds_only_exact_challenge_in_private_chat(self):
        def update(text, kind, chat_id=123, sender_id=123):
            return {"message": {"text": text, "chat": {"type": kind, "id": chat_id},
                                "from": {"id": sender_id}}}
        self.assertIsNone(matching_chat([update("/start", "private")], "bind_test"))
        self.assertIsNone(matching_chat([update("/start bind_test", "group")], "bind_test"))
        self.assertIsNone(matching_chat([update("/start bind_test", "private", 123, 456)], "bind_test"))
        self.assertEqual(matching_chat([update("/start bind_test", "private")], "bind_test"), "123")


if __name__ == "__main__":
    unittest.main()
