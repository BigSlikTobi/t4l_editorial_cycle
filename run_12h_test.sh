#!/usr/bin/env bash
# 12-hour test run: executes editorial-cycle every hour, 12 times total.
# Logs each run to var/test_runs/. Stops automatically after 12 cycles.
#
# Usage: nohup ./run_12h_test.sh &

set -euo pipefail
cd "$(dirname "$0")"

LOG_DIR="var/test_runs"
mkdir -p "$LOG_DIR"

MAX_RUNS=12
INTERVAL_SECONDS=3600

echo "Starting 12-hour editorial cycle test at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Logs: $LOG_DIR/"
echo "PID: $$"
echo "$$" > "$LOG_DIR/test_run.pid"

for i in $(seq 1 $MAX_RUNS); do
    TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
    RUN_LOG="$LOG_DIR/cycle_${i}_${TIMESTAMP}.log"
    RUN_JSON="$LOG_DIR/cycle_${i}_${TIMESTAMP}.json"

    echo "[$TIMESTAMP] Run $i/$MAX_RUNS starting..." | tee -a "$LOG_DIR/summary.log"

    ./venv/bin/editorial-cycle run --output-json "$RUN_JSON" > "$RUN_LOG" 2>&1
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        # Extract summary line from output
        SUMMARY=$(grep "^Cycle " "$RUN_LOG" || echo "completed")
        echo "[$TIMESTAMP] Run $i/$MAX_RUNS OK: $SUMMARY" | tee -a "$LOG_DIR/summary.log"
    else
        echo "[$TIMESTAMP] Run $i/$MAX_RUNS FAILED (exit $EXIT_CODE). See $RUN_LOG" | tee -a "$LOG_DIR/summary.log"
    fi

    # Don't sleep after the last run
    if [ $i -lt $MAX_RUNS ]; then
        echo "[$TIMESTAMP] Sleeping 1 hour until run $((i+1))..."
        sleep $INTERVAL_SECONDS
    fi
done

echo "12-hour test complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "$LOG_DIR/summary.log"
rm -f "$LOG_DIR/test_run.pid"
