"""The live fusion engine rides out a database outage: no crash, no lost
output, tracks kept in memory."""

import unittest
from unittest import mock

import psycopg2

from support import engine, flight, with_radar


class FakeDb:
    """Just enough of fusion/db.py for engine.run(), serving a fixed set of
    raw ticks a poll's worth at a time. write_cycle fails `fail_writes` times."""

    def __init__(self, ticks, fail_writes):
        # 10 s of data per poll, as live polling would see it (one big
        # batch is the documented backfill case: every ADS-B tick first).
        rows = sorted(ticks, key=lambda t: t["observed_at"])
        t0 = rows[0]["observed_at"]
        self.batches = []
        for t in rows:
            k = int((t["observed_at"] - t0).total_seconds() // 10)
            while len(self.batches) <= k:
                self.batches.append([])
            self.batches[k].append(t)
        self.fail_writes = fail_writes
        self.events_written = []
        self.tracks_written = set()
        self.tracks_retired = set()

    def raw_ticks_since(self, source, since_id, bbox=None):
        self.served = getattr(self, "served", set())
        for batch in self.batches:
            new = [t for t in batch if t["source"] == source and t["id"] not in self.served]
            if new:
                self.served |= {t["id"] for t in new}
                return new
        return []

    def write_cycle(self, events, track_rows, retired_ids):
        if self.fail_writes:
            self.fail_writes -= 1
            raise psycopg2.OperationalError("server closed the connection unexpectedly")
        self.events_written += events
        self.tracks_written |= {r["track_id"] for r in track_rows}
        self.tracks_retired |= set(retired_ids)


class OutageTest(unittest.TestCase):
    def test_writes_catch_up_after_outage(self):
        ticks = with_radar(flight("AAA1", 25.0, 53.0, 90.0, 220.0, minutes=2))
        fake = FakeDb(ticks, fail_writes=2)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 40:
                raise KeyboardInterrupt

        stubs = dict(init_schema=lambda: None,
                     latest_raw_id=lambda source: 0, adsb_update_interval=lambda: 10.0,
                     recent_center=lambda bbox=None: (25.0, 53.0), next_track_id=lambda: 1,
                     clear_fused_tracks=lambda: 0, recent_points=lambda: [(25.0, 53.0)],
                     raw_ticks_since=fake.raw_ticks_since, write_cycle=fake.write_cycle)
        with mock.patch.multiple(engine.db, **stubs), \
                mock.patch.object(engine.time, "sleep", sleep), \
                mock.patch("builtins.print"):
            engine.run(poll_seconds=0.25, gate_dist_m=800, gate_staleness_s=None,
                       track_ttl_s=None, r_adsb_std_m=50, r_radar_std_m=150,
                       accel_std=3.0, since_id_adsb=0, since_id_radar=0)

        self.assertEqual(fake.fail_writes, 0, "outage never happened")
        written = sorted(e["raw_tick_id"] for e in fake.events_written)
        self.assertEqual(written, sorted(t["id"] for t in ticks),
                         "every tick's fusion event reaches the log exactly once")
        # The idle cycles after the data runs out use the wall clock, so the
        # (2026-10-14) track is retired in the same write that catches up:
        # deleted from fused_tracks rather than upserted, which is the
        # right end state.
        self.assertEqual(fake.tracks_written | fake.tracks_retired, {1})


if __name__ == "__main__":
    unittest.main()
