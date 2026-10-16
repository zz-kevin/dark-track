#!/usr/bin/env python3
"""
server.py — Dark Track / api: serves the fused picture, anomaly feed and
replays as JSON, plus web/ as static files on the same origin.

Deliberately thin and dependency-free (stdlib http.server): reads what
fusion/ and anomaly/ already wrote, or re-runs them in memory for a
replay. Read-only against the database.

    python3 server.py                  # http://localhost:8000
    python3 server.py --port 8080

Endpoints:

    GET /api/status
    GET /api/tracks                    current fused picture + open anomalies
    GET /api/tracks/<id>/history       ticks that updated a track
    GET /api/alerts?limit=50           latest anomaly episodes
    GET /api/windows                   stored 15-min windows worth replaying
    GET /api/candidates?start=&end=&region=   callsigns an anomaly can be injected on
    GET /api/replay?start=&end=&region=[&step=6][&inject=went_dark|kinematic&target=CALLSIGN]
"""

import argparse
import json
import re
from collections import OrderedDict
from datetime import date, datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import replay  # before `db`: it loads fusion's own db.py by that name
import db

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
REPLAY_CACHE_SIZE = 8
_replay_cache = OrderedDict()


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def _parse_ts(s):
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _window(q):
    start, end = _parse_ts(q["start"]), _parse_ts(q["end"])
    if not start < end:
        raise ValueError("start must be before end")
    if (end - start).total_seconds() > replay.MAX_WINDOW_S:
        raise ValueError(f"window longer than {replay.MAX_WINDOW_S // 60} min")
    return start, end


def _tracks():
    rows = db.current_tracks()
    for r in rows:
        r["type_label"] = replay.aircraft_types.label(r["aircraft_type"], r["aircraft_variant"])
    return {"tracks": rows, "server_time": datetime.now(timezone.utc)}


def _replay(q):
    start, end = _window(q)
    step = min(max(float(q.get("step", 6)), 1.0), 60.0)
    kind, target = q.get("inject") or None, q.get("target") or None
    region = q.get("region") or None
    key = (start, end, step, kind, target, region)
    if key not in _replay_cache:
        _replay_cache[key] = replay.run(start, end, step, kind, target, region)
        while len(_replay_cache) > REPLAY_CACHE_SIZE:
            _replay_cache.popitem(last=False)
    _replay_cache.move_to_end(key)
    return _replay_cache[key]


def _candidates(q):
    start, end = _window(q)
    ticks = replay.window_ticks(start, end, q.get("region") or None)
    return {"candidates": replay.candidates(ticks)}


ROUTES = [
    (re.compile(r"^/api/status$"), lambda m, q: db.status()),
    (re.compile(r"^/api/tracks$"), lambda m, q: _tracks()),
    (re.compile(r"^/api/tracks/(\d+)/history$"),
     lambda m, q: {"track_id": int(m.group(1)), "history": db.track_history(int(m.group(1)))}),
    (re.compile(r"^/api/alerts$"),
     lambda m, q: {"alerts": db.recent_alerts(min(int(q.get("limit", 50)), 500))}),
    (re.compile(r"^/api/windows$"), lambda m, q: {"windows": db.capture_windows()}),
    (re.compile(r"^/api/candidates$"), lambda m, q: _candidates(q)),
    (re.compile(r"^/api/replay$"), lambda m, q: _replay(q)),
]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return super().do_GET()
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        for pattern, handler in ROUTES:
            m = pattern.match(url.path)
            if m:
                try:
                    return self._send(200, handler(m, query))
                except (KeyError, ValueError) as e:
                    return self._send(400, {"error": f"bad request: {e}"})
                except Exception as e:  # database down, etc.
                    return self._send(503, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "no such endpoint"})

    def _send(self, status, payload):
        body = json.dumps(payload, default=_json_default).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/tracks"):  # polled every few seconds
            super().log_message(fmt, *args)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Dark Track API + web UI on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
