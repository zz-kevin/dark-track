"""Track confirmation: radar-only tracks are tentative until CONFIRM_HITS."""

import unittest
from datetime import timedelta

from support import T0, Track, engine, flight, inject, radar_model, simulate


def radar_track(at=T0):
    return Track(0.0, 0.0, at, "radar", 3.0, 150.0 ** 2)


class LifecycleTest(unittest.TestCase):
    def test_radar_track_confirms_on_third_hit(self):
        t = radar_track()
        self.assertFalse(t.confirmed)
        for k in (1, 2):
            t.apply(0.0, 0.0, T0 + timedelta(seconds=10 * k), "radar", 150.0)
        self.assertTrue(t.confirmed)
        self.assertEqual(t.confirmed_at, T0 + timedelta(seconds=20))

    def test_adsb_confirms_at_once(self):
        self.assertTrue(Track(0.0, 0.0, T0, "adsb", 3.0, 50.0 ** 2, external_id="A").confirmed)
        t = radar_track()
        t.apply(0.0, 0.0, T0 + timedelta(seconds=5), "adsb", 50.0)
        self.assertTrue(t.confirmed)

    def test_tentative_tracks_retire_at_the_staleness_gate(self):
        tentative, confirmed = radar_track(), radar_track()
        for k in (1, 2):
            confirmed.apply(0.0, 0.0, T0 + timedelta(seconds=k), "radar", 150.0)
        confirmed.last_update = tentative.last_update = T0
        tracks = {tentative.id: tentative, confirmed.id: confirmed}
        retired = engine.prune_stale(tracks, {}, T0 + timedelta(seconds=40), 120.0, 30.0)
        self.assertEqual(retired, [tentative.id])
        self.assertIn(confirmed.id, tracks)

    def test_tentative_tracks_are_not_persisted(self):
        tentative = radar_track()
        written = {}
        real = engine.db.write_cycle
        engine.db.write_cycle = lambda events, rows, retired: written.update(rows=rows)
        try:
            engine.persist({tentative.id: tentative}, {tentative.id}, [], [], T0)
        finally:
            engine.db.write_cycle = real
        self.assertEqual(written["rows"], [])


class ClutterTest(unittest.TestCase):
    def radar_only_confirmed(self, ticks):
        seen = {}
        for cycle in simulate.run(ticks, simulate.scaled_params(ticks)):
            for t in cycle["tracks"].values():
                if t.adsb_hits == 0 and t.confirmed:
                    seen[t.id] = t
        return seen

    def test_clutter_does_not_confirm_but_a_silent_aircraft_does(self):
        adsb = (flight("AAA1", 25.9, 51.8, 90.0, 220.0, minutes=15)
                + flight("BBB2", 26.2, 51.8, 90.0, 220.0, minutes=15, first_id=1000))
        ticks = radar_model.regenerate(adsb, "gulf", seed=3)
        self.assertGreater(sum(t.get("truth_tick_id") == radar_model.CLUTTER for t in ticks), 100)
        self.assertEqual(self.radar_only_confirmed(ticks), {})
        silent = self.radar_only_confirmed(inject.inject_never_identified(ticks, ["BBB2"]))
        self.assertEqual(len(silent), 1)


if __name__ == "__main__":
    unittest.main()
