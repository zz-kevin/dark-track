"""Detectors and anomaly injection."""

import math
import unittest
from datetime import timedelta

from support import T0, detectors, flight, inject, radar_truth, with_radar


def _snap(adsb_age, radar_age, alt=9000.0, adsb_hits=5):
    return {"track_id": 1, "external_id": "ABC1", "adsb_hits": adsb_hits,
            "last_adsb_at": T0 - timedelta(seconds=adsb_age) if adsb_age is not None else None,
            "last_radar_at": T0 - timedelta(seconds=radar_age), "last_adsb_alt_m": alt}


class WentDarkTest(unittest.TestCase):
    def test_fires_when_adsb_silent_and_radar_fresh(self):
        hits = detectors.went_dark([_snap(40, 2)], T0, 30, 10)
        self.assertEqual([h["track_id"] for h in hits], [1])
        self.assertAlmostEqual(hits[0]["metric"], 40)

    def test_quiet_cases(self):
        for name, snap in [("adsb recent", _snap(20, 2)),
                           ("radar stale", _snap(40, 20)),
                           ("on the ground", _snap(40, 2, alt=None)),
                           ("below airborne floor", _snap(40, 2, alt=50.0)),
                           ("never on adsb", _snap(None, 2, adsb_hits=0))]:
            with self.subTest(name):
                self.assertEqual(detectors.went_dark([snap], T0, 30, 10), [])

    def test_thresholds_scale_with_cadence_above_floors(self):
        self.assertEqual(detectors.went_dark_thresholds(10), (30.0, 13.0))
        self.assertEqual(detectors.went_dark_thresholds(45), (67.5, 58.5))


class KinematicTest(unittest.TestCase):
    def _event(self, i, normalized):
        return {"track_id": 1, "source": "adsb", "observed_at": T0 + timedelta(seconds=i),
                "residual_m": normalized * 100.0, "innovation_std_m": 100.0}

    def test_needs_consecutive_and_fires_once(self):
        state = {}
        fired = []
        for i, n in enumerate([5, 1, 5, 5, 5, 1]):
            detected, resolved, state = detectors.kinematic_inconsistency(
                [self._event(i, n)], state, sigma_mult=4.0, consecutive=2)
            fired.append((len(detected), len(resolved)))
        self.assertEqual(fired, [(0, 0), (0, 0), (0, 0), (1, 0), (0, 0), (0, 1)])


class InjectTest(unittest.TestCase):
    def setUp(self):
        self.adsb = flight("TGT1", 25.0, 53.0, 90.0, 220.0) + flight(
            "OTH1", 25.5, 53.0, 90.0, 220.0, first_id=1000)
        self.ticks = with_radar(self.adsb, seed=3)

    def test_went_dark_drops_only_the_targets_later_adsb(self):
        modified, gt = inject.inject_went_dark(self.ticks, "TGT1")
        cut = gt["injected_at"]
        tgt_after = [t for t in modified if t["external_id"] == "TGT1" and t["observed_at"] > cut]
        self.assertEqual(tgt_after, [])
        self.assertEqual(sum(t["external_id"] == "OTH1" for t in modified),
                         sum(t["external_id"] == "OTH1" for t in self.ticks))
        self.assertEqual(sum(t["source"] == "radar" for t in modified),
                         sum(t["source"] == "radar" for t in self.ticks))

    def test_kinematic_moves_both_sensors_consistently_and_reproducibly(self):
        sources, _ = radar_truth.label(self.ticks)
        a, gt = inject.inject_kinematic_jump(self.ticks, "TGT1", radar_sources=sources, seed=11)
        b, _ = inject.inject_kinematic_jump(self.ticks, "TGT1", radar_sources=sources, seed=11)
        self.assertEqual([(t["lat"], t["lon"]) for t in a], [(t["lat"], t["lon"]) for t in b])

        before = {t["id"]: t for t in self.ticks}
        tgt_radar = {rid for rid, s in sources.items() if s["external_id"] == "TGT1"}
        moved_radar = [t for t in a if t["id"] in tgt_radar and t["observed_at"] > gt["injected_at"]
                       and (t["lat"], t["lon"]) != (before[t["id"]]["lat"], before[t["id"]]["lon"])]
        self.assertTrue(moved_radar, "target's radar contacts didn't move with it")
        last = max((t for t in a if t["external_id"] == "TGT1"), key=lambda t: t["observed_at"])
        elapsed = (last["observed_at"] - gt["injected_at"]).total_seconds()
        dv = math.hypot(  # velocity change should be a * t
            last["velocity_mps"] * math.sin(math.radians(last["heading_deg"])) - 220.0,
            last["velocity_mps"] * math.cos(math.radians(last["heading_deg"])))
        self.assertAlmostEqual(dv, 20.0 * elapsed, delta=1e-6)
        self.assertFalse(any(t["external_id"] == "OTH1" and t is not before[t["id"]]
                             and (t["lat"], t["lon"]) != (before[t["id"]]["lat"], before[t["id"]]["lon"])
                             for t in a))


if __name__ == "__main__":
    unittest.main()
