import unittest
from decimal import Decimal

from alert_format import best_resale_market, format_market_alert


class AlertFormatTests(unittest.TestCase):
    def setUp(self):
        self.result = {
            "price": Decimal("4"),
            "rate": Decimal("100"),
            "basis": "модель: Pumpkin",
            "basis_scope": "model",
            "next_floor": Decimal("5"),
            "profit_rub": Decimal("90"),
            "resale": Decimal("4.9"),
            "liquidity": "средняя",
            "market_floors": [
                {"market": "Tonnel", "model_floor": Decimal("5"), "model_verified": True},
                {"market": "Portals", "model_floor": Decimal("5.2"), "model_verified": True},
                {"market": "MRKT", "model_floor": Decimal("5.1"), "model_verified": True},
            ],
        }
        self.item = {"model": "Pumpkin", "backdrop": "Black"}

    def test_best_resale_market_uses_net_floor_and_enabled_sources(self):
        config = {
            "tonnel_round_trip_cost_fraction": 0.01,
            "portals_round_trip_cost_fraction": 0.05,
            "mrkt_round_trip_cost_fraction": 0.01,
        }
        self.assertEqual(best_resale_market(self.result, config)["market"], "MRKT")
        config["mrkt_enabled"] = False
        self.assertEqual(best_resale_market(self.result, config)["market"], "Tonnel")

    def test_alert_contains_resale_recommendation(self):
        text = format_market_alert(
            self.item, self.result, market="Tonnel",
            config={"portals_round_trip_cost_fraction": 0.02},
        )
        self.assertIn("Где выгоднее перепродать", text)
        self.assertIn("Portals", text)

    def test_alert_labels_direct_model_floors_and_missing_listings(self):
        self.result["market_floors"][1]["model_floor"] = None
        text = format_market_alert(self.item, self.result, market="Tonnel")
        self.assertIn("Floor модели по площадкам", text)
        self.assertIn("нет активных лотов", text)
        self.assertNotIn("нет устойчивых данных", text)


if __name__ == "__main__":
    unittest.main()
