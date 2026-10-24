
import unittest

from p2.density import calculate_density, classify_density, analyze_zone
from p2.prediction import CrowdPredictor


class TestDensity(unittest.TestCase):

    def test_density_calculation(self):
        self.assertEqual(calculate_density(30, 10), 3.0)

    def test_safe_zone(self):
        self.assertEqual(classify_density(3.0), "SAFE")

    def test_warning_zone(self):
        self.assertEqual(classify_density(5.0), "WARNING")

    def test_critical_zone(self):
        self.assertEqual(classify_density(7.0), "CRITICAL")

    def test_zone_analysis(self):
        result = analyze_zone(50, 10)
        self.assertEqual(result["density"], 5.0)
        self.assertEqual(result["status"], "WARNING")

    def test_zero_area_rejected(self):
        with self.assertRaises(ValueError):
            calculate_density(10, 0)


class TestPrediction(unittest.TestCase):

    def test_critical_density_has_zero_time(self):
        predictor = CrowdPredictor()
        result = predictor.update("Gate 1", 6.0, 0)
        self.assertEqual(result["seconds_to_critical"], 0.0)
        self.assertEqual(result["message"], "CRITICAL NOW")

    def test_first_measurement_collects_data(self):
        predictor = CrowdPredictor()
        result = predictor.update("Gate 1", 2.0, 0)
        self.assertIsNone(result["minutes_to_critical"])
        self.assertEqual(result["message"], "COLLECTING DATA")

    def test_no_prediction_from_a_few_seconds_of_noise(self):
        predictor = CrowdPredictor()
        for t, d in [(0.0, 3.0), (0.5, 3.4), (1.0, 3.9)]:
            result = predictor.update("Gate 1", d, t)
        self.assertIsNone(result["minutes_to_critical"])

    def test_rising_trend_predicts_time_to_critical(self):
        predictor = CrowdPredictor()
        for t in range(0, 61, 5):  # +1 person/m² per minute, from 2.0
            result = predictor.update("Gate 1", 2.0 + t / 60, t)
        self.assertEqual(result["message"], "RISK INCREASING")
        self.assertAlmostEqual(result["minutes_to_critical"], 3.0, places=1)

    def test_a_slow_creep_is_not_a_warning(self):
        predictor = CrowdPredictor()
        for t in range(0, 61, 5):  # +0.01 person/m² per minute
            result = predictor.update("Gate 1", 1.0 + t / 6000, t)
        self.assertIsNone(result["minutes_to_critical"])
        self.assertEqual(result["message"], "NO NEAR-TERM RISK")


class TestPeopleHeatmap(unittest.TestCase):

    def test_packed_people_glow_more_than_a_lone_person(self):
        import numpy as np
        from p2.heatmap import people_heatmap

        frame = np.full((200, 400, 3), 80, np.uint8)
        lone = [(40, 40, 70, 120)]
        packed = [(250 + dx, 40 + dy, 280 + dx, 120 + dy) for dx in (0, 15, 30) for dy in (0, 10)]
        out = people_heatmap(frame, lone + packed).astype(int)
        change = np.abs(out - 80).sum(axis=2)
        self.assertGreater(change[90:110, 270:300].mean(), 2 * change[90:110, 45:65].mean())
        self.assertEqual(change[180:, :].max(), 0)  # nobody there, nothing drawn

    def test_mask_limits_the_colour(self):
        import numpy as np
        from p2.heatmap import people_heatmap

        frame = np.full((100, 100, 3), 80, np.uint8)
        mask = np.zeros((100, 100), bool)
        out = people_heatmap(frame, [(30, 20, 60, 90)], mask=mask)
        self.assertTrue(np.array_equal(out, frame))


if __name__ == "__main__":
    unittest.main(verbosity=2)
