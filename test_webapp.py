import unittest
from pathlib import Path


class WebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parent / "webapp" / "index.html").read_text()

    def test_bottom_navigation_contains_all_sections(self):
        self.assertIn('data-view="settings"', self.source)
        self.assertIn('data-view="upgrades"', self.source)
        self.assertIn('data-view="offers"', self.source)
        self.assertIn('id="history_upgrades"', self.source)
        self.assertIn('id="history_offers"', self.source)
        self.assertIn('data-more="upgrade"', self.source)
        self.assertIn('data-more="offer"', self.source)

    def test_history_token_is_sent_in_post_body(self):
        self.assertIn("action:'read',kind,token:historyToken,offset", self.source)
        self.assertNotIn("searchParams.set('token'", self.source)

    def test_settings_use_durable_backend_instead_of_telegram_send_data(self):
        self.assertIn("action:'settings_read',token:historyToken", self.source)
        self.assertIn("action:'settings_save',token:historyToken,settings", self.source)
        self.assertNotIn("tg.sendData(payload)", self.source)
        self.assertIn("'max_lot','min_floor'", self.source)


if __name__ == "__main__":
    unittest.main()
