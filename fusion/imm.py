"""
imm.py — Dark Track / fusion: interacting-multiple-model filter.

Two motion models run side by side on every track, each an (extended)
Kalman filter over [x, y, vx, vy, omega]:

  - CV: constant velocity -- straight flight (omega carried, unused).
  - CT: coordinated turn at turn rate omega (rad/s), an EKF.

Each update weighs the models by how well they predicted the measurement
and blends them (Blom & Bar-Shalom's IMM). Why: with a single
constant-velocity filter, a transponder that drops *during a turn*
leaves the track coasting straight -- ~4 km off its next radar contact
45 s later on Gulf arrivals, which then went to a neighbour
(docs/FUSION.md). The CT model keeps bending the prediction while the
turn is still the likelier explanation.

Same interface as kalman.KalmanFilter (x/P are the blended 4-state
estimate), so Track, association and the detectors don't change.
"""

import math

import numpy as np

from kalman import process_noise

N = 5  # x, y, vx, vy, omega
H_POS = np.eye(2, N)
H_POS_VEL = np.eye(4, N)

# Tuning (development windows only -- see docs/FUSION.md).
CV_ACCEL_STD = 1.0       # m/s^2: straight flight, gentle speed changes
CT_ACCEL_STD = 1.0       # m/s^2: on top of the turn itself
OMEGA_NOISE = 0.005      # rad/s per sqrt(s): how fast turn rate changes
OMEGA_INIT_STD = 0.03    # rad/s: unknown turn rate on a new track
MODE_TAU_S = 120.0       # mean time between switching straight <-> turning
PRIOR_TURN = 0.2         # turn-model probability on a new track


def _q(dt, accel_std):
    Q = np.zeros((N, N))
    Q[:4, :4] = process_noise(dt, accel_std)
    Q[4, 4] = OMEGA_NOISE ** 2 * dt
    return Q


def _cv(x, P, dt):
    F = np.eye(N)
    F[0, 2] = F[1, 3] = dt
    return F @ x, F @ P @ F.T + _q(dt, CV_ACCEL_STD)


def _ct(x, P, dt):
    px, py, vx, vy, w = x
    if abs(w) < 1e-6:
        # Straight-line limit, plus the first-order effect of omega.
        F = np.eye(N)
        F[0, 2] = F[1, 3] = dt
        F[0, 4], F[1, 4] = -0.5 * dt * dt * vy, 0.5 * dt * dt * vx
        F[2, 4], F[3, 4] = -dt * vy, dt * vx
        xn = np.array([px + vx * dt, py + vy * dt, vx, vy, w])
    else:
        s, c = math.sin(w * dt), math.cos(w * dt)
        xn = np.array([
            px + (s * vx - (1 - c) * vy) / w,
            py + ((1 - c) * vx + s * vy) / w,
            c * vx - s * vy,
            s * vx + c * vy,
            w,
        ])
        F = np.eye(N)
        F[0, 2], F[0, 3] = s / w, -(1 - c) / w
        F[1, 2], F[1, 3] = (1 - c) / w, s / w
        F[2, 2], F[2, 3] = c, -s
        F[3, 2], F[3, 3] = s, c
        F[0, 4] = (vx * (dt * c * w - s) - vy * (dt * s * w - (1 - c))) / (w * w)
        F[1, 4] = (vx * (dt * s * w - (1 - c)) + vy * (dt * c * w - s)) / (w * w)
        F[2, 4] = -dt * s * vx - dt * c * vy
        F[3, 4] = dt * c * vx - dt * s * vy
    return xn, F @ P @ F.T + _q(dt, CT_ACCEL_STD)


MODELS = (_cv, _ct)


def _blend(weights, xs, Ps):
    x = sum(w * xi for w, xi in zip(weights, xs))
    P = sum(w * (Pi + np.outer(xi - x, xi - x)) for w, xi, Pi in zip(weights, xs, Ps))
    return x, P


class IMMFilter:
    def __init__(self, x0, y0, accel_std, init_pos_var, init_vel_var=2500.0, v0=None):
        vx0, vy0 = v0 if v0 is not None else (0.0, 0.0)
        x = np.array([x0, y0, vx0, vy0, 0.0])
        P = np.diag([init_pos_var, init_pos_var, init_vel_var, init_vel_var,
                     OMEGA_INIT_STD ** 2])
        self.xs = [x.copy(), x.copy()]
        self.Ps = [P.copy(), P.copy()]
        self.mu = np.array([1.0 - PRIOR_TURN, PRIOR_TURN])
        self._blended = None
        # Used by associate.py for the sub-second extrapolation inside an
        # association slice (and nowhere else).
        self.accel_std = max(CV_ACCEL_STD, CT_ACCEL_STD)
        self.last_innovation_std_m = math.sqrt(2 * init_pos_var)

    # -- IMM steps ---------------------------------------------------------

    def _mix_predict(self, dt):
        """Mixed + predicted per-model (xs, Ps) and predicted model
        probabilities c, dt seconds ahead."""
        dt = max(dt, 0.0)
        p = 1.0 - math.exp(-dt / MODE_TAU_S)  # chance of a switch within dt
        T = np.array([[1 - p, p], [p, 1 - p]])
        c = T.T @ self.mu
        xs, Ps = [], []
        for j, model in enumerate(MODELS):
            w = T[:, j] * self.mu / c[j]
            x0, P0 = _blend(w, self.xs, self.Ps)
            xj, Pj = model(x0, P0, dt)
            xs.append(xj)
            Ps.append(0.5 * (Pj + Pj.T))
        return xs, Ps, c

    def peek_predict(self, dt):
        """Blended predicted (x, P) over [x, y, vx, vy], dt seconds ahead,
        without changing state."""
        xs, Ps, c = self._mix_predict(dt)
        x, P = _blend(c, xs, Ps)
        return x[:4], P[:4, :4]

    def peek_predict_components(self, dt):
        """[(model probability, x, P)] per model, dt seconds ahead --
        association scores a contact against this mixture, not the blend."""
        xs, Ps, c = self._mix_predict(dt)
        return [(float(c[j]), xs[j][:4], Ps[j][:4, :4]) for j in range(len(xs))]

    def predict(self, dt):
        self._pending = self._mix_predict(dt)

    def update(self, z, r_std, v=None, rv_std=None):
        xs, Ps, c = getattr(self, "_pending", None) or self._mix_predict(0.0)
        self._pending = None
        if v is None:
            H, zv = H_POS, np.array(z, dtype=float)
            R = np.diag([r_std ** 2] * 2)
        else:
            H, zv = H_POS_VEL, np.array([z[0], z[1], v[0], v[1]], dtype=float)
            R = np.diag([r_std ** 2] * 2 + [rv_std ** 2] * 2)

        # Blended prediction, for the residual the detectors see.
        xb, Pb = _blend(c, xs, Ps)
        Sb = H @ Pb @ H.T + R
        innovation = zv - H @ xb
        self.last_innovation_std_m = float(math.sqrt(Sb[0, 0] + Sb[1, 1]))

        likes = np.empty(len(MODELS))
        for j in range(len(MODELS)):
            S = H @ Ps[j] @ H.T + R
            y = zv - H @ xs[j]
            K = Ps[j] @ H.T @ np.linalg.inv(S)
            xs[j] = xs[j] + K @ y
            Pj = (np.eye(N) - K @ H) @ Ps[j]
            Ps[j] = 0.5 * (Pj + Pj.T)
            m2 = float(y @ np.linalg.solve(S, y))
            likes[j] = math.exp(-0.5 * min(m2, 700.0)) / math.sqrt(np.linalg.det(2 * math.pi * S))
        mu = likes * c
        total = mu.sum()
        self.mu = mu / total if total > 0 and np.isfinite(total) else c
        self.xs, self.Ps = xs, Ps
        self._blended = None
        return innovation[:2]

    # -- blended state, KalmanFilter-compatible ------------------------------

    def _state(self):
        # Blended estimate, cached until the next update -- Track and the
        # API read position/velocity/variance several times per update.
        if self._blended is None:
            x, P = _blend(self.mu, self.xs, self.Ps)
            self._blended = (x[:4], P[:4, :4])
        return self._blended

    @property
    def x(self):
        return self._state()[0]

    @property
    def P(self):
        return self._state()[1]

    @property
    def position(self):
        x = self.x
        return float(x[0]), float(x[1])

    @property
    def velocity(self):
        x = self.x
        return float(x[2]), float(x[3])

    @property
    def position_variance(self):
        P = self.P
        return float(P[0, 0] + P[1, 1])

    @property
    def turn_probability(self):
        return float(self.mu[1])
