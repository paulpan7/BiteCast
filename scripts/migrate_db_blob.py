#!/usr/bin/env python3
"""Load the embedded `const DB=` blob from index.html into MySQL.

Read-only with respect to index.html. Idempotent: rerunning truncates and
reloads, so it can be run repeatedly while the migration is being validated.

The vocabulary/trip/weather tables are the source of truth. The derived tables
(trip_no, catch_rollup, boat_profile, boat_profile_period) are rebuilt here in
the order they depend on each other -- that order matters, because trip_no is
assigned across the whole sorted set and recent12 replays the last 12 epa values
in date order. Computing either row-locally produces silently wrong features.

Parity is guaranteed by construction rather than reimplementation: fingerprint(),
assign_trip_numbers() and rebuild_profiles() are imported from extend_history
rather than rewritten.

Usage:
    python3 scripts/migrate_db_blob.py [--index index.html] [--verify-only]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as dbmod
from extend_history import (
    ANALYSIS_SPECIES,
    assign_trip_numbers,
    extract_db,
    fingerprint,
    rebuild_profiles,
)

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
BATCH = 2000

# Order matters: children before parents.
TABLES_IN_LOAD_ORDER = [
    "catch_rollup",
    "boat_profile_period",
    "boat_profile",
    "trip_species",
    "trip",
    "analysis_row",
    "species",
    "boat",
    "landing",
]


def fingerprint_hash(trip: dict) -> str:
    """Stable sha1 of extend_history.fingerprint()'s tuple.

    Uses the imported fingerprint() so the dedup identity cannot drift from the
    scraper's. json.dumps gives a canonical, order-stable serialisation of the
    nested species tuples.
    """
    payload = json.dumps(fingerprint(trip), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def load_blob(index_path: Path) -> dict:
    parsed, _, _ = extract_db(index_path.read_text(encoding="utf-8"))
    return parsed


def truncate_all(cur) -> None:
    cur.execute("SET FOREIGN_KEY_CHECKS = 0")
    for table in TABLES_IN_LOAD_ORDER:
        cur.execute(f"TRUNCATE TABLE {table}")
    cur.execute("SET FOREIGN_KEY_CHECKS = 1")


def load_vocabularies(cur, trips: list[dict]) -> tuple[dict, dict, dict]:
    landings = {}
    for trip in trips:
        landings.setdefault(trip["landing"], (trip.get("landing_path"), trip.get("city")))
    cur.executemany(
        "INSERT INTO landing (name, landing_path, city) VALUES (%s, %s, %s)",
        [(name, path, city) for name, (path, city) in sorted(landings.items())],
    )
    cur.execute("SELECT name, landing_id FROM landing")
    landing_ids = dict(cur.fetchall())

    boats = {}
    for trip in sorted(trips, key=lambda t: (t["date"], t["period"])):
        boats[trip["boat"]] = (trip.get("boat_path"), trip["landing"])
    cur.executemany(
        "INSERT INTO boat (name, boat_path, landing_id) VALUES (%s, %s, %s)",
        [(name, path, landing_ids[landing]) for name, (path, landing) in sorted(boats.items())],
    )
    cur.execute("SELECT name, boat_id FROM boat")
    boat_ids = dict(cur.fetchall())

    # The 10 analysis species keep their positional index; the rest get NULL.
    positions = {name: index for index, name in enumerate(ANALYSIS_SPECIES)}
    names = {item["species"] for trip in trips for item in trip["species"]}
    names.update(ANALYSIS_SPECIES)
    cur.executemany(
        "INSERT INTO species (name, analysis_pos) VALUES (%s, %s)",
        [(name, positions.get(name)) for name in sorted(names)],
    )
    cur.execute("SELECT name, species_id FROM species")
    species_ids = dict(cur.fetchall())
    return landing_ids, boat_ids, species_ids


def load_trips(cur, trips, landing_ids, boat_ids, species_ids) -> None:
    # trip_no is derived across the whole sorted set, never row-locally.
    assign_trip_numbers(trips)

    rows = [
        (
            fingerprint_hash(trip),
            trip["date"],
            trip["period"],
            boat_ids[trip["boat"]],
            landing_ids[trip["landing"]],
            trip["anglers"],
            trip["kept"],
            trip["released"],
            trip["encounters"],
            trip["epa"],
            trip["tripNo"],
            trip.get("source_url"),
        )
        for trip in trips
    ]
    statement = (
        "INSERT INTO trip (fingerprint, trip_date, period, boat_id, landing_id, anglers,"
        " kept, released, encounters, epa, trip_no, source_url)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    )
    for start in range(0, len(rows), BATCH):
        cur.executemany(statement, rows[start:start + BATCH])

    cur.execute("SELECT fingerprint, trip_id FROM trip")
    trip_ids = dict(cur.fetchall())

    species_rows = []
    for trip in trips:
        trip_id = trip_ids[fingerprint_hash(trip)]
        for item in trip["species"]:
            species_rows.append((trip_id, species_ids[item["species"]], item["kept"], item["released"]))
    statement = "INSERT INTO trip_species (trip_id, species_id, kept, released) VALUES (%s,%s,%s,%s)"
    for start in range(0, len(species_rows), BATCH):
        cur.executemany(statement, species_rows[start:start + BATCH])
    return len(species_rows)


def load_analysis_rows(cur, rows: list[dict]) -> None:
    payload = [
        (
            row["date"],
            row["period"],
            row.get("airTempF"),
            row.get("waterTempF"),
            row.get("pressureHpa"),
            row.get("swellFt"),
            row.get("tideHeightFt"),
            row.get("tideSwingFt"),
            row.get("tideDeltaFt"),
        )
        for row in rows
    ]
    statement = (
        "INSERT INTO analysis_row (obs_date, period, air_temp_f, water_temp_f, pressure_hpa,"
        " swell_ft, tide_height_ft, tide_swing_ft, tide_delta_ft)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    )
    for start in range(0, len(payload), BATCH):
        cur.executemany(statement, payload[start:start + BATCH])


def rebuild_catch_rollup(cur) -> int:
    """Replaces the positional fish[10] array, aggregated straight from the trips."""
    cur.execute(
        """
        INSERT INTO catch_rollup (obs_date, period, species_id, kept, released, encounters, trips, anglers)
        SELECT t.trip_date, t.period, ts.species_id,
               SUM(ts.kept), SUM(ts.released), SUM(ts.kept + ts.released),
               COUNT(DISTINCT t.trip_id), SUM(t.anglers)
        FROM trip t
        JOIN trip_species ts ON ts.trip_id = t.trip_id
        GROUP BY t.trip_date, t.period, ts.species_id
        """
    )
    return cur.rowcount


def load_boat_profiles(cur, trips, boat_ids, landing_ids) -> None:
    profiles = rebuild_profiles(trips)
    cur.executemany(
        "INSERT INTO boat_profile (boat_id, trip_count, landing_id, recent12) VALUES (%s,%s,%s,%s)",
        [
            (boat_ids[boat], profile["tripCount"], landing_ids[profile["landing"]], profile["recent12"])
            for boat, profile in sorted(profiles.items())
        ],
    )
    period_rows = []
    for boat, profile in sorted(profiles.items()):
        for period, values in profile["periods"].items():
            period_rows.append(
                (
                    boat_ids[boat],
                    period,
                    values["n"],
                    values["recent12"],
                    json.dumps(values["distribution"]),
                )
            )
    cur.executemany(
        "INSERT INTO boat_profile_period (boat_id, period, n, recent12, distribution)"
        " VALUES (%s,%s,%s,%s,%s)",
        period_rows,
    )


def verify(cur, blob: dict) -> list[str]:
    """Assert the database reproduces the blob. Returns a list of failures."""
    failures = []

    def check(label, got, want):
        if got != want:
            failures.append(f"{label}: got {got}, expected {want}")

    cur.execute("SELECT COUNT(*) FROM trip")
    check("trip count", cur.fetchone()[0], len(blob["trips"]))
    cur.execute("SELECT COUNT(DISTINCT fingerprint) FROM trip")
    check("distinct fingerprints", cur.fetchone()[0], len(blob["trips"]))
    cur.execute("SELECT COUNT(*) FROM analysis_row")
    check("analysis_row count", cur.fetchone()[0], len(blob["analysisRows"]))
    cur.execute("SELECT COUNT(*) FROM boat")
    check("boat count", cur.fetchone()[0], len({t["boat"] for t in blob["trips"]}))
    cur.execute("SELECT COUNT(*) FROM trip_species")
    check("trip_species count", cur.fetchone()[0], sum(len(t["species"]) for t in blob["trips"]))

    # Weather columns must round-trip exactly, nulls included.
    for column, key in (
        ("water_temp_f", "waterTempF"),
        ("air_temp_f", "airTempF"),
        ("pressure_hpa", "pressureHpa"),
        ("swell_ft", "swellFt"),
        ("tide_delta_ft", "tideDeltaFt"),
    ):
        cur.execute(f"SELECT COUNT(*) FROM analysis_row WHERE {column} IS NOT NULL")
        expected = sum(1 for row in blob["analysisRows"] if row.get(key) is not None)
        check(f"analysis_row.{column} non-null", cur.fetchone()[0], expected)

    # Gate A, the sharp one: regenerate the positional fish[] array from
    # catch_rollup and compare it to what the blob shipped.
    cur.execute(
        """
        SELECT r.obs_date, r.period, s.analysis_pos, r.encounters
        FROM catch_rollup r JOIN species s ON s.species_id = r.species_id
        WHERE s.analysis_pos IS NOT NULL
        """
    )
    regenerated = {}
    for obs_date, period, pos, encounters in cur.fetchall():
        key = (obs_date.isoformat(), period)
        regenerated.setdefault(key, [0] * len(ANALYSIS_SPECIES))[pos] = int(encounters)

    mismatches = 0
    for row in blob["analysisRows"]:
        want = row["fish"]
        got = regenerated.get((row["date"], row["period"]), [0] * len(ANALYSIS_SPECIES))
        if got != want:
            mismatches += 1
    check("fish[] arrays regenerated from catch_rollup", mismatches, 0)
    return failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default=str(INDEX))
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    blob = load_blob(Path(args.index))
    trips, analysis_rows = blob["trips"], blob["analysisRows"]
    print(f"blob: {len(trips)} trips, {len(analysis_rows)} analysis rows", flush=True)

    connection = dbmod.connect()
    try:
        with connection.cursor() as cur:
            if not args.verify_only:
                truncate_all(cur)
                landing_ids, boat_ids, species_ids = load_vocabularies(cur, trips)
                print(f"vocabularies: {len(landing_ids)} landings, {len(boat_ids)} boats,"
                      f" {len(species_ids)} species", flush=True)
                species_count = load_trips(cur, trips, landing_ids, boat_ids, species_ids)
                print(f"loaded {len(trips)} trips, {species_count} trip_species rows", flush=True)
                load_analysis_rows(cur, analysis_rows)
                print(f"loaded {len(analysis_rows)} analysis rows", flush=True)
                rollup = rebuild_catch_rollup(cur)
                print(f"rebuilt catch_rollup: {rollup} rows", flush=True)
                load_boat_profiles(cur, trips, boat_ids, landing_ids)
                print("rebuilt boat profiles", flush=True)
                connection.commit()

            failures = verify(cur, blob)
        if failures:
            print("\nVERIFY FAILED:", flush=True)
            for failure in failures:
                print(f"  - {failure}", flush=True)
            sys.exit(1)
        print("\nverify: all checks passed", flush=True)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
