#!/usr/bin/env python3
"""
opensky_poll.py — Dark Track / ingestion: OpenSky Network live ADS-B feed.

Polls OpenSky's public `/states/all` endpoint for a bounding box, prints
callsign, lat/lon, altitude, and velocity for every aircraft currently
inside it, and writes each state vector into the shared `raw_ticks` table
(source='adsb') so fusion/replay can consume it later. Run
`python3 db.py` once first to create the table.

Each tick also carries the transponder's icao24 address and, if it belongs
to one of the watched military types in aircraft_types.py (F-15E, F-35,
A-10, F-16, EA-18G, B-2, HC-130J, HH-60W/UH-60, E-3, E-2D, P-8A), its
aircraft_type/aircraft_variant. Watched aircraft are highlighted in the
printed output. Pass --aircraft-db with OpenSky's aircraft metadata CSV to
identify specific types; without it only the US military hex block is
flagged.

No account required -- anonymous access works, but its daily credit
allowance runs out within about an hour on a large box like --region
gulf at the default 10s cadence. Either raise --poll-seconds or set
OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET (a free OpenSky account's API
client) for a larger allowance.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import warnings

# The venv's macOS system Python 3.9 links LibreSSL; urllib3 v2 warns about
# it on import but HTTPS to OpenSky works fine. Silence just that warning.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

try:
    import psycopg2
    import requests
except ImportError as e:
    sys.exit(
        f"{e} -- run inside the project venv: `source .venv/bin/activate` "
        f"from the repo root (and `pip install -r requirements.txt` once)"
    )

import db
from aircraft_types import Classifier, label
from regions import REGIONS

REPO_ROOT = Path(__file__).resolve().parent.parent
# Picked up automatically when --aircraft-db isn't given.
DEFAULT_AIRCRAFT_DB = REPO_ROOT / "data" / "raw" / "aircraft-database.csv"
# OpenSky API client credentials, as downloaded from the account page.
CREDENTIALS_FILE = REPO_ROOT / "opensky-credentials.json"

# ---- Bounding box presets (--region, see regions.py), or pass --bbox ----

POLL_SECONDS = 10
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)
# Fallback wait after an HTTP 429 when OpenSky doesn't say how long.
RATE_LIMIT_BACKOFF_SECONDS = 300
OPENSKY_URL = "https://opensky-network.org/api/states/all"

# OpenSky state vector field indices (from the public API docs) — named
# here so the rest of the script doesn't have unexplained magic numbers.
IDX_ICAO24 = 0
IDX_CALLSIGN = 1
IDX_TIME_POSITION = 3
IDX_LAST_CONTACT = 4  # latest message of any kind -- what velocity is as of
IDX_LON = 5
IDX_LAT = 6
IDX_BARO_ALT = 7
IDX_VELOCITY = 9
IDX_TRUE_TRACK = 10  # heading, degrees clockwise from north


class RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__(f"rate limited, retry in {retry_after:.0f}s")
        self.retry_after = retry_after


class OpenSkyAuth:
    """OAuth2 client-credentials login for a (free) OpenSky account, which
    gets a much larger daily credit allowance than anonymous access.
    Credentials come from the OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET env
    vars if set, else from CREDENTIALS_FILE -- the credentials.json OpenSky
    lets you download when creating an API client, saved at the repo root
    (gitignored). Never from flags or code, so they don't land in shell
    history or git.
    Tokens last ~30 min; refreshed a minute before expiry."""

    def __init__(self):
        self.client_id = os.environ.get("OPENSKY_CLIENT_ID")
        self.client_secret = os.environ.get("OPENSKY_CLIENT_SECRET")
        if not (self.client_id and self.client_secret) and CREDENTIALS_FILE.is_file():
            creds = json.loads(CREDENTIALS_FILE.read_text())
            self.client_id = creds.get("clientId")
            self.client_secret = creds.get("clientSecret")
        self.token = None
        self.expires_at = 0.0

    @property
    def enabled(self):
        return bool(self.client_id and self.client_secret)

    def headers(self):
        if not self.enabled:
            return {}
        if self.token is None or time.time() > self.expires_at - 60:
            resp = requests.post(OPENSKY_TOKEN_URL, data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }, timeout=15)
            resp.raise_for_status()
            body = resp.json()
            self.token = body["access_token"]
            self.expires_at = time.time() + body.get("expires_in", 1800)
        return {"Authorization": f"Bearer {self.token}"}


def fetch_states(bbox, auth):
    lamin, lamax, lomin, lomax = bbox
    params = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}
    resp = requests.get(OPENSKY_URL, params=params, headers=auth.headers(),
                        timeout=15)
    if resp.status_code == 401 and auth.enabled:
        auth.token = None  # expired/revoked early -- re-login next poll
    if resp.status_code == 429:
        # Anonymous daily credits used up -- OpenSky says when they reset.
        retry_after = resp.headers.get("X-Rate-Limit-Retry-After-Seconds")
        raise RateLimited(float(retry_after) if retry_after else RATE_LIMIT_BACKOFF_SECONDS)
    resp.raise_for_status()
    return resp.json()


def print_states(states, ts, classifier, quiet=False):
    if not states:
        print(f"[{ts}] no aircraft currently in the bounding box")
        return
    watched = 0
    lines = []
    for s in states:
        callsign = (s[IDX_CALLSIGN] or "").strip() or "?"
        lat, lon = s[IDX_LAT], s[IDX_LON]
        alt, velocity = s[IDX_BARO_ALT], s[IDX_VELOCITY]
        alt_str = f"{alt:.0f}m" if alt is not None else "n/a"
        vel_str = f"{velocity:.0f}m/s" if velocity is not None else "n/a"
        type_label = label(*classifier.classify(s[IDX_ICAO24]))
        marker = "*" if type_label else " "
        watched += bool(type_label)
        line = (f" {marker} {callsign:<10} lat={lat} lon={lon} "
                f"alt={alt_str} vel={vel_str}")
        if type_label:
            line += f"  <- {type_label}"
        if type_label or not quiet:
            lines.append(line)
    print(f"[{ts}] {len(states)} aircraft ({watched} watched)"
          + (":" if lines else ""))
    if lines:
        print("\n".join(lines))


def to_rows(data, fetched_at, classifier, watched_only=False):
    """Turn a raw OpenSky response into raw_ticks rows. Skips states with no
    position fix -- those aren't a usable tick for fusion or replay -- and,
    with watched_only, anything that isn't a watched type."""
    rows = []
    for s in data.get("states") or []:
        lat, lon = s[IDX_LAT], s[IDX_LON]
        if lat is None or lon is None:
            continue
        icao24 = (s[IDX_ICAO24] or "").strip().lower() or None
        aircraft_type, aircraft_variant = classifier.classify(icao24)
        if watched_only and aircraft_type is None:
            continue
        callsign = (s[IDX_CALLSIGN] or "").strip() or None
        time_position = s[IDX_TIME_POSITION]
        observed_at = (
            datetime.fromtimestamp(time_position, tz=timezone.utc)
            if time_position is not None
            else fetched_at
        )
        rows.append({
            "source": "adsb",
            "external_id": callsign,
            "lat": lat,
            "lon": lon,
            "altitude_m": s[IDX_BARO_ALT],
            "velocity_mps": s[IDX_VELOCITY],
            "heading_deg": s[IDX_TRUE_TRACK],
            "observed_at": observed_at,
            # Velocity comes from the latest velocity message, which can be
            # seconds newer than the position fix (observed_at); fusion
            # weights it by the gap. Approximated by last_contact.
            "velocity_at": (
                datetime.fromtimestamp(s[IDX_LAST_CONTACT], tz=timezone.utc)
                if s[IDX_LAST_CONTACT] is not None else None
            ),
            "icao24": icao24,
            "aircraft_type": aircraft_type,
            "aircraft_variant": aircraft_variant,
        })
    return rows


def resolve_aircraft_db(path):
    """--aircraft-db as given (relative to cwd), else relative to the repo
    root, so it works whichever directory the poller is launched from.
    With no flag, use DEFAULT_AIRCRAFT_DB if it's been downloaded."""
    if path is None:
        return str(DEFAULT_AIRCRAFT_DB) if DEFAULT_AIRCRAFT_DB.is_file() else None
    p = Path(path).expanduser()
    if not p.is_file() and not p.is_absolute() and (REPO_ROOT / p).is_file():
        p = REPO_ROOT / p
    return str(p)


def main():
    args = parse_args()
    bbox = tuple(args.bbox) if args.bbox else REGIONS[args.region]
    try:
        classifier = Classifier(resolve_aircraft_db(args.aircraft_db))
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))
    auth = OpenSkyAuth()
    lamin, lamax, lomin, lomax = bbox
    if auth.enabled:
        print(f"OpenSky: API client {auth.client_id}")
    else:
        print("OpenSky: anonymous (set OPENSKY_CLIENT_ID / "
              "OPENSKY_CLIENT_SECRET for a larger daily limit)")
    print(
        f"Polling OpenSky every {args.poll_seconds:g}s for box "
        f"lat[{lamin},{lamax}] lon[{lomin},{lomax}]"
        f"{' (watched types only)' if args.watched_only else ''}. "
        f"Ctrl+C to stop.\n"
    )
    try:
        while True:
            time.sleep(poll_once(bbox, classifier, auth, args))
    except KeyboardInterrupt:
        print("\nstopped.")


def poll_once(bbox, classifier, auth, args):
    """One fetch + store. Returns how long to sleep before the next one.
    Network, rate-limit, and database failures are reported and survived
    -- a Postgres restart or a dropped connection shouldn't kill a
    long-running poller."""
    fetched_at = datetime.now(timezone.utc)
    try:
        data = fetch_states(bbox, auth)
    except RateLimited as e:
        print(f"OpenSky {e} (anonymous daily limit; a smaller --bbox "
              f"costs fewer credits per poll)", file=sys.stderr)
        return e.retry_after
    except requests.exceptions.RequestException as e:
        print(f"request failed: {e}", file=sys.stderr)
        return args.poll_seconds

    states = data.get("states") or []
    print_states(states, fetched_at.strftime("%H:%M:%S"), classifier, args.quiet)
    rows = to_rows(data, fetched_at, classifier, args.watched_only)
    if rows:
        try:
            db.insert_ticks(rows)
        except psycopg2.Error as e:
            first_line = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            print(f"db write failed, {len(rows)} ticks dropped: {first_line} "
                  f"(is Postgres running? `brew services start postgresql@16`)",
                  file=sys.stderr)
    return args.poll_seconds


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", choices=sorted(REGIONS), default="nyc",
                    help="bounding box preset (default: nyc)")
    p.add_argument("--bbox", type=float, nargs=4,
                    metavar=("LAMIN", "LAMAX", "LOMIN", "LOMAX"),
                    help="custom bounding box, overrides --region")
    p.add_argument("--aircraft-db", default=None,
                    help="path to OpenSky's aircraft metadata CSV, used to "
                         "identify watched military types by icao24 "
                         "(default: data/raw/aircraft-database.csv if present)")
    p.add_argument("--watched-only", action="store_true",
                    help="only store ticks from watched types (and the US "
                         "military hex block) -- everything is still printed")
    p.add_argument("--poll-seconds", type=float, default=POLL_SECONDS,
                    help=f"seconds between polls (default {POLL_SECONDS}). "
                         f"A large box like --region gulf burns anonymous "
                         f"credits fast; 30-60 stretches them through the day")
    p.add_argument("--quiet", action="store_true",
                    help="print only a per-poll count plus watched aircraft, "
                         "not every aircraft in the box")
    return p.parse_args()


if __name__ == "__main__":
    main()
