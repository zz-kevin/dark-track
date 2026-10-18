"""Kalman filter, gating/association, and the engine's per-cycle logic."""

import math
import random
import unittest
from datetime import timedelta

import numpy as np

from support import T0, Track, associate, engine, kalman


def _track(x, y, vx=0.0, vy=0.0, pos_var=400.0, vel_var=25.0, at=T0, external_id=None):
    t = Track(x, y, at, "adsb", 3.0, pos_var, external_id=external_id, v0=(vx, vy),
              init_vel_var=vel_var)
    return t


def _tick(x, y, at=T0, r_std=150.0, source="radar", raw_id=1):
    return {"raw_id": raw_id, "external_id": None, "x": x, "y": y, "observed_at": at,
            "r_std": r_std, "source": source, "v0": None, "altitude_m": 9000.0,
            "velocity_lead_s": 0.0}


class KalmanTest(unittest.TestCase):
    def test_predict_moves_along_velocity(self):
        kf = kalman.KalmanFilter(0, 0, 3.0, 100.0, v0=(200.0, 0.0))
        x, P = kf.peek_predict(10.0)
        self.assertAlmostEqual(x[0], 2000.0)
        self.assertGreater(P[0, 0], 100.0)

    def test_negative_dt_is_clamped(self):
        kf = kalman.KalmanFilter(0, 0, 3.0, 100.0, v0=(200.0, 0.0))
        x, _ = kf.peek_predict(-5.0)
        self.assertEqual(x[0], 0.0)

    def test_update_shrinks_uncertainty_toward_trusted_source(self):
        kf = kalman.KalmanFilter(0, 0, 3.0, 150.0 ** 2)
        kf.update((100.0, 0.0), 20.0)
        self.assertGreater(kf.position[0], 90.0)
        self.assertLess(kf.position_variance, 2 * 150.0 ** 2)

    def test_velocity_measurement_pins_velocity(self):
        kf = kalman.KalmanFilter(0, 0, 3.0, 2500.0, init_vel_var=2500.0)
        kf.update((0.0, 0.0), 50.0, v=(230.0, -10.0), rv_std=5.0)
        vx, vy = kf.velocity
        self.assertAlmostEqual(vx, 230.0, delta=3.0)
        self.assertAlmostEqual(vy, -10.0, delta=3.0)

    def test_position_only_update_returns_2d_innovation(self):
        kf = kalman.KalmanFilter(0, 0, 3.0, 100.0)
        self.assertEqual(kf.update((5.0, 5.0), 20.0).shape, (2,))
        self.assertEqual(kf.update((5.0, 5.0), 20.0, v=(1.0, 1.0), rv_std=5.0).shape, (2,))


class AssociateTest(unittest.TestCase):
    def setUp(self):
        self._cost = associate.COST

    def tearDown(self):
        associate.COST = self._cost

    def test_matches_nearby_track_and_rejects_far(self):
        tracks = {1: _track(0, 0)}
        res = associate.associate([_tick(100, 0), _tick(50_000, 0, raw_id=2)], tracks, 800, 30)
        self.assertEqual(res, [1, None])

    def test_stale_track_is_not_a_candidate(self):
        tracks = {1: _track(0, 0)}
        res = associate.associate([_tick(10, 0, at=T0 + timedelta(seconds=60))], tracks, 800, 30)
        self.assertEqual(res, [None])

    def test_one_to_one(self):
        tracks = {1: _track(0, 0)}
        res = associate.associate([_tick(10, 0), _tick(-10, 0, raw_id=2)], tracks, 800, 30)
        self.assertEqual(sorted(r is None for r in res), [False, True])

    def test_likelihood_prefers_uncertain_track_that_explains_the_contact(self):
        """The went-dark case: a dark track whose 45s prediction is ~1.7 km
        off but ~4 km uncertain vs. a neighbour updated seconds ago, 600 m
        away with a tight covariance."""
        dark = _track(-1700, 0, pos_var=4000.0 ** 2 / 2, vel_var=0.0)
        neighbour = _track(600, 0, pos_var=30.0 ** 2, vel_var=0.0)
        tracks = {1: dark, 2: neighbour}
        associate.COST = "likelihood"
        self.assertEqual(associate.associate([_tick(0, 0)], tracks, 800, 90), [1])
        associate.COST = "euclidean"
        self.assertEqual(associate.associate([_tick(0, 0)], tracks, 800, 90), [2])

    def test_vectorized_gating_matches_per_pair_reference(self):
        rng = random.Random(7)
        for trial in range(20):
            tracks = {}
            for tid in range(rng.randint(1, 12)):
                t = _track(rng.uniform(-5000, 5000), rng.uniform(-5000, 5000),
                           rng.uniform(-250, 250), rng.uniform(-250, 250),
                           pos_var=rng.uniform(100, 1e6), vel_var=rng.uniform(1, 2500),
                           at=T0 - timedelta(seconds=rng.uniform(0, 60)))
                tracks[tid] = t
            ticks = [_tick(rng.uniform(-6000, 6000), rng.uniform(-6000, 6000), raw_id=i)
                     for i in range(rng.randint(1, 10))]
            got = associate.associate(ticks, tracks, 800, 45)
            for tick, tid in zip(ticks, got):
                if tid is None:
                    continue
                self.assertTrue(_gated_reference(tick, tracks[tid], 800, 45),
                                f"trial {trial}: assigned an ungated pair")


def _gated_reference(tick, track, max_dist, max_age):
    """The original per-pair gate, via KalmanFilter.peek_predict."""
    age = (tick["observed_at"] - track.last_update).total_seconds()
    if abs(age) > max_age:
        return False
    x, P = track.kf.peek_predict(age)
    d = np.array([tick["x"] - x[0], tick["y"] - x[1]])
    if math.hypot(*d) <= max_dist:
        return True
    S = P[:2, :2] + np.eye(2) * tick["r_std"] ** 2
    return float(d @ np.linalg.solve(S, d)) <= associate.CHI2_GATE


class AltitudeTest(unittest.TestCase):
    def setUp(self):
        self._alt = associate.USE_ALTITUDE

    def tearDown(self):
        associate.USE_ALTITUDE = self._alt

    def _stack(self):
        """Two tracks the same distance from the contact, 2 km apart in
        height -- a holding stack."""
        low, high = _track(-300, 0), _track(300, 0)
        low.observe_altitude(5000.0, 15.0, T0)
        high.observe_altitude(7000.0, 15.0, T0)
        tick = {**_tick(0, 0), "alt": 7050.0, "alt_std": 300.0}
        return {1: low, 2: high}, tick

    def test_altitude_picks_the_right_level(self):
        tracks, tick = self._stack()
        associate.USE_ALTITUDE = True
        self.assertEqual(associate.associate([tick], tracks, 800, 30), [2])

    def test_altitude_gate_rejects_wrong_level(self):
        tracks, tick = self._stack()
        del tracks[2]
        associate.USE_ALTITUDE = True
        self.assertEqual(associate.associate([tick], tracks, 800, 30), [None])
        associate.USE_ALTITUDE = False
        self.assertEqual(associate.associate([tick], tracks, 800, 30), [1])

    def test_track_without_altitude_does_not_win_by_default(self):
        tracks, tick = self._stack()
        tracks[1] = _track(-300, 0)  # no altitude estimate at all
        associate.USE_ALTITUDE = True
        self.assertEqual(associate.associate([tick], tracks, 800, 30), [2])

    def test_contact_without_altitude_is_scored_horizontally(self):
        tracks, tick = self._stack()
        tick["alt"] = None
        associate.USE_ALTITUDE = True
        self.assertIsNotNone(associate.associate([tick], tracks, 800, 30)[0])


class AltitudeFilterTest(unittest.TestCase):
    def test_single_glitch_is_ignored(self):
        """FRS91, 2026-10-16: 937 m between two ~5,000 m fixes 2 s apart."""
        from altitude import AltitudeFilter
        f = AltitudeFilter(4999.0, 15.0, T0)
        f.update(937.0, 15.0, T0 + timedelta(seconds=2))
        f.update(4580.0, 15.0, T0 + timedelta(seconds=45))
        self.assertAlmostEqual(f.x[0], 4580.0, delta=20)
        self.assertLess(abs(f.x[1]), 20.0)

    def test_real_jump_is_accepted_after_repeats(self):
        from altitude import AltitudeFilter
        f = AltitudeFilter(5000.0, 15.0, T0)
        for i in range(1, 4):
            f.update(1000.0, 15.0, T0 + timedelta(seconds=i))
        self.assertAlmostEqual(f.x[0], 1000.0, delta=20)


class DarkTrackingTest(unittest.TestCase):
    """What the UI shows when following a dark plane."""

    def test_last_confirmed_position_comes_only_from_the_transponder(self):
        t = _track(0, 0)
        t.apply(1000, 0, T0 + timedelta(seconds=10), "adsb", 50.0)
        t.apply(1500, 50, T0 + timedelta(seconds=12), "radar", 150.0)
        self.assertEqual(t.last_adsb_xy, (1000, 0))
        radar_born = Track(0, 0, T0, "radar", 3.0, 150.0 ** 2)
        self.assertIsNone(radar_born.last_adsb_xy)

    def test_uncertainty_grows_while_nothing_updates_the_track(self):
        t = _track(0, 0, vx=200.0)
        now = t.position_std_m()
        later = t.position_std_m(T0 + timedelta(seconds=45))
        self.assertAlmostEqual(now, t.position_std_m(T0))
        self.assertGreater(later, 2 * now)


class EngineTest(unittest.TestCase):
    def test_two_contacts_of_one_aircraft_in_one_batch_both_join(self):
        track = _track(0, 0, vx=200.0, vel_var=1.0)
        tracks = {track.id: track}
        ticks = [_tick(200 * 1.5, 0, at=T0 + timedelta(seconds=1.5), raw_id=1),
                 _tick(200 * 3.0, 0, at=T0 + timedelta(seconds=3.0), raw_id=2)]
        events, touched = [], set()
        engine.apply_anonymous(ticks, tracks, 3.0, 800, 30, events, touched)
        self.assertEqual(len(tracks), 1, "second contact spawned a phantom track")
        self.assertEqual(track.radar_hits, 2)

    def test_velocity_measurement_rules(self):
        tick = {"source": "adsb", "v0": (200.0, 0.0), "altitude_m": 9000.0,
                "velocity_lead_s": 0.0}
        v, rv = engine._velocity_measurement(tick, 3.0)
        self.assertEqual(v, (200.0, 0.0))
        self.assertAlmostEqual(rv, engine.ADSB_VEL_STD_MPS)
        saved = engine.VEL_TIME_OFFSET
        try:
            engine.VEL_TIME_OFFSET = True
            _, rv_late = engine._velocity_measurement({**tick, "velocity_lead_s": 4.0}, 3.0)
            self.assertAlmostEqual(rv_late, math.hypot(engine.ADSB_VEL_STD_MPS, 12.0))
            engine.VEL_TIME_OFFSET = False
            _, rv_off = engine._velocity_measurement({**tick, "velocity_lead_s": 4.0}, 3.0)
            self.assertAlmostEqual(rv_off, engine.ADSB_VEL_STD_MPS)
        finally:
            engine.VEL_TIME_OFFSET = saved
        self.assertEqual(engine._velocity_measurement({**tick, "altitude_m": None}, 3.0),
                         (None, None))
        self.assertEqual(engine._velocity_measurement({**tick, "source": "radar"}, 3.0),
                         (None, None))

    def test_scaled_gates(self):
        self.assertEqual(engine.scaled_gates(10.0), (30.0, 120.0))
        self.assertEqual(engine.scaled_gates(45.0), (112.5, 135.0))

    def test_prune_retires_stale_tracks_and_frees_callsign(self):
        t = _track(0, 0, external_id="ABC1")
        tracks, cs = {t.id: t}, {"ABC1": t.id}
        retired = engine.prune_stale(tracks, cs, T0 + timedelta(seconds=200), 120)
        self.assertEqual(retired, [t.id])
        self.assertEqual((tracks, cs), ({}, {}))


if __name__ == "__main__":
    unittest.main()
