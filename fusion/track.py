"""
track.py — Dark Track / fusion: a single fused real-world entity.

Wraps a KalmanFilter with the identity/recency bookkeeping the rest of
fusion (and later anomaly/) needs: whether it currently has a callsign,
and how long it's been since each source last corroborated it. That
last_adsb_at / last_radar_at gap is exactly what anomaly/'s went-dark
detector (weeks 10-11) will threshold on.
"""

import math

from kalman import KalmanFilter


class Track:
    _next_id = 1

    def __init__(self, x0, y0, observed_at, source, accel_std, init_pos_var,
                 external_id=None):
        self.id = Track._next_id
        Track._next_id += 1

        self.kf = KalmanFilter(x0, y0, accel_std, init_pos_var)
        self.external_id = external_id
        self.created_at = observed_at
        self.last_update = observed_at
        self.last_adsb_at = observed_at if source == "adsb" else None
        self.last_radar_at = observed_at if source == "radar" else None
        self.adsb_hits = 1 if source == "adsb" else 0
        self.radar_hits = 1 if source == "radar" else 0
        self.last_residual_m = 0.0

    def apply(self, x, y, observed_at, source, r_std):
        dt = (observed_at - self.last_update).total_seconds()
        self.kf.predict(dt)
        innovation = self.kf.update((x, y), r_std)
        self.last_residual_m = float(math.hypot(*innovation))
        self.last_update = observed_at
        if source == "adsb":
            self.last_adsb_at = observed_at
            self.adsb_hits += 1
        else:
            self.last_radar_at = observed_at
            self.radar_hits += 1

    def predicted_position(self, at_time):
        dt = (at_time - self.last_update).total_seconds()
        x_pred, _ = self.kf.peek_predict(dt)
        return float(x_pred[0]), float(x_pred[1])

    def confidence(self, now):
        """Heuristic, not a calibrated probability: recent ADS-B
        corroboration is trusted most, radar-only corroboration less (that
        gap is the whole point of the went-dark anomaly), and position
        uncertainty from the filter's own covariance further discounts
        either case. See docs/FUSION.md known limitations."""
        adsb_age = (now - self.last_adsb_at).total_seconds() if self.last_adsb_at else None
        radar_age = (now - self.last_radar_at).total_seconds() if self.last_radar_at else None

        if adsb_age is not None and adsb_age < 15:
            base = 0.95
        elif radar_age is not None and radar_age < 15:
            base = 0.6
        else:
            base = 0.3

        uncertainty_factor = 1.0 / (1.0 + self.kf.position_variance / 10000.0)
        return round(base * uncertainty_factor, 3)

    def heading_deg(self):
        vx, vy = self.kf.velocity
        if vx == 0.0 and vy == 0.0:
            return None
        return (math.degrees(math.atan2(vx, vy)) + 360.0) % 360.0

    def speed_mps(self):
        vx, vy = self.kf.velocity
        return math.hypot(vx, vy)
