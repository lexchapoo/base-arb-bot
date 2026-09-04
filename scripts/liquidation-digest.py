#!/usr/bin/env python3
"""Condense the 6-hourly liquidation monitor into one daily line-item summary.

The monitor writes a full run every 6 hours, which is the right granularity to collect
and the wrong one to read. This parses the last N hours of those runs and reports only
what changed: realised liquidations per protocol, and the movement in each health band.

Band movement is the signal worth watching. A cascade shows up as debt migrating DOWN
into HF 1.00-1.02 before anything becomes liquidatable, and it matters most on Aave,
whose collateral is liquid (WETH, cbBTC) and therefore exitable atomically -- unlike
Morpho's largest concentration, a USDe/USDC loop that sits near its limit by design and
whose collateral would be depegging at exactly the moment it needed selling.
"""
from __future__ import annotations
import argparse, re, sys
from datetime import datetime, timedelta

RUN = re.compile(r"^=== (\d{4}-\d{2}-\d{2}T[\d:+\-]+) ===")
PROTO = re.compile(r"^\s+(morpho|moonwell|aave-v3)\s+([\d,]+)\s+([\d.]+)\s+([\d,]+)\s*$")
AAVE_BAND = re.compile(r"^\s+HF ([\d.]+)-([\d.]+):\s+([\d,]+) positions, \$\s*([\d,]+) debt")
MORPHO_BAND = re.compile(r"^\s+health ([\d.]+)-([\d.]+):\s+([\d,]+) positions, \$\s*([\d,]+) debt")
AAVE_TOTAL = re.compile(r"^\s+([\d,]+) borrowers with open debt, \$([\d,]+) total")
MORPHO_TOTAL = re.compile(r"^\s+([\d,]+) open borrows, \$([\d,]+) debt priced")
LIQUIDATABLE = re.compile(r"^\s+liquidatable right now: (\d+)")


def num(s: str) -> int:
    return int(s.replace(",", ""))


def parse(path: str) -> list[dict]:
    runs: list[dict] = []
    cur: dict | None = None
    section = "aave"
    for line in open(path, errors="ignore"):
        m = RUN.match(line)
        if m:
            cur = {"ts": m.group(1), "realised": {}, "aave": {}, "morpho": {},
                   "aave_total": None, "morpho_total": None, "liquidatable": []}
            runs.append(cur)
            section = "aave"
            continue
        if cur is None:
            continue
        if "morpho pipeline" in line:
            section = "morpho"
            continue
        if (m := PROTO.match(line)):
            cur["realised"][m.group(1)] = num(m.group(2))
        elif (m := AAVE_BAND.match(line)):
            cur["aave"][f"{m.group(1)}-{m.group(2)}"] = (num(m.group(3)), num(m.group(4)))
        elif (m := MORPHO_BAND.match(line)):
            cur["morpho"][f"{m.group(1)}-{m.group(2)}"] = (num(m.group(3)), num(m.group(4)))
        elif (m := AAVE_TOTAL.match(line)):
            cur["aave_total"] = (num(m.group(1)), num(m.group(2)))
        elif (m := MORPHO_TOTAL.match(line)):
            cur["morpho_total"] = (num(m.group(1)), num(m.group(2)))
        elif (m := LIQUIDATABLE.match(line)):
            cur["liquidatable"].append((section, int(m.group(1))))
    return runs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="/home/shiesty/scripts/monitor-liquidations.log")
    ap.add_argument("--hours", type=int, default=24)
    a = ap.parse_args()

    try:
        runs = parse(a.log)
    except FileNotFoundError:
        print(f"no monitor log at {a.log}")
        return 1
    if not runs:
        print("no runs parsed")
        return 1

    cutoff = datetime.now().astimezone() - timedelta(hours=a.hours)
    recent = []
    for r in runs:
        try:
            if datetime.fromisoformat(r["ts"]) >= cutoff:
                recent.append(r)
        except ValueError:
            continue
    if not recent:
        recent = runs[-1:]

    print(f"=== liquidation digest {datetime.now().astimezone().isoformat(timespec='seconds')} ===")
    print(f"  {len(recent)} monitor run(s) in the last {a.hours}h "
          f"({recent[0]['ts']} -> {recent[-1]['ts']})")

    tot = {}
    for r in recent:
        for k, v in r["realised"].items():
            tot[k] = tot.get(k, 0) + v
    if tot:
        print("\n  realised liquidations observed:")
        for k, v in sorted(tot.items(), key=lambda kv: -kv[1]):
            print(f"    {k:10} {v:>5}")
        if not any(tot.values()):
            print("    (none -- liquidations are bursty; 0 over 24h is normal)")

    alerts = []
    for label, key in (("aave", "aave"), ("morpho", "morpho")):
        # Baseline against the earliest run that actually reported this protocol, not
        # blindly against recent[0]. Early runs predate the Morpho pipeline and the
        # borrower-accumulation fix, so comparing to them reported a field appearing for
        # the first time as a +$167M surge -- an artefact that would fire an alert every
        # time the monitor gained a capability.
        having = [r for r in recent if r.get(key)]
        if not having:
            continue
        first, last = having[0][key], having[-1][key]
        comparable = len(having) > 1
        print(f"\n  {label} health bands (first -> last):")
        for band in sorted(set(first) | set(last)):
            lp, ld = last.get(band, (0, 0))
            if not comparable or band not in first:
                print(f"    {band:>10}: {lp:>5} pos  ${ld:>14,}   (no baseline)")
                continue
            fd = first[band][1]
            delta = ld - fd
            arrow = "  " if delta == 0 else ("UP" if delta > 0 else "DN")
            pct = (delta / fd * 100) if fd else 0.0
            print(f"    {band:>10}: {lp:>5} pos  ${ld:>14,}  {arrow} ${delta:+,} ({pct:+.1f}%)")
            # Debt migrating into the tightest band is the cascade signal. Require a
            # material move so ordinary interest accrual does not page anyone.
            if band.startswith("1.00") and fd > 0 and delta > 0 and pct >= 25:
                alerts.append(f"{label}: debt entering {band} +${delta:,} ({pct:+.0f}%)")
        tk = f"{label}_total"
        if recent[-1].get(tk):
            n, d = recent[-1][tk]
            print(f"    total     : {n:>5} borrowers  ${d:>14,}")

    liq = [(r["ts"], s, n) for r in recent for s, n in r["liquidatable"] if n > 0]
    if liq:
        alerts.append(f"{len(liq)} observation(s) with positions liquidatable")
        print("\n  LIQUIDATABLE POSITIONS SEEN:")
        for ts, s, n in liq:
            print(f"    {ts}  {s}: {n}")
    else:
        print("\n  liquidatable right now: 0 across all runs")

    print("\n  " + ("ALERTS: " + "; ".join(alerts) if alerts else "no alerts"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
