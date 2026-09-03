"""Offline hyperparameter sweep for the fleet weather model in index.html.

This is a READ-ONLY research tool. It does not write to index.html, does not
retrain the deployed model, and does not touch data/validation/. It exists
purely to make the choice of ridge lambda and the boat-specific-vs-shared
training cutoff reproducible and revisitable, instead of leaving them as
unexplained magic numbers in the shipped JS.

The deployed model is currently frozen for prospective validation
(data/validation/MODEL_FROZEN.json). This script does not lift that freeze;
re-fitting the shipped model with any parameters this script recommends
requires an explicit user decision to resume model updates.

Replicates, in Python, the exact math of the JS functions in index.html:
weatherFeatures, solveRidge, modelPredict, quantile, buildFleetWeatherModels.
If those functions change, update this file to match or the sweep is testing
stale math.

Usage:
    python3 scripts/tune_hyperparameters.py
    python3 scripts/tune_hyperparameters.py --lambdas 4,8,12,16,24 --cutoffs 12,20,30
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"


def load_db() -> dict:
    source = INDEX.read_text(encoding="utf-8")
    start = source.index("const DB=") + len("const DB=")
    end = source.index(";\n", start)
    return json.loads(source[start:end])


def weather_features(row: dict) -> list[float]:
    w = (row["waterTempF"] - 65) / 10
    s = (row["swellFt"] - 3) / 3
    tide = row.get("tideDeltaFt")
    t = (tide if isinstance(tide, (int, float)) else 0) / 4
    pm = 1.0 if row["period"] == "PM" else 0.0
    recent = min(20, row.get("recent12") or 0) / 5
    return [1.0, w, w * w, s, s * s, pm, recent, pm * w, t]


def solve_ridge(rows: list[dict], lam: float) -> list[float]:
    n = len(weather_features(rows[0]))
    a = [[0.0] * (n + 1) for _ in range(n)]
    for r in rows:
        x = weather_features(r)
        y = r["y"]
        for i in range(n):
            a[i][n] += x[i] * y
            for j in range(n):
                a[i][j] += x[i] * x[j]
    for i in range(1, n):
        a[i][i] += lam
    for i in range(n):
        pivot = i
        for j in range(i + 1, n):
            if abs(a[j][i]) > abs(a[pivot][i]):
                pivot = j
        a[i], a[pivot] = a[pivot], a[i]
        d = a[i][i] or 1e-9
        for j in range(i, n + 1):
            a[i][j] /= d
        for k in range(n):
            if k != i:
                m = a[k][i]
                for j in range(i, n + 1):
                    a[k][j] -= m * a[i][j]
    return [row[n] for row in a]


def model_predict(model: list[float], row: dict) -> float:
    return max(0.0, sum(x * m for x, m in zip(weather_features(row), model)))


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    a = sorted(values)
    p = (len(a) - 1) * q
    i = int(p)
    f = p - i
    hi = a[i + 1] if i + 1 < len(a) else a[i]
    return a[i] + (hi - a[i]) * f


def build_rows(db: dict) -> list[dict]:
    wx = {f"{r['date']}|{r['period']}": r for r in db["analysisRows"]}
    boat_profiles = db["boatProfiles"]
    queues: dict[str, list[float]] = {}
    rows: list[dict] = []
    trips = sorted(db["trips"], key=lambda t: (t["date"], t["period"]))
    for t in trips:
        weather = wx.get(f"{t['date']}|{t['period']}")
        if not weather:
            continue
        wtf, sf, tdf = weather.get("waterTempF"), weather.get("swellFt"), weather.get("tideDeltaFt")
        if not all(isinstance(v, (int, float)) for v in (wtf, sf, tdf)):
            continue
        key = f"{t['boat']}|{t['period']}"
        q = queues.setdefault(key, [])
        recent = sum(q) / len(q) if q else boat_profiles[t["boat"]]["recent12"]
        epa = min(20, t["epa"] or 0)
        rows.append({
            "boat": t["boat"], "date": t["date"], "period": t["period"],
            "waterTempF": wtf, "swellFt": sf, "tideDeltaFt": tdf,
            "recent12": recent, "y": epa,
        })
        q.append(epa)
        if len(q) > 12:
            q.pop(0)
    return rows


def evaluate(rows: list[dict], lam: float, cutoff: int, split_date: str = "2025-01-01") -> dict:
    boat_names = sorted({r["boat"] for r in rows})
    train = [r for r in rows if r["date"] < split_date]
    holdout = [r for r in rows if r["date"] >= split_date]
    global_hold = solve_ridge(train, lam)
    all_residuals: list[float] = []
    dated_residuals: list[tuple[str, float]] = []
    for boat in boat_names:
        boat_train = [r for r in train if r["boat"] == boat]
        boat_test = [r for r in holdout if r["boat"] == boat]
        if not boat_test:
            continue
        own = len(boat_train) >= cutoff
        hold_model = solve_ridge(boat_train, lam) if own else global_hold
        for r in boat_test:
            residual = r["y"] - model_predict(hold_model, r)
            all_residuals.append(residual)
            dated_residuals.append((r["date"], residual))
    errors = [abs(v) for v in all_residuals]
    mae = sum(errors) / len(errors)
    rmse = (sum(e * e for e in errors) / len(errors)) ** 0.5
    within2 = 100 * sum(1 for e in errors if e <= 2) / len(errors)
    dated_residuals.sort(key=lambda d: d[0])
    split_at = len(dated_residuals) // 2
    calibration = [v for _, v in dated_residuals[:split_at]]
    unseen = [v for _, v in dated_residuals[split_at:]]
    q10, q90 = quantile(calibration, 0.1), quantile(calibration, 0.9)
    coverage = (
        100 * sum(1 for v in unseen if q10 <= v <= q90) / len(unseen)
        if unseen else 0.0
    )
    return {
        "lambda": lam, "cutoff": cutoff, "n": len(errors),
        "mae": mae, "rmse": rmse, "within2": within2, "coverage": coverage,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lambdas", default="4,8,12,16,24,32", help="comma-separated ridge lambda candidates")
    parser.add_argument("--cutoffs", default="12,20,30,50", help="comma-separated boat-specific training-size cutoffs")
    args = parser.parse_args()
    lambdas = [float(x) for x in args.lambdas.split(",")]
    cutoffs = [int(x) for x in args.cutoffs.split(",")]

    db = load_db()
    rows = build_rows(db)
    print(f"{len(rows)} weather-matched trips loaded from {INDEX.name}\n")
    print(f"{'lambda':>8} {'cutoff':>8} {'n':>6} {'MAE':>7} {'RMSE':>7} {'within2%':>9} {'coverage%':>10}")
    results = []
    for lam in lambdas:
        for cutoff in cutoffs:
            r = evaluate(rows, lam, cutoff)
            results.append(r)
            print(f"{r['lambda']:>8.1f} {r['cutoff']:>8d} {r['n']:>6d} {r['mae']:>7.4f} {r['rmse']:>7.4f} {r['within2']:>9.2f} {r['coverage']:>10.2f}")

    best_mae = min(results, key=lambda r: r["mae"])
    best_coverage = min(results, key=lambda r: abs(r["coverage"] - 80))
    print(f"\nLowest MAE: lambda={best_mae['lambda']}, cutoff={best_mae['cutoff']} (MAE={best_mae['mae']:.4f})")
    print(f"Closest to 80% coverage: lambda={best_coverage['lambda']}, cutoff={best_coverage['cutoff']} (coverage={best_coverage['coverage']:.2f}%)")
    print("\nCurrently deployed: lambda=12, cutoff=20 (index.html solveRidge/buildFleetWeatherModels).")
    print("This script does not change the deployed model. The MODEL_FROZEN.json")
    print("freeze must be explicitly lifted by the user before any retraining.")


if __name__ == "__main__":
    main()
