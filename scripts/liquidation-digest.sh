#!/usr/bin/env bash
# Daily digest of the 6-hourly liquidation monitor. Runs after the 08:17 collection
# so the morning summary includes that night's runs.
set -u
LOG=/home/shiesty/scripts/liquidation-digest.log
{
  python3 "/home/shiesty/Desktop/AI Projects/projects/base-arb-bot/scripts/liquidation-digest.py" --hours 24
  echo
} >> "$LOG" 2>&1
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 3000 ]; then
  tail -n 3000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
