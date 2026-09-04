#!/usr/bin/env python3
"""Fit the ridge models from MySQL and store coefficients for the site to serve.

This replaces the ~94 ridge fits the browser currently runs on every page load
(2 fleet + up to 2 per boat x 46 boats, over ~17k joined rows), which is the
only reason the full 10.65 MB corpus has to ship to every visitor.

The math is NOT reimplemented here. weather_features, solve_ridge,
model_predict, quantile and build_rows are imported from tune_hyperparameters,
which is a verified line-for-line port of index.html's solveRidge /
buildFleetWeatherModels. Eligibility and residual rules follow
freeze_forecast_snapshot.js.

Modes:
    --verify-rows   rebuild training rows from MySQL and from the index.html
                    blob, then assert they are identical. This is the sharp
                    test of the MySQL loader: if the ~17k training rows match,
                    everything downstream follows.
    --dry-run       fit and report metrics without writing to the database.
    (default)       fit and persist a new fit_run with coefficients/residuals.

Usage:
    python3 scripts/model_fit.py --verify-rows
    python3 scripts/model_fit.py --dry-run
    python3 scripts/model_fit.py --status live
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as dbmod
from tune_hyperparameters import (
    build_rows,
    load_db,
    model_predict,
    quantile,
    solve_ridge,
)

ROOT = Path(__file__).resolve().parents[1]
FREEZE_MARKER = ROOT / "data" / "validation" / "MODEL_FROZEN.json"

LAMBDA = 12.0
SPLIT_DATE = "2025-01-01"
OWN_MODEL_MIN_ROWS = 20     # per-boat fit threshold (freeze_forecast_snapshot.js)
ELIGIBLE_MIN_ROWS = 30      # boat must have this many training rows to be served
RECENT_WINDOW_DAYS = 180    # ...and a trip within this many days of the latest data


def load_db_from_mysql(connection) -> dict:
    """Return the same dict shape build_rows() expects from the index.html blob.

    Trip order matters and is not incidental. build_rows re-sorts by
    (date, period) with a stable sort, so ties keep their incoming order, and
    two trips sharing a boat/date/period share a recent12 queue -- 90 such
    groups exist. Ordering by (trip_date, boat name, period, trip_no) reproduces
    the blob's own ordering, which is how trip_no was assigned in the first place.
    """
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT t.trip_date, t.period, b.name, l.name, t.anglers, t.kept, t.released,
                   t.encounters, t.epa, t.trip_no
            FROM trip t
            JOIN boat b ON b.boat_id = t.boat_id
            JOIN landing l ON l.landing_id = t.landing_id
            ORDER BY t.trip_date, b.name, t.period, t.trip_no
            """
        )
        trips = [
            {
                "date": row[0].isoformat(),
                "period": row[1],
                "boat": row[2],
                "landing": row[3],
                "anglers": row[4],
                "kept": row[5],
                "released": row[6],
                "encounters": row[7],
                "epa": None if row[8] is None else float(row[8]),
                "tripNo": row[9],
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT obs_date, period, air_temp_f, water_temp_f, pressure_hpa, swell_ft,
                   tide_height_ft, tide_swing_ft, tide_delta_ft
            FROM analysis_row ORDER BY obs_date, period
            """
        )
        analysis_rows = []
        for row in cur.fetchall():
            entry = {"date": row[0].isoformat(), "period": row[1]}
            for key, value in zip(
                ("airTempF", "waterTempF", "pressureHpa", "swellFt",
                 "tideHeightFt", "tideSwingFt", "tideDeltaFt"),
                row[2:],
            ):
                if value is not None:
                    entry[key] = float(value)
            analysis_rows.append(entry)

        cur.execute(
            """
            SELECT b.name, p.trip_count, p.recent12
            FROM boat_profile p JOIN boat b ON b.boat_id = p.boat_id
            """
        )
        profiles = {
            row[0]: {
                "tripCount": row[1],
                "recent12": 0 if row[2] is None else float(row[2]),
                "periods": {},
            }
            for row in cur.fetchall()
        }
        cur.execute(
            """
            SELECT b.name, p.period, p.n, p.recent12
            FROM boat_profile_period p JOIN boat b ON b.boat_id = p.boat_id
            """
        )
        for name, period, n, recent12 in cur.fetchall():
            profiles[name]["periods"][period] = {
                "n": n,
                "recent12": None if recent12 is None else float(recent12),
            }

    return {"trips": trips, "analysisRows": analysis_rows, "boatProfiles": profiles}


def fit(rows: list[dict], trips: list[dict], lam: float = LAMBDA) -> dict:
    """Fit fleet and per-boat models, mirroring freeze_forecast_snapshot.js."""
    train = [r for r in rows if r["date"] < SPLIT_DATE]
    holdout = [r for r in rows if r["date"] >= SPLIT_DATE]
    global_holdout_model = solve_ridge(train, lam)
    global_model = solve_ridge(rows, lam)

    latest = max(t["date"] for t in trips)
    cutoff = (dt.date.fromisoformat(latest) - dt.timedelta(days=RECENT_WINDOW_DAYS)).isoformat()
    recent_boats = {t["boat"] for t in trips if t["date"] >= cutoff}

    by_boat: dict[str, list[dict]] = {}
    for row in rows:
        by_boat.setdefault(row["boat"], []).append(row)

    eligible = sorted(
        boat for boat, boat_rows in by_boat.items()
        if len(boat_rows) >= ELIGIBLE_MIN_ROWS and boat in recent_boats
    )

    models, residuals, fleet_residuals, training_counts = {}, {}, [], {}
    for boat in eligible:
        boat_rows = by_boat[boat]
        training_counts[boat] = len(boat_rows)
        models[boat] = (
            solve_ridge(boat_rows, lam) if len(boat_rows) >= OWN_MODEL_MIN_ROWS else global_model
        )
        boat_train = [r for r in train if r["boat"] == boat]
        boat_test = [r for r in holdout if r["boat"] == boat]
        holdout_model = (
            solve_ridge(boat_train, lam) if len(boat_train) >= OWN_MODEL_MIN_ROWS
            else global_holdout_model
        )
        boat_residuals = [r["y"] - model_predict(holdout_model, r) for r in boat_test]
        residuals[boat] = boat_residuals
        fleet_residuals.extend(boat_residuals)

    errors = [abs(v) for v in fleet_residuals]
    metrics = {
        "n": len(errors),
        "mae": sum(errors) / len(errors) if errors else None,
        "rmse": (sum(e * e for e in errors) / len(errors)) ** 0.5 if errors else None,
        "within2": 100 * sum(1 for e in errors if e <= 2) / len(errors) if errors else None,
    }
    return {
        "globalModel": global_model,
        "models": models,
        "residuals": residuals,
        "fleetResiduals": fleet_residuals,
        "trainingCounts": training_counts,
        "eligible": eligible,
        "metrics": metrics,
        "trainedThrough": latest,
        "trainingRows": len(rows),
    }


def persist(connection, result: dict, lam: float, status: str) -> int:
    metrics = result["metrics"]
    with connection.cursor() as cur:
        cur.execute("SELECT name, boat_id FROM boat")
        boat_ids = dict(cur.fetchall())

        cur.execute(
            """
            INSERT INTO fit_run (fitted_at, lambda_value, split_date, rows_trained,
                                 trained_through, status, mae, rmse, within2, coverage)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
                lam, SPLIT_DATE, result["trainingRows"], result["trainedThrough"], status,
                metrics["mae"], metrics["rmse"], metrics["within2"], None,
            ),
        )
        fit_run_id = cur.lastrowid

        coefficients = [(fit_run_id, 0, i, v) for i, v in enumerate(result["globalModel"])]
        for boat, model in result["models"].items():
            coefficients.extend((fit_run_id, boat_ids[boat], i, v) for i, v in enumerate(model))
        cur.executemany(
            "INSERT INTO model_coef (fit_run_id, boat_id, coef_index, coef_value)"
            " VALUES (%s,%s,%s,%s)",
            coefficients,
        )

        def quantiles(values):
            return (
                quantile(values, 0.10), quantile(values, 0.25),
                quantile(values, 0.75), quantile(values, 0.90),
            )

        rows = [(fit_run_id, 0, result["trainingRows"], len(result["fleetResiduals"]),
                 "fleet", *quantiles(result["fleetResiduals"]))]
        for boat, boat_residuals in result["residuals"].items():
            # Boats with a thin holdout fall back to the fleet residual spread,
            # matching how the snapshot picks its interval source.
            source = boat_residuals if len(boat_residuals) >= 30 else result["fleetResiduals"]
            rows.append((
                fit_run_id, boat_ids[boat],
                result["trainingCounts"][boat], len(boat_residuals),
                "own" if len(boat_residuals) >= 30 else "fleet",
                *quantiles(source),
            ))
        cur.executemany(
            "INSERT INTO model_residual (fit_run_id, boat_id, training_n, validation_n, mode,"
            " q10, q25, q75, q90) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            rows,
        )

        if status == "live":
            cur.execute(
                "UPDATE fit_run SET status='superseded' WHERE status='live' AND fit_run_id <> %s",
                (fit_run_id,),
            )
    connection.commit()
    return fit_run_id


def verify_rows(connection) -> int:
    """Assert MySQL-derived training rows are identical to blob-derived ones."""
    blob_rows = build_rows(load_db())
    sql_rows = build_rows(load_db_from_mysql(connection))
    print(f"blob training rows: {len(blob_rows)}")
    print(f"mysql training rows: {len(sql_rows)}")
    if len(blob_rows) != len(sql_rows):
        print("MISMATCH: row counts differ")
        return 1

    mismatches = []
    for index, (a, b) in enumerate(zip(blob_rows, sql_rows)):
        for key in ("boat", "date", "period", "waterTempF", "swellFt", "tideDeltaFt", "recent12", "y"):
            left, right = a[key], b[key]
            if isinstance(left, float) or isinstance(right, float):
                if abs(float(left) - float(right)) > 1e-9:
                    mismatches.append((index, key, left, right))
            elif left != right:
                mismatches.append((index, key, left, right))
        if len(mismatches) > 10:
            break

    if mismatches:
        print(f"MISMATCH in {len(mismatches)} field(s); first few:")
        for index, key, left, right in mismatches[:10]:
            print(f"  row {index} {key}: blob={left!r} mysql={right!r}")
        return 1
    print("all training rows identical (boat, date, period, weather, recent12, y)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-rows", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lambda", dest="lam", type=float, default=LAMBDA)
    parser.add_argument("--status", choices=["live", "shadow"], default="shadow")
    args = parser.parse_args()

    connection = dbmod.connect()
    try:
        if args.verify_rows:
            sys.exit(verify_rows(connection))

        source = load_db_from_mysql(connection)
        rows = build_rows(source)
        result = fit(rows, source["trips"], args.lam)
        metrics = result["metrics"]
        print(f"training rows: {result['trainingRows']}")
        print(f"eligible boats: {len(result['eligible'])}")
        print(f"holdout n={metrics['n']} mae={metrics['mae']:.4f} "
              f"rmse={metrics['rmse']:.4f} within2={metrics['within2']:.2f}%")

        if args.dry_run:
            print("(dry run -- nothing written)")
            return

        status = args.status
        if status == "live" and FREEZE_MARKER.exists():
            status = "shadow"
            print("freeze marker present -- writing as shadow, not flipping the served pointer")
        fit_run_id = persist(connection, result, args.lam, status)
        print(f"wrote fit_run {fit_run_id} (status={status})")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
