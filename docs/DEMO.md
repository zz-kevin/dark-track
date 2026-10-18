# Demo script

A 10-minute walkthrough that shows each part of the project on stored
data, so it doesn't depend on live OpenSky traffic or API credits. Every
number below was checked on 2026-10-16 against the code it describes.

## Before you start

```
./run.sh test          # 48 tests, under a second -- a good first slide
./run.sh api           # http://localhost:8000
```

Open http://localhost:8000. With the live feed stopped it opens on
**Recordings**. On a first visit a short guide appears; its **Show me an
example** button runs step 2 below in one click and follows the plane
automatically (press **?** to bring the guide back).

## 1. The fused picture (2 min)

1. You're on **Recordings**.
2. Choose a recording: **14 Oct · 03:30–03:45 UTC · Persian Gulf · 122
   aircraft**. Simulate an event: **Nothing — just real traffic**.
3. Click **Play this recording**. It runs the real fusion engine and both
   detectors over the stored 15 minutes in about a second.
4. Points to make while it plays (10x):
   - Each arrow is one fused track: ADS-B and synthetic radar combined
     by a Kalman filter. Cyan = both sensors agree.
   - **No alerts.** 136 tracks of real traffic, zero went-dark or
     maneuver alarms -- the false-alarm rate on ordinary traffic is 0%
     on every validation window.
   - Click any aircraft: speed, heading, confidence and its trail.
   - YETI45 (centre of the Gulf, 7,925 m) is labelled **US military
     (type unknown)** -- its transponder address is in the US military
     block (`ingestion/aircraft_types.py`).

## 2. A transponder goes dark (3 min)

1. Simulate an event: **A plane switches its transponder off**; which
   plane: **YETI45**. Play. (Or the guide's **Show me an example**.)
2. Playback starts 90 s before the dropout (03:37:34 UTC) with YETI45
   selected.
3. What happens:
   - At 03:37:34 its ADS-B stops. The radar (non-cooperative, no ID)
     keeps seeing it, and fusion keeps assigning those anonymous
     contacts to YETI45's track.
   - **03:38:28, 54 s later: WENT DARK alert**, the track turns red with
     a pulsing ring. Its last ADS-B fix was 03:37:19, so that's 69 s of
     silence -- just past the 68 s threshold this feed's 45 s cadence
     calls for -- with a radar contact still fresh.
   - It stays flagged, one alert, until the window ends: the radar keeps
     tracking an aircraft that stopped identifying itself.
   - A pop-up says so, with **Follow it**. Following keeps it centred
     and the **Dark aircraft** panel counts up how long it's been dark and
     how far it has flown (about 20 km two minutes in). On the map: a
     hollow circle where its transponder last put it (03:37:19), a dashed
     line to where radar sees it now, a ring for where it could be, and
     where it will be in 5 minutes if it holds course.
4. Same thing in the terminal, scored:

   ```
   ./run.sh harness one went_dark 2026-10-14T03:30:00+00:00 2026-10-14T03:45:00+00:00 --external-id YETI45 --region gulf
   ```

   -> `true positive: latency 54 s`, `false positives (0)`.

## 3. A maneuver (1 min)

Simulate an event: **A plane swerves suddenly (2g maneuver)** on **YETI45**. Play. A sustained 2g push the
filter can't explain: **MANEUVER** alert at 03:39:32, 118 s after it
starts (budget: three 45 s updates). No went-dark alert -- its
transponder is still on.

## 4. How good is it, honestly (3 min)

Numbers from windows nothing was tuned on (`docs/PROBLEM.md`):

| | Target | Result |
|---|---|---|
| Went-dark detection | within silence + 1 update | 94.2-98.3% of injected dropouts |
| Went-dark false alarms | < 5% | 0% |
| Fusion accuracy | 90% of aircraft | Gulf 96.7-100%; NYC 100% airborne, 82.8% all traffic |
| Throughput | 150 tracks, < 500 ms | 100% under 500 ms at ~510 tracks |

This window itself scores 100% (118/118 aircraft, fused error median
24 m):

```
./run.sh accuracy 2026-10-14T03:30:00+00:00 2026-10-14T03:45:00+00:00 --region gulf
./run.sh bench 2026-10-14T06:15:00+00:00 2026-10-14T06:30:00+00:00 --region gulf --copies 4
```

What it can't do, and why -- worth a slide each:

- **NYC taxiing traffic** (the one missed target): aircraft closer
  together than the radar's 150 m noise. 93% of the swapped contacts
  really were likelier under the other aircraft -- no position-based
  rule can do better.
- **Same-level formations** (FRS41/FRS42): inside radar resolution in
  every dimension.
- **Holding patterns at 45 s cadence**: heading changes 40-80 deg
  between updates; an IMM turn model was built and measured, and didn't
  help (`docs/FUSION.md`).

## 5. How it got here (optional, 1 min)

`anomaly/README.md` "How it got here": went-dark recall 6% -> 96-98%,
one measured change per row, each with what it fixed. The decisions
log in `docs/ARCHITECTURE.md` records what was tried and rejected,
including the negative results (IMM, altitude, velocity timestamp).

## Live mode (if there's time and OpenSky credits)

Four terminals, Gulf at 45 s polling:

```
./run.sh poll --region gulf --quiet --poll-seconds 45
./run.sh radar
./run.sh fusion --region gulf
./run.sh anomaly
```

Give it a minute, then switch the UI to **Live**. Real transponder
dropouts are rare; Replay is the reliable way to show one.
