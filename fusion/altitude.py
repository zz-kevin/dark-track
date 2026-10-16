"""
altitude.py — Dark Track / fusion: per-track altitude estimate.

A small Kalman filter over [altitude, vertical rate], kept beside each
track's horizontal filter (kalman.py / imm.py) rather than inside it:
altitude only enters association (associate.py), as a third dimension of
the likelihood, and keeping it separate leaves the horizontal filters and
everything that reads them unchanged.

Why it's there: two aircraft holding in the same stack, or a formation
pair, can be inside radar's horizontal noise of each other but 1,000 ft
apart vertically. A 3D surveillance radar measures height too, if
coarsely (radar_sim: 300 m std by default); ADS-B reports barometric
altitude to 25 ft.
"""

import numpy as np

# How fast vertical rate changes, m/s^2 (level-offs, start of climb).
VERT_ACCEL_STD = 1.0
INIT_VZ_STD = 10.0  # m/s: unknown climb/descent rate on a new estimate
# Real ADS-B altitude glitches: FRS91 (2026-10-16 04:51 UTC) reported
# 937 m, then 4,580 m two seconds later. Taken at face value that's a
# 1,800 m/s climb, and the filter oscillated by hundreds of m/s for the
# rest of the window, poisoning association around it. So: readings
# more than OUTLIER_SIGMAS off the prediction are skipped (re-initialising
# after REINIT_AFTER in a row, so a real jump isn't ignored for long),
# and vertical rate is held to what aircraft can do.
OUTLIER_SIGMAS = 5.0
REINIT_AFTER = 2
VZ_MAX = 60.0  # m/s, ~12,000 ft/min


def _predict(x, P, dt):
    dt = max(dt, 0.0)
    F = np.array([[1.0, dt], [0.0, 1.0]])
    q = VERT_ACCEL_STD ** 2
    Q = q * np.array([[dt ** 4 / 4, dt ** 3 / 2], [dt ** 3 / 2, dt ** 2]])
    return F @ x, F @ P @ F.T + Q


class AltitudeFilter:
    def __init__(self, h, std, at):
        self.x = np.array([h, 0.0])
        self.P = np.diag([std ** 2, INIT_VZ_STD ** 2])
        self.at = at
        self.rejected = 0

    def peek(self, at):
        """(altitude, variance) predicted at datetime `at`."""
        x, P = _predict(self.x, self.P, (at - self.at).total_seconds())
        return float(x[0]), float(P[0, 0])

    def update(self, h, std, at):
        x, P = _predict(self.x, self.P, (at - self.at).total_seconds())
        S = P[0, 0] + std ** 2
        if (h - x[0]) ** 2 > OUTLIER_SIGMAS ** 2 * S:
            self.rejected += 1
            if self.rejected >= REINIT_AFTER:
                self.__init__(h, std, at)
            return
        self.rejected = 0
        K = P[:, 0] / S
        self.x = x + K * (h - x[0])
        self.x[1] = min(max(self.x[1], -VZ_MAX), VZ_MAX)
        self.P = P - np.outer(K, P[0, :])
        self.at = max(self.at, at)
