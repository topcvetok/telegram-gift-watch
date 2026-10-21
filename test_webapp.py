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


if __name__ == "__main__":
    unittest.main()
