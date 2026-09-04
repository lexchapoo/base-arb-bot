#!/usr/bin/env bash
# Track the Base liquidation market on a schedule.
#
# Measured over 111h: Morpho Blue produced $65,230 of gross liquidation bonus
# ($587/hour) against Aave V3's $336 ($3.02/hour). Morpho is the market; Aave is
# noise. This records both so the trend is visible, plus Aave's health-factor
# bands as a cheap proxy for how much debt sits near its threshold.
#
# Read-only: it queries the local node and signs nothing.
set -u
REPO="/home/shiesty/Desktop/AI Projects/projects/base-arb-bot"
PY="$REPO/python/.venv/bin/python"
LOG="/home/shiesty/scripts/monitor-liquidations.log"
BLOCKS=${BLOCKS:-11000}   # ~6h at 2s/block

cd "$REPO" || exit 1
{
  echo "=== $(date -Is) ==="
  timeout 1800 "$PY" scripts/compare_lending_markets.py --blocks "$BLOCKS" 2>&1 \
    | grep -vE "^\s+[0-9]+/[0-9]+" | grep -E "liquidations|total gross bonus|protocol|morpho|moonwell|aave"
  timeout 1800 "$PY" scripts/collect_liquidations.py --blocks "$BLOCKS" 2>&1 \
    | grep -vE "scanned to|health factors" | grep -E "HF |liquidatable|borrowers with open debt"
  echo "--- morpho pipeline (where the liquidation volume actually is) ---"
  timeout 1800 "$PY" scripts/collect_morpho_pipeline.py 2>&1 \
    | grep -vE "scanned |positions [0-9]+/" | grep -E "health |liquidatable|open borrows|tracked"
} >> "$LOG" 2>&1

# keep the log bounded
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 5000 ]; then
  tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
