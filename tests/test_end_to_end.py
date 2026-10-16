"""The whole replay pipeline on synthetic traffic: fusion accuracy, a
detected injected went-dark, and no false alarms on clean traffic."""

import unittest

import support  # noqa: F401  (sets up sys.path)
from support import inject, simulate, traffic

accuracy = support.load("dt_accuracy", "fusion/accuracy.py")
# harness.py imports inject/detectors by bare name, as when run as a
# script; appended so fusion's `db` stays the one already imported.
support.sys.path.append(str(support.ROOT / "anomaly"))
harness = support.load("dt_harness", "anomaly/harness.py")


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ticks = traffic(n_aircraft=4, interval_s=10.0)
        cls.params = harness.scaled_params(cls.ticks)

    def test_window_cadence_is_measured(self):
        self.assertEqual(simulate.window_interval(self.ticks), 10.0)

    def test_every_aircraft_correctly_associated(self):
        contacts, stats, _ = accuracy.evaluate(self.ticks)
        self.assertEqual(stats["exact"], sum(t["source"] == "radar" for t in self.ticks))
        s = accuracy.summarize(contacts)
        self.assertEqual(s["aircraft_rate"], 1.0)
        self.assertLess(s["error_p95"], 150)

    def test_clean_traffic_raises_no_alerts(self):
        self.assertEqual(harness.run_scenario(self.ticks, self.params), [])

    def test_injected_went_dark_is_flagged_in_budget(self):
        modified, gt = inject.inject_went_dark(self.ticks, "TST2")
        budget = harness.default_latency_budget("went_dark", self.params)
        detections = [d for d in harness.run_scenario(modified, self.params)
                      if d["kind"] == "went_dark"]
        hit, false = harness.score(detections, gt, budget)
        self.assertIsNotNone(hit)
        self.assertEqual(false, [])


if __name__ == "__main__":
    unittest.main()
