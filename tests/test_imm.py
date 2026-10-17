"""The interacting-multiple-model filter (fusion/imm.py)."""

import math
import unittest

import support  # noqa: F401  (sets up sys.path)
import imm
from kalman import KalmanFilter


def _turn(t, v=200.0, rate_deg=3.0, straight_s=60.0):
    """Straight east for straight_s, then a constant-rate left turn."""
    if t <= straight_s:
        return v * t, 0.0, v, 0.0
    w = math.radians(rate_deg)
    th = w * (t - straight_s)
    return (v * straight_s + v / w * math.sin(th), v / w * (1 - math.cos(th)),
            v * math.cos(th), v * math.sin(th))


def _fly(kf, until, step=10):
    prev = 0
    for t in range(step, until + 1, step):
        x, y, vx, vy = _turn(t)
        kf.predict(t - prev)
        kf.update((x, y), 50.0, v=(vx, vy), rv_std=10.0)
        prev = t


class IMMTest(unittest.TestCase):
    def test_straight_flight_stays_in_straight_mode(self):
        kf = imm.IMMFilter(0, 0, 3.0, 50 ** 2, init_vel_var=100, v0=(200, 0))
        _fly(kf, 60)
        self.assertLess(kf.turn_probability, 0.2)
        x, _ = kf.peek_predict(45)
        self.assertAlmostEqual(x[0], 200 * 105, delta=150)

    def test_learns_turn_rate_and_predicts_through_a_dropout(self):
        kf = imm.IMMFilter(0, 0, 3.0, 50 ** 2, init_vel_var=100, v0=(200, 0))
        _fly(kf, 100)  # 40 s into the turn, then the transponder drops
        self.assertGreater(kf.turn_probability, 0.9)
        self.assertAlmostEqual(math.degrees(kf.xs[1][4]), 3.0, delta=0.2)
        truth = _turn(145)
        turn_component = max(kf.peek_predict_components(45), key=lambda c: c[0])
        self.assertLess(math.hypot(turn_component[1][0] - truth[0],
                                   turn_component[1][1] - truth[1]), 300)

    def test_beats_constant_velocity_after_dropout_mid_turn(self):
        truth = _turn(145)
        errors = {}
        for name, cls in (("cv", KalmanFilter), ("imm", imm.IMMFilter)):
            kf = cls(0, 0, 3.0, 50 ** 2, init_vel_var=100, v0=(200, 0))
            _fly(kf, 100)
            x, _ = kf.peek_predict(45)
            errors[name] = math.hypot(x[0] - truth[0], x[1] - truth[1])
        self.assertLess(errors["imm"], errors["cv"] / 2)

    def test_components_weights_sum_to_one(self):
        kf = imm.IMMFilter(0, 0, 3.0, 50 ** 2, v0=(200, 0))
        comps = kf.peek_predict_components(30)
        self.assertEqual(len(comps), 2)
        self.assertAlmostEqual(sum(w for w, _, _ in comps), 1.0)


if __name__ == "__main__":
    unittest.main()
