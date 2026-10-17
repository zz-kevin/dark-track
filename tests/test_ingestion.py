"""Regions and radar ground truth."""

import unittest

from support import flight, radar_truth, regions, with_radar


class RegionsTest(unittest.TestCase):
    def test_region_of(self):
        self.assertEqual(regions.region_of(40.7, -73.9), "nyc")
        self.assertEqual(regions.region_of(25.3, 53.5), "gulf")
        self.assertIsNone(regions.region_of(51.5, -0.1))

    def test_pick(self):
        ticks = [{"lat": 40.7, "lon": -73.9}, {"lat": 25.3, "lon": 53.5}]
        with self.assertRaisesRegex(ValueError, "mixes regions"):
            regions.pick(ticks)
        self.assertEqual(regions.pick(ticks, "gulf"), [ticks[1]])
        self.assertEqual(regions.pick(ticks[:1]), ticks[:1])
        with self.assertRaises(ValueError):
            regions.pick(ticks, "mars")


class RadarTruthTest(unittest.TestCase):
    def _two_close_aircraft(self, record_truth):
        # Same route 2 km apart in trail, different altitudes.
        adsb = flight("AAA1", 25.0, 53.0, 90.0, 220.0, alt_m=9000.0) + flight(
            "BBB2", 25.0, 52.98, 90.0, 220.0, alt_m=9300.0, first_id=1000)
        return with_radar(adsb, seed=5, record_truth=record_truth)

    def _check(self, ticks, expect_exact):
        labels, stats = radar_truth.label(ticks)
        radar = [t for t in ticks if t["source"] == "radar"]
        by_id = {t["id"]: t for t in ticks}
        truth = {r["id"]: by_id[r["truth_tick_id"]] for r in radar
                 if r.get("truth_tick_id") is not None}
        if expect_exact:
            self.assertEqual(stats["exact"], len(radar))
        else:
            self.assertGreaterEqual(stats["inferred"], 0.98 * len(radar))
        return labels

    def test_exact_truth_column(self):
        self._check(self._two_close_aircraft(record_truth=True), expect_exact=True)

    def test_inference_recovers_the_source_aircraft(self):
        with_truth = self._two_close_aircraft(record_truth=True)
        truth = {t["id"]: t["truth_tick_id"] for t in with_truth if t["source"] == "radar"}
        inferred = self._check(self._two_close_aircraft(record_truth=False), expect_exact=False)
        by_id = {t["id"]: t for t in with_truth}
        wrong = [rid for rid, src in inferred.items()
                 if src["external_id"] != by_id[truth[rid]]["external_id"]]
        self.assertEqual(wrong, [])


class RadarSimTest(unittest.TestCase):
    def test_height_error_is_applied_and_recorded(self):
        import random
        from support import radar_sim
        src = flight("AAA1", 25.0, 53.0, 90.0, 220.0, minutes=1)[0]
        rng = random.Random(1)
        rows = [radar_sim.make_radar_row(src, 150.0, 1.0, 4.0, rng, 300.0) for _ in range(400)]
        errs = [r["altitude_m"] - src["altitude_m"] for r in rows]
        self.assertTrue(all(r["altitude_std_m"] == 300.0 for r in rows))
        self.assertAlmostEqual((sum(e * e for e in errs) / len(errs)) ** 0.5, 300.0, delta=40)
        ground = radar_sim.make_radar_row({**src, "altitude_m": None}, 150.0, 1.0, 4.0, rng)
        self.assertIsNone(ground["altitude_m"])
        self.assertIsNone(ground["altitude_std_m"])


if __name__ == "__main__":
    unittest.main()
