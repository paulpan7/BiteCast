#!/usr/bin/env python3
"""Compare a frozen BiteCast forecast with later reports without retraining the model."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path

import extend_history


ROOT = Path(__file__).resolve().parents[1]


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"matchedWindows": 0}
    errors = [row["error"] for row in rows]
    abs_errors = [abs(error) for error in errors]
    total_anglers = sum(row["anglers"] for row in rows)
    return {
        "matchedWindows": len(rows),
        "anglers": total_anglers,
        "mae": round(sum(abs_errors) / len(rows), 4),
        "rmse": round(math.sqrt(sum(error * error for error in errors) / len(rows)), 4),
        "biasActualMinusForecast": round(sum(errors) / len(rows), 4),
        "anglerWeightedMae": round(sum(abs(row["error"]) * row["anglers"] for row in rows) / total_anglers, 4),
        "within1FishPerAnglerPct": round(100 * sum(error <= 1 for error in abs_errors) / len(rows), 2),
        "within2FishPerAnglerPct": round(100 * sum(error <= 2 for error in abs_errors) / len(rows), 2),
        "typical50CoveragePct": round(100 * sum(row["typicalLow"] <= row["actualFishPerAngler"] <= row["typicalHigh"] for row in rows) / len(rows), 2),
        "planning80CoveragePct": round(100 * sum(row["planningLow"] <= row["actualFishPerAngler"] <= row["planningHigh"] for row in rows) / len(rows), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    start = dt.date.fromisoformat(snapshot["validationStart"])
    end = dt.date.fromisoformat(snapshot["validationEnd"])
    if dt.date.today() <= end and not args.allow_partial:
        raise SystemExit(f"Holdout is still active through {end}. Run after {end} or pass --allow-partial for a provisional report.")

    predictions = {(row["date"], row["boat"], row["period"]): row for row in snapshot["predictions"]}
    actual = defaultdict(lambda: {"encounters": 0, "anglers": 0, "reports": 0})
    through = min(end, dt.date.today() - dt.timedelta(days=1))
    for offset in range((through - start).days + 1):
        day = start + dt.timedelta(days=offset)
        _, source = extend_history.download_fish_day(day)
        trips = extend_history.parse_fish_page(day.isoformat(), Path(source).read_text(encoding="utf-8", errors="replace"))
        for trip in trips:
            if trip.get("landing") in extend_history.EXCLUDED_LANDINGS:
                continue
            key = (trip["date"], trip["boat"], trip["period"])
            if key not in predictions:
                continue
            actual[key]["encounters"] += trip["encounters"]
            actual[key]["anglers"] += trip["anglers"]
            actual[key]["reports"] += 1

    matches = []
    for key, totals in sorted(actual.items()):
        if not totals["anglers"]:
            continue
        prediction = predictions[key]
        actual_rate = totals["encounters"] / totals["anglers"]
        matches.append({
            "date": key[0], "boat": key[1], "period": key[2],
            "reports": totals["reports"], "anglers": totals["anglers"], "fish": totals["encounters"],
            "actualFishPerAngler": round(actual_rate, 4),
            "predictedFishPerAngler": prediction["predictedFishPerAngler"],
            "error": round(actual_rate - prediction["predictedFishPerAngler"], 4),
            "typicalLow": prediction["typicalLow"], "typicalHigh": prediction["typicalHigh"],
            "planningLow": prediction["planningLow"], "planningHigh": prediction["planningHigh"],
            "weatherSource": prediction["weatherSource"],
        })

    by_period = {period: summarize([row for row in matches if row["period"] == period]) for period in ("AM", "PM")}
    by_boat = {boat: summarize([row for row in matches if row["boat"] == boat]) for boat in snapshot["eligibleBoats"]}
    report = {
        "protocol": snapshot["protocol"],
        "snapshotFrozenAt": snapshot["frozenAt"],
        "trainingDataThrough": snapshot["trainingDataThrough"],
        "validationStart": snapshot["validationStart"],
        "validationEnd": snapshot["validationEnd"],
        "evaluatedThrough": through.isoformat(),
        "provisional": through < end,
        "modelWasRetrained": False,
        "overall": summarize(matches),
        "byPeriod": by_period,
        "byBoat": by_boat,
        "matches": matches,
    }
    output = args.output or args.snapshot.with_name(args.snapshot.stem.replace("frozen_forecasts", "validation_report") + ".json")
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["overall"], indent=2))
    print(f"Report written to {output}")


if __name__ == "__main__":
    main()
