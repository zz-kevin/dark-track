"""
track.py — Dark Track / fusion: a single fused real-world entity.

Wraps a KalmanFilter with the identity/recency bookkeeping the rest of
fusion (and later anomaly/) needs: whether it currently has a callsign,
and how long it's been since each source last corroborated it. That
last_adsb_at / last_radar_at gap is exactly what anomaly/'s went-dark
detector (weeks 10-11) will threshold on.
"""

import math

from altitude import AltitudeFilter
from imm import IMMFilter
from kalman import KalmanFilter

# "cv": one constant-velocity Kalman filter per track. "imm": constant
# velocity + coordinated turn, blended (imm.py).
FILTER = "cv"


class Track:
    _next_id = 1

    def __init__(self, x0, y0, observed_at, source, accel_std, init_pos_var,
                 external_id=None, v0=None, init_vel_var=2500.0):
        self.id = Track._next_id
        Track._next_id += 1

        cls = IMMFilter if FILTER == "imm" else KalmanFilter
        self.kf = cls(x0, y0, accel_std, init_pos_var, init_vel_var=init_vel_var, v0=v0)
        self.external_id = external_id
        # Watched military type (ingestion/aircraft_types.py), only ever
        # learned from identified ADS-B ticks -- see note_type().
        self.aircraft_type = None
        self.aircraft_variant = None
        self.created_at = observed_at
        self.last_update = observed_at
        self.last_adsb_at = observed_at if source == "adsb" else None
        self.last_radar_at = observed_at if source == "radar" else None
        # Where the aircraft's own transponder last put it (local x, y) --
        # the "last confirmed position" once it goes dark.
        self.last_adsb_xy = (x0, y0) if source == "adsb" else None
        # Altitude from the most recent identified ADS-B tick (None = OpenSky
        # reported no barometric altitude, which it does for aircraft on the
        # ground). anomaly/'s went-dark detector skips ground traffic.
        self.last_adsb_alt_m = None
        # Altitude estimate for association (altitude.py), from ADS-B and
        # radar heights; None until a tick with an altitude arrives.
        self.alt = None
        self.adsb_hits = 1 if source == "adsb" else 0
        self.radar_hits = 1 if source == "radar" else 0
        self.last_residual_m = 0.0
        # Placeholder for the "created, not yet updated" event this track's
        # constructor tick gets logged with (see apply_identified/
        # apply_anonymous in engine.py) -- no Kalman update() has run yet to
        # produce a real S, so this uses the initial P's position block the
        # same way S normally would (P[0,0]+P[1,1], then sqrt), rather than
        # 0.0, which would make kinematic_inconsistency()'s normalization
        # divide by zero.
        self.last_innovation_std_m = (2 * init_pos_var) ** 0.5

    def apply(self, x, y, observed_at, source, r_std, v=None, rv_std=None):
        dt = (observed_at - self.last_update).total_seconds()
        self.kf.predict(dt)
        innovation = self.kf.update((x, y), r_std, v, rv_std)
        self.last_residual_m = float(math.hypot(*innovation))
        self.last_innovation_std_m = self.kf.last_innovation_std_m
        self.last_update = observed_at
        if source == "adsb":
            self.last_adsb_at = observed_at
            self.last_adsb_xy = (x, y)
            self.adsb_hits += 1
        else:
            self.last_radar_at = observed_at
            self.radar_hits += 1

    def observe_altitude(self, h, std, at):
        if h is None or std is None:
            return
        if self.alt is None:
            self.alt = AltitudeFilter(h, std, at)
        else:
            self.alt.update(h, std, at)

    def position_std_m(self, at=None):
        """One-sigma position uncertainty per axis, metres -- how far off the
        fused position might be, predicted forward to `at` (default: as of
        the last update). Grows while only noisy radar (or nothing) is
        updating the track; the web UI draws it as a ring."""
        if at is None:
            var = self.kf.position_variance
        else:
            _, P = self.kf.peek_predict((at - self.last_update).total_seconds())
            var = float(P[0, 0] + P[1, 1])
        return math.sqrt(var / 2)

    def note_type(self, aircraft_type, aircraft_variant):
        """Record type from an ADS-B tick. A later tick with no type never
        clears it, and a specific type/variant beats the generic US_MIL
        hex-block flag."""
        if aircraft_type is None:
            return
        if aircraft_type == "US_MIL" and self.aircraft_type not in (None, "US_MIL"):
            return
        self.aircraft_type = aircraft_type
        self.aircraft_variant = aircraft_variant or self.aircraft_variant

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
