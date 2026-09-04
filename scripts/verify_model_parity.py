#!/usr/bin/env python3
"""Gate B: prove the MySQL-backed Python fit reproduces the JS fit exactly.

Runs freeze_forecast_snapshot.js (the existing, unmodified Node implementation)
against index.html, then refits from MySQL in Python and recomputes every
prediction in the snapshot. Both sides therefore see identical data, which is
the only way to isolate "is the port faithful?" from "did the inputs change?".

This is deliberately NOT a diff against the checked-in
data/validation/frozen_forecasts_*.json: that artifact was frozen on
2026-09-01, before the CDIP swell backfill added 781 trips to the training set,
so its numbers are legitimately unreproducible from current data. Comparing
against it would fail for a reason that has nothing to do with the port.

Each snapshot prediction records the exact weather inputs used
(waterTempF / swellFt / tideDeltaFt), so the only value this has to rederive is
recent12, which follows the same period-weighted blend the Node script applies.

Usage:
    python3 scripts/verify_model_parity.py                 # generates a fresh snapshot
    python3 scripts/verify_model_parity.py --snapshot P    # reuses one
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as dbmod
from model_fit import fit, load_db_from_mysql
from tune_hyperparameters import build_rows, model_predict, quantile

ROOT = Path(__file__).resolve().parents[1]
FREEZE_SCRIPT = ROOT / "scripts" / "freeze_forecast_snapshot.js"
TOLERANCE = 1e-4
# Seasonal rows' inputs are written via toFixed(3); a 5e-4 input error can move
# the prediction by a few 1e-4 through the fitted coefficients. Observed max is
# 2e-4, so this bound is tight enough to still catch a genuine fit divergence.
SEASONAL_TOLERANCE = 1e-3


def generate_snapshot(destination: Path) -> None:
    subprocess.run(
        ["node", str(FREEZE_SCRIPT), str(destination)],
        check=True, capture_output=True, text=True,
    )


def forecast_recent12(profiles: dict, boat: str, period: str) -> float:
    """Mirror freeze_forecast_snapshot.js's period-weighted recent12 blend."""
    profile = profiles[boat]
    period_profile = profile["periods"].get(period, {})
    n = period_profile.get("n", 0)
    weight = min(1.0, n / 12)
    period_recent = period_profile.get("recent12")
    if period_recent is None:
        period_recent = profile["recent12"]
    return weight * period_recent + (1 - weight) * profile["recent12"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot")
    parser.add_argument("--tolerance", type=float, default=TOLERANCE)
    args = parser.parse_args()

    if args.snapshot:
        snapshot_path = Path(args.snapshot)
    else:
        snapshot_path = Path(tempfile.mkdtemp()) / "snapshot.json"
        print("running freeze_forecast_snapshot.js against index.html...", flush=True)
        generate_snapshot(snapshot_path)

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    predictions = snapshot["predictions"]
    print(f"node snapshot: {len(predictions)} predictions, "
          f"{len(snapshot['eligibleBoats'])} eligible boats, "
          f"trainingDataThrough={snapshot['trainingDataThrough']}")

    connection = dbmod.connect()
    try:
        source = load_db_from_mysql(connection)
    finally:
        connection.close()

    result = fit(build_rows(source), source["trips"], float(snapshot["ridgeLambda"]))
    profiles = source["boatProfiles"]
    print(f"python fit: {result['trainingRows']} training rows, "
          f"{len(result['eligible'])} eligible boats")

    if sorted(result["eligible"]) != sorted(snapshot["eligibleBoats"]):
        print("MISMATCH: eligible boat sets differ")
        print(f"  node:   {sorted(snapshot['eligibleBoats'])}")
        print(f"  python: {sorted(result['eligible'])}")
        sys.exit(1)

    # Two populations, because the snapshot preserves their inputs differently.
    #
    # Live-forecast rows record inputs losslessly, so Python must reproduce Node
    # EXACTLY -- that is the real proof of port fidelity.
    #
    # Seasonal rows are computed from full-precision medians but written out via
    # toFixed(3). Feeding those rounded inputs back in cannot recover the last
    # decimal, so they are checked against a bound derived from that rounding
    # rather than treated as failures. Tightening this would mean re-porting the
    # seasonal median logic into Python -- a second copy to drift, for no signal
    # the live rows do not already give.
    buckets = {
        "live marine forecast": {"limit": args.tolerance, "values": 0, "failures": [], "max": 0.0},
        "seasonal weather outlook": {"limit": SEASONAL_TOLERANCE, "values": 0, "failures": [], "max": 0.0},
    }

    for record in predictions:
        boat, period = record["boat"], record["period"]
        row = {
            "waterTempF": record["waterTempF"],
            "swellFt": record["swellFt"],
            "tideDeltaFt": record["tideDeltaFt"],
            "period": period,
            "recent12": forecast_recent12(profiles, boat, period),
        }
        point = model_predict(result["models"][boat], row)
        boat_residuals = result["residuals"][boat]
        residuals = boat_residuals if len(boat_residuals) >= 30 else result["fleetResiduals"]
        computed = {
            "predictedFishPerAngler": point,
            "typicalLow": max(0.0, point + quantile(residuals, 0.25)),
            "typicalHigh": max(0.0, point + quantile(residuals, 0.75)),
            "planningLow": max(0.0, point + quantile(residuals, 0.10)),
            "planningHigh": max(0.0, point + quantile(residuals, 0.90)),
        }
        bucket = buckets[record["weatherSource"]]
        for key, value in computed.items():
            # The Node side rounds to 4dp on write, so compare at that grain.
            deviation = abs(round(value, 4) - record[key])
            bucket["values"] += 1
            bucket["max"] = max(bucket["max"], deviation)
            if deviation > bucket["limit"]:
                bucket["failures"].append((record["date"], boat, period, key, record[key], round(value, 4)))

    failed = False
    for name, bucket in buckets.items():
        status = "FAIL" if bucket["failures"] else "ok"
        print(f"  {name:26} values={bucket['values']:5d} "
              f"max_dev={bucket['max']:.6f} limit={bucket['limit']:g}  [{status}]")
        if bucket["failures"]:
            failed = True
            for date, boat, period, key, want, got in bucket["failures"][:10]:
                print(f"    {date} {boat:14} {period} {key:22} node={want} python={got}")

    if failed:
        print("\nGATE B FAILED")
        sys.exit(1)

    exact = buckets["live marine forecast"]
    print(f"\nGate B passed. Live-forecast rows -- the ones whose inputs are recorded "
          f"losslessly -- match exactly ({exact['values']} values, max deviation "
          f"{exact['max']:.6f}).")


if __name__ == "__main__":
    main()
