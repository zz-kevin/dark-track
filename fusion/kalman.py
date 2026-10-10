"""
kalman.py — Dark Track / fusion: constant-velocity Kalman filter.

One instance per fused track. State is [x, y, vx, vy] in local ENU meters
(see geo.py); measurements are position-only [x, y] -- velocity/heading
carried on raw ticks is informational, not fed into the filter, to keep
the measurement model (H) the same for both sources. See
docs/FUSION.md for why a Kalman filter was chosen over an alpha-beta
filter: per-source measurement noise (R) is what lets the same filter
trust an ADS-B position far more than a radar-derived one, which is the
actual point of fusing two sources.
"""

import math

import numpy as np

STATE_DIM = 4
H = np.array([[1.0, 0.0, 0.0, 0.0],
              [0.0, 1.0, 0.0, 0.0]])


def transition_matrix(dt):
    return np.array([
        [1.0, 0.0, dt, 0.0],
        [0.0, 1.0, 0.0, dt],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ])


def process_noise(dt, accel_std):
    """Discretized white-noise-acceleration model. accel_std is how much an
    aircraft's velocity is expected to change per second (m/s^2) -- larger
    values let the filter follow turns/speed changes faster at the cost of
    noisier position estimates between measurements."""
    q = accel_std ** 2
    dt2, dt3, dt4 = dt ** 2, dt ** 3, dt ** 4
    return q * np.array([
        [dt4 / 4, 0.0,      dt3 / 2, 0.0],
        [0.0,      dt4 / 4, 0.0,      dt3 / 2],
        [dt3 / 2, 0.0,      dt2,      0.0],
        [0.0,      dt3 / 2, 0.0,      dt2],
    ])


class KalmanFilter:
    def __init__(self, x0, y0, accel_std, init_pos_var, init_vel_var=2500.0):
        self.x = np.array([x0, y0, 0.0, 0.0])
        self.P = np.diag([init_pos_var, init_pos_var, init_vel_var, init_vel_var])
        self.accel_std = accel_std

    def peek_predict(self, dt):
        """Predicted (x, P) at dt seconds ahead, without mutating state --
        used for gating distance checks before an association is decided."""
        dt = max(dt, 0.0)
        F = transition_matrix(dt)
        x_pred = F @ self.x
        P_pred = F @ self.P @ F.T + process_noise(dt, self.accel_std)
        return x_pred, P_pred

    def predict(self, dt):
        self.x, self.P = self.peek_predict(dt)

    def update(self, z, r_std):
        """z is (x, y) meters. r_std is this measurement's position noise
        std dev (meters) -- small for ADS-B, large for radar."""
        R = np.diag([r_std ** 2, r_std ** 2])
        y = np.array(z) - H @ self.x
        S = H @ self.P @ H.T + R
        K = self.P @ H.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(STATE_DIM) - K @ H) @ self.P
        return y  # innovation, for residual/QC tracking

    @property
    def position(self):
        return float(self.x[0]), float(self.x[1])

        # S is the innovation covariance -- the actual, dt-aware spread the
        # innovation y should have under the process+measurement model
        # (grows with elapsed dt via process_noise, unlike a fixed r_std).
        # Stored as a scalar std dev (trace of the position block, same
        # convention as position_variance below) so callers normalizing
        # residuals -- anomaly/detectors.py's kinematic_inconsistency --
        # compare against the filter's real uncertainty, not just R.
        self.last_innovation_std_m = float(math.sqrt(S[0, 0] + S[1, 1]))
    @property
    def velocity(self):
        return float(self.x[2]), float(self.x[3])

    @property
    def position_variance(self):
        """Trace of the position block of P -- a simple scalar uncertainty
        measure for confidence scoring."""
        return float(self.P[0, 0] + self.P[1, 1])
