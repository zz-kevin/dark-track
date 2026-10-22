"""The realistic radar (ingestion/radar_model.py) and fusion's use of it."""

import math
import random
import unittest

from support import T0, engine, flight, radar_model, radar_truth, simulate

GULF = radar_model.SITES["gulf"]


def at_range(site, km, bearing_deg=90.0, alt_m=9000.0):
    """One ADS-B fix km from the site, no velocity (so latency doesn't move it)."""
    e = km * 1000 * math.sin(math.radians(bearing_deg))
    n = km * 1000 * math.cos(math.radians(bearing_deg))
    lat, lon = radar_model._latlon(site, e, n)
    return {"id": 7, "source": "adsb", "external_id": "AAA1", "lat": lat, "lon": lon,
            "altitude_m": alt_m, "velocity_mps": None, "heading_deg": None, "observed_at": T0}


class AccuracyTest(unittest.TestCase):
    def test_cross_range_error_grows_with_range(self):
        near, _ = radar_model.measurement_std(GULF, 20_000)
        far, far_h = radar_model.measurement_std(GULF, 200_000)
        self.assertLess(near, 60)
        self.assertAlmostEqual(far, math.sqrt((50 ** 2 + (200_000 * math.radians(0.15)) ** 2) / 2),
                               places=6)
        self.assertGreater(far_h, 800)

    def test_reported_accuracy_matches_actual_error(self):
        rng = random.Random(3)
        fix = at_range(GULF, 150)
        errs, std = [], None
        for _ in range(3000):
            c = radar_model.contact(GULF, fix, rng)
            if c is None:
                continue
            std = c["noise_std_m"]
            e, n = radar_model._enu(GULF, c["lat"], c["lon"])
            e0, n0 = radar_model._enu(GULF, fix["lat"], fix["lon"])
            errs.append((e - e0) ** 2 + (n - n0) ** 2)
        per_axis_rms = math.sqrt(sum(errs) / len(errs) / 2)
        self.assertAlmostEqual(per_axis_rms, std, delta=0.05 * std)


class CoverageTest(unittest.TestCase):
    def detections(self, fix, n=500):
        rng = random.Random(1)
        return sum(radar_model.contact(GULF, fix, rng) is not None for _ in range(n)) / n

    def test_near_targets_are_seen(self):
        self.assertAlmostEqual(self.detections(at_range(GULF, 50)), GULF.pd_near, delta=0.04)

    def test_beyond_max_range_is_not_seen(self):
        self.assertEqual(self.detections(at_range(GULF, 380, alt_m=12000)), 0.0)

    def test_horizon_hides_low_aircraft(self):
        # 250 km out: a high aircraft is above the horizon, a low one isn't.
        self.assertGreater(self.detections(at_range(GULF, 250, alt_m=11000)), 0.3)
        self.assertEqual(self.detections(at_range(GULF, 250, alt_m=500)), 0.0)
        self.assertEqual(self.detections(at_range(GULF, 120, alt_m=None)), 0.0)


class ClutterTest(unittest.TestCase):
    def test_rate_and_truth(self):
        rng = random.Random(2)
        rows = radar_model.clutter(GULF, T0, 3600, rng)
        self.assertAlmostEqual(len(rows) / 60, GULF.clutter_per_min, delta=1.5)
        self.assertTrue(all(r["truth_tick_id"] == radar_model.CLUTTER for r in rows))
        self.assertTrue(all(math.hypot(*radar_model._enu(GULF, r["lat"], r["lon"]))
                            <= GULF.max_range_m for r in rows))

    def test_radar_truth_counts_clutter_without_labelling_it(self):
        ticks = radar_model.regenerate(flight("AAA1", 25.9, 51.8, 90.0, 220.0), "gulf")
        labels, stats = radar_truth.label(ticks)
        clutter = [t for t in ticks if t.get("truth_tick_id") == radar_model.CLUTTER]
        self.assertGreater(len(clutter), 0)
        self.assertEqual(stats["clutter"], len(clutter))
        self.assertFalse(any(t["id"] in labels for t in clutter))
        self.assertEqual(stats["exact"], len(labels))


class RegenerateTest(unittest.TestCase):
    def test_replaces_stored_radar_deterministically(self):
        adsb = flight("AAA1", 25.9, 51.8, 90.0, 220.0)
        stored = adsb + [{**adsb[0], "id": 99999, "source": "radar", "external_id": None}]
        a = radar_model.regenerate(stored, "gulf", seed=4)
        b = radar_model.regenerate(stored, "gulf", seed=4)
        self.assertEqual(a, b)
        self.assertNotIn(99999, {t["id"] for t in a})
        radar = [t for t in a if t["source"] == "radar"]
        self.assertTrue(all(t["id"] < 0 and t["noise_std_m"] > 0 for t in radar))
        self.assertEqual([t for t in a if t["source"] == "adsb"], adsb)

    def test_same_contacts_whatever_order_ties_arrive_in(self):
        # Two aircraft reporting at the same instants: the database returns
        # such ties in no fixed order.
        adsb = flight("AAA1", 25.9, 51.8, 90.0, 220.0) + flight(
            "BBB2", 26.0, 51.8, 90.0, 220.0, first_id=1000)
        a = radar_model.regenerate(sorted(adsb, key=lambda t: (t["observed_at"], t["id"])), "gulf")
        b = radar_model.regenerate(sorted(adsb, key=lambda t: (t["observed_at"], -t["id"])), "gulf")
        self.assertEqual(a, b)

    def test_site_from_the_data(self):
        self.assertIs(radar_model.site_for(flight("A", 40.7, -73.9, 0, 200)),
                      radar_model.SITES["nyc"])
        self.assertIs(radar_model.site_for(flight("A", 26.0, 52.0, 0, 200)), GULF)


class FusionTest(unittest.TestCase):
    def test_contact_accuracy_is_the_measurement_noise(self):
        row = {"id": 1, "external_id": None, "lat": 25.0, "lon": 53.0, "observed_at": T0,
               "noise_std_m": 420.0}
        self.assertEqual(engine.tick_from_row(row, "radar", 150.0)["r_std"], 420.0)
        self.assertEqual(engine.tick_from_row({**row, "noise_std_m": None}, "radar", 150.0)["r_std"],
                         150.0)

    def test_far_contacts_stay_on_their_aircraft(self):
        # Two aircraft 3 km apart, ~180 km from the radar (contacts ~340 m
        # per axis), plus clutter: the radar contacts still land on their own
        # aircraft's track.
        adsb = (flight("AAA1", 25.9, 53.4, 90.0, 220.0)
                + flight("BBB2", 25.927, 53.4, 90.0, 220.0, first_id=1000))
        ticks = radar_model.regenerate(adsb, "gulf", seed=1)
        labels, _ = radar_truth.label(ticks)
        right = wrong = 0
        for cycle in simulate.run(ticks, simulate.scaled_params(ticks)):
            for e in cycle["events"]:
                if e["source"] == "radar" and e["raw_tick_id"] in labels:
                    track = cycle["tracks"].get(e["track_id"])
                    ok = track is not None and track.external_id == labels[e["raw_tick_id"]]["external_id"]
                    right += ok
                    wrong += not ok
        self.assertGreater(right, 0)
        self.assertGreaterEqual(right / (right + wrong), 0.95)


if __name__ == "__main__":
    unittest.main()
