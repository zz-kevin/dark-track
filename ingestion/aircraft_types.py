"""
aircraft_types.py — Dark Track / ingestion: watched military aircraft types.

ADS-B state vectors from OpenSky carry an ICAO 24-bit transponder address
(`icao24`) and a callsign, but no airframe type. Type comes from joining
`icao24` against OpenSky's public aircraft metadata database (a CSV dump,
downloaded once -- see ingestion/README.md), matching each airframe's ICAO
type designator and free-text model against the WATCHLIST below.

Some watched types can't be seen on ADS-B at all (RQ-180, LUCAS) -- they
carry no ICAO type designator and no transponder. They're kept in the
list with adsb_trackable=False so the catalog stays complete, but the
classifier never returns them; they only ever show up as radar-only
contacts.

Most of the other airframes fly with ADS-B off or spoofed during real
operations, so a match here is "this transponder belongs to a watched
type", not a claim of full coverage. That gap -- a watched type whose
transponder drops while the radar contact keeps flying -- is exactly what
the went-dark detector in anomaly/ is for.
"""

import csv
import os
import re
import sys
from dataclasses import dataclass, field

# US military ICAO 24-bit address block (AE0000-AFFFFF). A hex in here with
# no metadata match is still worth flagging as "US military, type unknown".
US_MIL_HEX_MIN = 0xAE0000
US_MIL_HEX_MAX = 0xAFFFFF


@dataclass(frozen=True)
class WatchedType:
    key: str            # stored in raw_ticks.aircraft_type
    name: str
    category: str       # manned | support | drone | naval
    role: str
    typecodes: tuple    # ICAO Doc 8643 type designators
    model_patterns: tuple = ()
    # Typecode alone isn't specific enough (e.g. C30J is every C-130J, not
    # just the HC-130J) -- also require a model_patterns hit.
    require_model_match: bool = False
    # Model strings that rule a typecode hit back out (e.g. an F-15C).
    exclude_patterns: tuple = ()
    # variant label -> regex on the metadata model string
    variants: dict = field(default_factory=dict)
    # Model patterns are specific enough to match on their own (drones
    # often have no ICAO type designator in the metadata DB).
    model_only_ok: bool = False
    # False for types that never broadcast ADS-B -- listed, never matched.
    adsb_trackable: bool = True
    notes: str = ""


WATCHLIST = (
    # ---- Manned combat aircraft ----
    WatchedType(
        key="F15E", name="F-15E Strike Eagle", category="manned",
        role="Primary strike fighter",
        typecodes=("F15",),
        model_patterns=(r"F-?15E", r"Strike Eagle"),
        # Export derivatives (Qatar's F-15QA etc.) share the F15 designator.
        exclude_patterns=(r"F-?15[A-D]\b", r"F-?15EX", r"Eagle II",
                          r"F-?15(QA|SA|SG|K|I)\b", r"Ababil"),
    ),
    WatchedType(
        key="F35", name="F-35 Lightning II", category="manned",
        role="Stealth strike/surveillance fighter (A: land-based, C: carrier)",
        typecodes=("F35",),
        model_patterns=(r"F-?35", r"Lightning II"),
        variants={"A": r"F-?35A", "C": r"F-?35C"},
    ),
    WatchedType(
        key="A10", name="A-10 Thunderbolt II", category="manned",
        role="Close air support / low-altitude cover for rescue ops",
        typecodes=("A10",),
        model_patterns=(r"A-?10", r"Thunderbolt"),
    ),
    WatchedType(
        key="F16", name="F-16 Fighting Falcon", category="manned",
        role="Strike and patrol from regional bases",
        typecodes=("F16",),
        model_patterns=(r"F-?16", r"Fighting Falcon"),
    ),
    WatchedType(
        key="EA18G", name="EA-18G Growler", category="manned",
        role="Electronic warfare: radar and comms jamming",
        # F18/F18S also cover non-EW Hornets and Super Hornets.
        typecodes=("F18", "F18S", "G18"),
        model_patterns=(r"EA-?18", r"Growler"),
        require_model_match=True,
    ),
    WatchedType(
        key="B2", name="B-2 Spirit", category="manned",
        role="Stealth bomber, precision strike",
        typecodes=("B2",),
        model_patterns=(r"B-?2\b", r"Spirit"),
    ),

    # ---- Naval air power (carrier air wings, see CARRIERS) ----
    # Must come after EA18G: both share the F18S designator, and the
    # Growler's model string has to be checked first.
    WatchedType(
        key="FA18EF", name="F/A-18E/F Super Hornet", category="naval",
        role="Carrier-based strike fighter",
        typecodes=("F18S", "F18"),
        model_patterns=(r"F/?A-?18[EF]", r"Super Hornet"),
        require_model_match=True,
        variants={"E": r"F/?A-?18E", "F": r"F/?A-?18F"},
    ),

    # ---- Support and rescue aircraft ----
    WatchedType(
        key="HC130J", name="HC-130J Combat King II", category="support",
        role="Airborne command post for personnel recovery / SAR",
        typecodes=("C30J",),
        model_patterns=(r"HC-?130", r"Combat King"),
        require_model_match=True,
    ),
    WatchedType(
        key="H60", name="HH-60W Jolly Green II / UH-60 Black Hawk",
        category="support",
        role="Combat rescue and transport helicopter",
        # H60 also covers SH/MH-60 Seahawks and civil S-70s.
        typecodes=("H60",),
        model_patterns=(r"HH-?60W", r"Jolly Green", r"UH-?60", r"Black ?Hawk"),
        require_model_match=True,
        variants={"HH-60W": r"HH-?60W|Jolly Green", "UH-60": r"UH-?60|Black ?Hawk"},
    ),
    WatchedType(
        key="E3", name="E-3 Sentry (AWACS)", category="support",
        role="Airborne warning and control",
        typecodes=("E3TF", "E3CF"),
        model_patterns=(r"E-?3\b", r"Sentry", r"AWACS"),
    ),
    WatchedType(
        key="E2D", name="E-2D Advanced Hawkeye", category="support",
        role="Carrier-based airborne early warning and control",
        typecodes=("E2",),
        model_patterns=(r"E-?2D", r"Hawkeye"),
        exclude_patterns=(r"E-?2[ABC]\b",),
        variants={"D": r"E-?2D|Advanced Hawkeye"},
    ),
    WatchedType(
        key="P8", name="P-8A Poseidon", category="support",
        role="Maritime patrol over the Gulf / Strait of Hormuz",
        typecodes=("P8",),
        model_patterns=(r"P-?8", r"Poseidon"),
        exclude_patterns=(r"P-?8I\b", r"Neptune"),  # India's export variant
    ),

    # ---- Drones (UAVs) ----
    WatchedType(
        key="MQ9", name="MQ-9 Reaper", category="drone",
        role="Surveillance and precision strike (heavy reported losses)",
        typecodes=("Q9", "MQ9"),
        model_patterns=(r"MQ-?9", r"Reaper"),
        model_only_ok=True,
        # MQ-9B family (SkyGuardian / SeaGuardian / UK Protector, whose
        # metadata model is just a serial like UHK97000-15) is a different
        # airframe sharing the Q9 designator.
        exclude_patterns=(r"MQ-?9[BC]", r"Sky ?Guardian", r"Sea ?Guardian",
                          r"Protector", r"^U[A-Z]{2}97000"),
    ),
    WatchedType(
        key="MQ4C", name="MQ-4C Triton", category="drone",
        role="High-altitude long-endurance maritime/overland ISR",
        # Q4 is also the RQ-4 Global Hawk, and "Triton" alone matches
        # civilian light aircraft (Triton Sport/XL) -- require "MQ-4C".
        typecodes=("Q4", "MQ4"),
        model_patterns=(r"MQ-?4C",),
        require_model_match=True,
        model_only_ok=True,
    ),
    WatchedType(
        key="RQ180", name="RQ-180", category="drone",
        role="Stealth penetrating ISR in contested airspace",
        typecodes=(),
        adsb_trackable=False,
        notes="Classified program; no transponder, no public metadata.",
    ),
    WatchedType(
        key="LUCAS", name="LUCAS one-way attack drone", category="drone",
        role="Low-cost loitering munition (Shahed-136 class)",
        typecodes=(),
        adsb_trackable=False,
        notes="Expendable munition, no transponder -- only ever a small, "
              "slow, low-altitude radar-only contact.",
    ),
)

# Carriers projecting the naval air power above. Warships don't broadcast
# ADS-B and typically run AIS dark, so these aren't tracked positions --
# reference only, for labeling carrier-air-wing tracks.
CARRIERS = (
    ("CVN-72", "USS Abraham Lincoln"),
    ("CVN-78", "USS Gerald R. Ford"),
    ("CVN-77", "USS George H.W. Bush"),
)

# Types embarked on those carriers (F-35 here means the C variant).
CARRIER_AIR_WING = ("FA18EF", "F35", "EA18G", "E2D")

BY_KEY = {t.key: t for t in WATCHLIST}


def _search(patterns, text):
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def match_metadata(typecode, model):
    """Match one airframe's metadata against WATCHLIST.

    Returns (key, variant) or None. variant is None when the type has no
    variants or the model string doesn't say which one it is."""
    typecode = (typecode or "").strip().upper()
    model = (model or "").strip()
    for t in WATCHLIST:
        if not t.adsb_trackable:
            continue
        # Model-only matching is too loose for the short patterns
        # ("Spirit", "A-10"), so a typecode hit is required unless the
        # type opts into model_only_ok.
        if typecode not in t.typecodes and not (
            t.model_only_ok and _search(t.model_patterns, model)
        ):
            continue
        if t.require_model_match and not _search(t.model_patterns, model):
            continue
        if _search(t.exclude_patterns, model):
            continue
        variant = next(
            (v for v, p in t.variants.items() if re.search(p, model, re.IGNORECASE)),
            None,
        )
        return t.key, variant
    return None


def is_us_military_hex(icao24):
    try:
        return US_MIL_HEX_MIN <= int(icao24, 16) <= US_MIL_HEX_MAX
    except (TypeError, ValueError):
        return False


def _clean(s):
    return (s or "").strip().strip("'\"").strip()


def load_watched_airframes(csv_path):
    """Scan an OpenSky aircraft-database CSV and return {icao24: (key,
    variant)} for watched airframes only -- the full dump is ~500k rows,
    no reason to hold all of it in memory.

    Tolerates both the older double-quoted dumps and the newer
    single-quoted ones (header names come back quoted either way)."""
    watched = {}
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(
            f"aircraft db not found: {csv_path} -- download it from "
            f"https://opensky-network.org/datasets/metadata/ (see "
            f"ingestion/README.md), or drop --aircraft-db"
        )
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        sample = f.read(4096)
        f.seek(0)
        quotechar = "'" if sample.count("'") > sample.count('"') else '"'
        reader = csv.reader(f, quotechar=quotechar)
        header = [_clean(h).lower() for h in next(reader)]
        try:
            i_icao = header.index("icao24")
            i_type = header.index("typecode")
            i_model = header.index("model")
        except ValueError as e:
            raise ValueError(
                f"{csv_path} doesn't look like an OpenSky aircraft database "
                f"(missing column: {e})"
            ) from None
        for row in reader:
            if len(row) <= max(i_icao, i_type, i_model):
                continue
            m = match_metadata(_clean(row[i_type]), _clean(row[i_model]))
            if m:
                watched[_clean(row[i_icao]).lower()] = m
    return watched


class Classifier:
    """icao24 -> (aircraft_type, aircraft_variant) for the poller."""

    def __init__(self, csv_path=None):
        self.watched = {}
        if csv_path:
            self.watched = load_watched_airframes(csv_path)
            print(f"aircraft db: {len(self.watched)} watched airframes "
                  f"loaded from {csv_path}")
        else:
            print(
                "aircraft db: none given (--aircraft-db) -- only flagging the "
                "US military hex block, specific types can't be identified",
                file=sys.stderr,
            )

    def classify(self, icao24):
        icao24 = (icao24 or "").strip().lower()
        m = self.watched.get(icao24)
        if m:
            return m
        if is_us_military_hex(icao24):
            return "US_MIL", None
        return None, None


def label(key, variant=None):
    if key is None:
        return None
    if key == "US_MIL":
        return "US military (type unknown)"
    name = BY_KEY[key].name
    return f"{name} [{variant}]" if variant else name
