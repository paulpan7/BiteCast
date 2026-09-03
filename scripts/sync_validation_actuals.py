#!/usr/bin/env python3
"""Refresh prospective actual fish counts without changing the frozen model."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import extend_history


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data/validation/frozen_forecasts_2026-09-01_2026-09-28.json"
OUTPUT = ROOT / "data/validation/latest_actuals.json"
ARCHIVE = ROOT / "data/validation/post_freeze_fish_counts.json"
INDEX = ROOT / "index.html"
PACIFIC = ZoneInfo("America/Los_Angeles")


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"matchedWindows": 0}
    total_anglers = sum(row["anglers"] for row in rows)
    errors = [row["error"] for row in rows]
    return {
        "matchedWindows": len(rows),
        "anglers": total_anglers,
        "mae": round(sum(abs(error) for error in errors) / len(errors), 4),
        "rmse": round(math.sqrt(sum(error * error for error in errors) / len(errors)), 4),
        "actualFishPerAngler": round(sum(row["fish"] for row in rows) / total_anglers, 4),
        "predictedFishPerAngler": round(
            sum(row["predictedFishPerAngler"] * row["anglers"] for row in rows) / total_anglers, 4
        ),
    }


def season_day(iso_date: str) -> int:
    _, month, day = map(int, iso_date.split("-"))
    return (dt.date(2000, month, day) - dt.date(2000, 1, 1)).days


def historical_species_shares(rows: list[dict], iso_date: str) -> dict[str, float]:
    target = season_day(iso_date)
    nearby = []
    for trip in rows:
        gap = abs(season_day(trip["date"]) - target)
        if min(gap, 366 - gap) <= 50:
            nearby.append(trip)

    def totals(trips: list[dict]) -> tuple[int, dict[str, int]]:
        species = defaultdict(int)
        total = 0
        for trip in trips:
            total += trip["encounters"]
            for item in trip["species"]:
                species[item["species"]] += item["kept"] + item["released"]
        return total, species

    total, species = totals(nearby)
    if total < 100:
        total, species = totals(rows)
    return {name: round(count / total, 6) for name, count in species.items()} if total else {}


def refresh_page(day: dt.date, use_cache: bool) -> Path:
    """Refresh recent source pages so late dock-total corrections are captured."""
    path = extend_history.FISH_CACHE / f"{day.isoformat()}.html"
    if use_cache and path.exists() and path.stat().st_size >= 500:
        return path
    selected = day.strftime("%m-%d-%Y")
    url = f"{extend_history.BASE}?select={selected}&trip_type_id=1"
    payload = extend_history.request_bytes(url)
    if len(payload) < 500:
        raise RuntimeError(f"Fish report for {day} was unexpectedly short")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".html.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return path


def embed_report(report: dict, index_path: Path) -> None:
    source = index_path.read_text(encoding="utf-8")
    marker = "const MODEL_ACTUALS="
    start = source.find(marker)
    if start < 0:
        raise RuntimeError("index.html is missing the MODEL_ACTUALS marker")
    end = source.find(";\n", start)
    if end < 0:
        raise RuntimeError("MODEL_ACTUALS marker is not terminated")
    compact = json.dumps(report, separators=(",", ":"), ensure_ascii=False)
    index_path.write_text(source[:start] + marker + compact + source[end:], encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--archive", type=Path, default=ARCHIVE)
    parser.add_argument("--days", type=int, default=14, help="Recent source days to re-read")
    parser.add_argument("--through", type=dt.date.fromisoformat)
    parser.add_argument("--use-cache", action="store_true", help="Do not re-download cached source pages")
    parser.add_argument("--embed-index", action="store_true")
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    validation_start = dt.date.fromisoformat(snapshot["validationStart"])
    validation_end = dt.date.fromisoformat(snapshot["validationEnd"])
    actual_start = dt.date.fromisoformat(snapshot["trainingDataThrough"]) + dt.timedelta(days=1)
    today = dt.datetime.now(PACIFIC).date()
    through = min(args.through or today, validation_end)
    predictions = {(row["date"], row["boat"], row["period"]): row for row in snapshot["predictions"]}

    actual = defaultdict(lambda: {"fish": 0, "anglers": 0, "reports": 0, "species": defaultdict(int)})
    observed_trips = []
    refreshed_dates = set()
    not_yet_posted = []
    downloaded_trips = downloaded_fish = downloaded_anglers = 0
    if through >= actual_start:
        source_start = max(actual_start, through - dt.timedelta(days=max(1, args.days) - 1))
        for offset in range((through - source_start).days + 1):
            day = source_start + dt.timedelta(days=offset)
            page = refresh_page(day, args.use_cache)
            text = page.read_text(encoding="utf-8", errors="replace")
            # `day` is always within the refresh window, i.e. recent enough that
            # any previously archived data for it could only have come from this
            # same job -- so marking it refreshed here is safe even when we skip
            # it below: it purges stale fallback-content trips an earlier run may
            # have mislabeled with this date, rather than leaving them in place.
            refreshed_dates.add(day.isoformat())
            # SanDiegoFishReports silently serves the most recent available day's
            # report when the requested day has no data posted yet, rather than an
            # empty page. Treat a mismatch as "not yet reported" instead of
            # mislabeling that stale content as belonging to `day`.
            if extend_history.page_report_date(text) != day.isoformat():
                not_yet_posted.append(day.isoformat())
                continue
            trips = extend_history.parse_fish_page(day.isoformat(), text)
            for trip in trips:
                if trip.get("landing") in extend_history.EXCLUDED_LANDINGS:
                    continue
                downloaded_trips += 1
                downloaded_fish += trip["encounters"]
                downloaded_anglers += trip["anglers"]
                observed_trips.append(trip)
                key = (trip["date"], trip["boat"], trip["period"])
                if key in predictions:
                    actual[key]["fish"] += trip["encounters"]
                    actual[key]["anglers"] += trip["anglers"]
                    actual[key]["reports"] += 1
                    for item in trip["species"]:
                        actual[key]["species"][item["species"]] += item["kept"] + item["released"]

    previous_archive = {"trips": []}
    if args.archive.exists():
        previous_archive = json.loads(args.archive.read_text(encoding="utf-8"))
    archived_trips = [trip for trip in previous_archive.get("trips", []) if trip["date"] not in refreshed_dates]
    archived_trips.extend(observed_trips)
    extend_history.assign_trip_numbers(archived_trips)
    archived_trips.sort(key=lambda trip: (trip["date"], trip["boat"], trip["period"], trip["tripNo"]))
    archive = {
        "modelTrainingDataThrough": snapshot["trainingDataThrough"],
        "actualDataStart": actual_start.isoformat(),
        "actualDataThrough": through.isoformat() if through >= actual_start else None,
        "modelWasRetrained": False,
        "trips": archived_trips,
    }
    args.archive.parent.mkdir(parents=True, exist_ok=True)
    args.archive.write_text(json.dumps(archive, indent=2) + "\n", encoding="utf-8")

    db, _, _ = extend_history.extract_db(INDEX.read_text(encoding="utf-8"))
    history_by_window = defaultdict(list)
    for trip in db["trips"]:
        history_by_window[(trip["boat"], trip["period"])].append(trip)

    matches = []
    for key, totals in sorted(actual.items()):
        if not totals["anglers"]:
            continue
        prediction = predictions[key]
        actual_rate = totals["fish"] / totals["anglers"]
        predicted_rate = prediction["predictedFishPerAngler"]
        shares = historical_species_shares(history_by_window[(key[1], key[2])], key[0])
        matches.append({
            "date": key[0], "boat": key[1], "period": key[2],
            "reports": totals["reports"], "anglers": totals["anglers"], "fish": totals["fish"],
            "actualFishPerAngler": round(actual_rate, 4),
            "predictedFishPerAngler": predicted_rate,
            "error": round(actual_rate - predicted_rate, 4),
            "actualSpecies": dict(sorted(totals["species"].items())),
            "predictedSpeciesFishPerAngler": {
                name: round(predicted_rate * share, 4) for name, share in sorted(shares.items())
            },
        })

    daily_groups = defaultdict(list)
    for row in matches:
        daily_groups[row["date"]].append(row)
    daily = []
    for date, rows in sorted(daily_groups.items()):
        anglers = sum(row["anglers"] for row in rows)
        daily.append({
            "date": date,
            "matchedWindows": len(rows),
            "anglers": anglers,
            "fish": sum(row["fish"] for row in rows),
            "actualFishPerAngler": round(sum(row["fish"] for row in rows) / anglers, 4),
            "predictedFishPerAngler": round(
                sum(row["predictedFishPerAngler"] * row["anglers"] for row in rows) / anglers, 4
            ),
        })

    reported_groups = defaultdict(list)
    for trip in archived_trips:
        reported_groups[trip["date"]].append(trip)
    reported_daily = []
    for date, rows in sorted(reported_groups.items()):
        anglers = sum(row["anglers"] for row in rows)
        fish = sum(row["encounters"] for row in rows)
        reported_daily.append({
            "date": date, "reports": len(rows), "anglers": anglers, "fish": fish,
            "fishPerAngler": round(fish / anglers, 4) if anglers else None,
        })

    report = {
        "protocol": snapshot["protocol"],
        "snapshotFrozenAt": snapshot["frozenAt"],
        "trainingDataThrough": snapshot["trainingDataThrough"],
        "validationStart": snapshot["validationStart"],
        "validationEnd": snapshot["validationEnd"],
        "actualDataStart": actual_start.isoformat(),
        "actualDataThrough": through.isoformat() if through >= actual_start else None,
        "evaluatedThrough": through.isoformat() if through >= validation_start else None,
        "syncedAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "sourceDaysRefreshed": args.days,
        "downloadedTrips": downloaded_trips,
        "downloadedFish": downloaded_fish,
        "downloadedAnglers": downloaded_anglers,
        "modelWasRetrained": False,
        "overall": summarize(matches),
        "reportedDaily": reported_daily,
        "reportedTrips": archived_trips,
        "daily": daily,
        "matches": matches,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.embed_index:
        embed_report(report, INDEX)
    print(f"Synced {downloaded_trips} reports; matched {len(matches)} frozen forecast windows")
    print(f"Actuals written to {args.output} and {args.archive}; modelWasRetrained=false")
    if not_yet_posted:
        print(f"Not yet posted upstream (skipped): {', '.join(not_yet_posted)}")


if __name__ == "__main__":
    main()
