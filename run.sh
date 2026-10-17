#!/bin/sh
# Run any pipeline component from any directory, with the project venv.
#   ./run.sh poll --region gulf --quiet --poll-seconds 45
#   ./run.sh radar | fusion | anomaly | api | replay ... | harness ...
#   ./run.sh accuracy | bench <start> <end> ...   -- fusion evaluation
#   ./run.sh status        -- what's running, track and alert counts
# Long-running components also write their output to logs/<name>.log. If
# one is already running, this shows its live output instead of starting
# a second copy (Ctrl+C stops watching; the component keeps running).
ROOT="$(cd "$(dirname "$0")" && pwd)"
if [ "$1" = status ]; then
    for c in poll:ingestion/opensky_poll.py radar:ingestion/radar_sim.py \
             fusion:fusion/engine.py anomaly:anomaly/engine.py api:api/server.py; do
        name=${c%%:*}; script=${c#*:}
        if pids=$(pgrep -f "$script"); then
            printf "  %-8s running  (pid %s)\n" "$name" "$(echo $pids)"
        else
            printf "  %-8s STOPPED  -> ./run.sh %s\n" "$name" "$name"
        fi
    done
    psql -d dark_track -Atc "
        SELECT '  tracks   ' || count(*) || ' active, '
               || count(aircraft_type) || ' watched type' FROM fused_tracks;
        SELECT '  alerts   ' || count(*) || ' in the last hour'
        FROM anomaly_events WHERE detected_at > now() - interval '1 hour';" 2>/dev/null
    exit 0
fi
case "$1" in
    poll)    script=ingestion/opensky_poll.py ;;
    radar)   script=ingestion/radar_sim.py ;;
    replay)  script=ingestion/replay.py ;;
    fusion)  script=fusion/engine.py ;;
    anomaly) script=anomaly/engine.py ;;
    harness) script=anomaly/harness.py ;;
    accuracy) script=fusion/accuracy.py ;;
    bench)   script=fusion/bench.py ;;
    api)     script=api/server.py ;;
    *) echo "usage: $0 {status|poll|radar|fusion|anomaly|api|replay|harness|accuracy|bench} [args...]" >&2; exit 2 ;;
esac
name=$1
shift
case "$script" in
    *replay.py|*harness.py|*accuracy.py|*bench.py)
        exec "$ROOT/.venv/bin/python" "$ROOT/$script" "$@" ;;
esac

LOG="$ROOT/logs/$name.log"
if pids=$(pgrep -f "$script"); then
    echo "$name is already running (pid $(echo $pids)) -- showing its live output." >&2
    echo "Ctrl+C stops watching; $name keeps running. To restart it: pkill -f $script" >&2
    if [ -f "$LOG" ]; then
        exec tail -n 20 -f "$LOG"
    fi
    echo "(no log yet -- it was started before logging was added; restart it once to get one)" >&2
    exit 1
fi
mkdir -p "$ROOT/logs"
echo "--- started $(date '+%Y-%m-%d %H:%M:%S') ---" >> "$LOG"
# -u: unbuffered, so the log (and anyone tailing it) sees lines immediately.
"$ROOT/.venv/bin/python" -u "$ROOT/$script" "$@" 2>&1 | tee -a "$LOG"
