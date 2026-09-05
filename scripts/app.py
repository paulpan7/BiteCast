#!/usr/bin/env python3
"""BiteCast Flask application: the SPA shell plus JSON endpoints backed by MySQL.

Replaces the static-mirror launcher. The page shell (0.21 MB) is still served
from disk; what changes is that the 10.65 MB `const DB=` literal no longer has
to be inlined, because the tabs fetch only the slices they need and the ridge
coefficients arrive pre-fitted instead of being refit in every visitor's browser.

Endpoints fall into two groups:
  precomputed  -- served from derived tables, cacheable, small
  live query   -- /api/trips, paged, the only one that touches raw trip rows

Run locally:
    BITECAST_DB_NAME=bitecast_test .venv/bin/flask --app scripts/app run --port 5001
"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as dbmod

SITE_DIR = Path(__file__).resolve().parents[1]
PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 500

app = Flask(__name__, static_folder=None)


def query(sql: str, params: tuple = (), one: bool = False):
    connection = dbmod.connect(dict_rows=True)
    try:
        with connection.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        connection.close()
    return (rows[0] if rows else None) if one else rows


def jsonify_cached(payload, seconds: int = 3600):
    response = jsonify(payload)
    response.headers["Cache-Control"] = f"public, max-age={seconds}"
    return response


def isoformat_dates(rows: list[dict]) -> list[dict]:
    for row in rows:
        for key, value in row.items():
            if isinstance(value, dt.date):
                row[key] = value.isoformat()
            elif isinstance(value, dt.datetime):
                row[key] = value.isoformat()
    return rows


# ----------------------------------------------------------------- static shell

@app.route("/")
def index():
    return send_from_directory(SITE_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(SITE_DIR, path)


# -------------------------------------------------------------- precomputed API

@app.route("/api/bootstrap")
def bootstrap():
    """Dropdown vocabularies and corpus bounds -- replaces four full scans of
    DB.trips the page currently does just to populate its selects."""
    boats = query("SELECT b.name FROM boat b ORDER BY b.name")
    landings = query("SELECT name FROM landing ORDER BY name")
    species = query(
        "SELECT name, analysis_pos FROM species ORDER BY analysis_pos IS NULL, analysis_pos, name"
    )
    bounds = query(
        "SELECT MIN(trip_date) first_date, MAX(trip_date) last_date, COUNT(*) trips FROM trip",
        one=True,
    )
    years = query(
        "SELECT YEAR(trip_date) y, COUNT(*) n FROM trip GROUP BY YEAR(trip_date) ORDER BY y"
    )
    return jsonify_cached({
        "boats": [row["name"] for row in boats],
        "landings": [row["name"] for row in landings],
        "species": [row["name"] for row in species],
        "analysisSpecies": [row["name"] for row in species if row["analysis_pos"] is not None],
        "firstDate": bounds["first_date"].isoformat() if bounds["first_date"] else None,
        "maxDate": bounds["last_date"].isoformat() if bounds["last_date"] else None,
        "tripCount": bounds["trips"],
        "tripsByYear": {str(row["y"]): row["n"] for row in years},
    })


@app.route("/api/db")
def full_db():
    """The whole dataset in the shape the page's `const DB=` literal had.

    This is what lets the 10.65 MB literal leave index.html without touching a
    single consumer in the page: the shape is identical, only the delivery
    changes. DB.stats and DB.weatherValidation are omitted -- both are read
    exactly zero times by the page's JS.

    It is deliberately the heaviest endpoint. The narrower ones (/api/bootstrap,
    /api/models, /api/analysis) exist to retire it consumer by consumer.
    """
    connection = dbmod.connect(dict_rows=True)
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT t.trip_id, t.trip_date, t.period, t.anglers, t.kept, t.released,
                       t.encounters, t.epa, t.trip_no, t.source_url,
                       b.name AS boat, b.boat_path, l.name AS landing,
                       l.landing_path, l.city
                FROM trip t
                JOIN boat b ON b.boat_id = t.boat_id
                JOIN landing l ON l.landing_id = t.landing_id
                ORDER BY t.trip_date, b.name, t.period, t.trip_no
                """
            )
            trip_rows = cur.fetchall()

            cur.execute(
                """
                SELECT ts.trip_id, s.name, ts.kept, ts.released
                FROM trip_species ts JOIN species s ON s.species_id = ts.species_id
                ORDER BY ts.trip_id, ts.position
                """
            )
            species_by_trip: dict[int, list] = {}
            for row in cur.fetchall():
                species_by_trip.setdefault(row["trip_id"], []).append(
                    {"species": row["name"], "kept": row["kept"], "released": row["released"]}
                )

            cur.execute(
                "SELECT name FROM species WHERE analysis_pos IS NOT NULL ORDER BY analysis_pos"
            )
            analysis_species = [row["name"] for row in cur.fetchall()]

            cur.execute(
                """
                SELECT a.obs_date, a.period, a.air_temp_f, a.water_temp_f, a.pressure_hpa,
                       a.swell_ft, a.tide_height_ft, a.tide_swing_ft, a.tide_delta_ft
                FROM analysis_row a ORDER BY a.obs_date, a.period
                """
            )
            weather_rows = cur.fetchall()

            # fish[] is a projection over analysis_pos, not stored.
            cur.execute(
                """
                SELECT r.obs_date, r.period, s.analysis_pos, r.encounters
                FROM catch_rollup r JOIN species s ON s.species_id = r.species_id
                WHERE s.analysis_pos IS NOT NULL
                """
            )
            fish: dict[tuple, list] = {}
            for row in cur.fetchall():
                key = (row["obs_date"], row["period"])
                fish.setdefault(key, [0] * len(analysis_species))[row["analysis_pos"]] = int(
                    row["encounters"]
                )

            cur.execute(
                """
                SELECT b.name, p.trip_count, p.recent12, l.name AS landing
                FROM boat_profile p JOIN boat b ON b.boat_id = p.boat_id
                LEFT JOIN landing l ON l.landing_id = p.landing_id
                """
            )
            profiles = {
                row["name"]: {
                    "tripCount": row["trip_count"],
                    "landing": row["landing"],
                    "periods": {},
                    "recent12": float(row["recent12"]) if row["recent12"] is not None else 0,
                }
                for row in cur.fetchall()
            }
            cur.execute(
                """
                SELECT b.name, p.period, p.n, p.recent12, p.distribution
                FROM boat_profile_period p JOIN boat b ON b.boat_id = p.boat_id
                """
            )
            for row in cur.fetchall():
                distribution = row["distribution"]
                if isinstance(distribution, (str, bytes)):
                    distribution = json.loads(distribution)
                profiles[row["name"]]["periods"][row["period"]] = {
                    "n": row["n"],
                    "recent12": float(row["recent12"]) if row["recent12"] is not None else None,
                    "distribution": distribution or [],
                }

            cur.execute("SELECT meta_key, meta_value FROM site_meta")
            meta = {row["meta_key"]: row["meta_value"] for row in cur.fetchall()}
    finally:
        connection.close()

    def number(value):
        return float(value) if value is not None else None

    trips = [
        {
            "date": row["trip_date"].isoformat(),
            "period": row["period"],
            "anglers": row["anglers"],
            "source_url": row["source_url"],
            "boat": row["boat"],
            "boat_path": row["boat_path"],
            "landing": row["landing"],
            "landing_path": row["landing_path"],
            "city": row["city"],
            "species": species_by_trip.get(row["trip_id"], []),
            "kept": row["kept"],
            "released": row["released"],
            "encounters": row["encounters"],
            "epa": number(row["epa"]),
            "tripNo": row["trip_no"],
        }
        for row in trip_rows
    ]

    analysis_rows = []
    for row in weather_rows:
        entry = {
            "date": row["obs_date"].isoformat(),
            "period": row["period"],
            "fish": fish.get((row["obs_date"], row["period"]), [0] * len(analysis_species)),
        }
        for key, column in (
            ("airTempF", "air_temp_f"), ("waterTempF", "water_temp_f"),
            ("pressureHpa", "pressure_hpa"), ("swellFt", "swell_ft"),
            ("tideHeightFt", "tide_height_ft"), ("tideSwingFt", "tide_swing_ft"),
            ("tideDeltaFt", "tide_delta_ft"),
        ):
            if row[column] is not None:
                entry[key] = float(row[column])
        analysis_rows.append(entry)

    forecast_payload = forecast().get_json()
    forecast_days = []
    for day in forecast_payload["days"]:
        weekday = dt.date.fromisoformat(day["date"]).strftime("%a")
        forecast_days.append({"date": day["date"], "dow": weekday, "periods": day["periods"]})

    return jsonify_cached({
        "trips": trips,
        "analysisRows": analysis_rows,
        "boatProfiles": profiles,
        "forecast": forecast_days,
        "analysisSpecies": analysis_species,
        "forecastGenerated": meta.get("forecastGenerated"),
        "retrieved": meta.get("retrieved"),
    }, seconds=900)


@app.route("/api/db.js")
def full_db_script():
    """The same payload as /api/db, as a classic script that assigns a global.

    index.html loads this with a plain <script src>, which blocks parsing until
    it has run, so the inline script that follows still sees its data as an
    ordinary synchronous value. That matters: the page's two inline scripts
    share one top-level script scope, and the second borrows helpers (fmt, esc)
    declared in the first. Switching the first to a module to allow a top-level
    `await fetch` would move those declarations out of reach and break the
    FleetTrack panel, with the failure swallowed by its own .catch.
    """
    payload = full_db().get_json()
    body = "window.__BITECAST_DB__=" + json.dumps(payload, separators=(",", ":")) + ";\n"
    response = app.response_class(body, mimetype="application/javascript")
    response.headers["Cache-Control"] = "public, max-age=900"
    return response


@app.route("/api/models")
def models():
    """Fitted coefficients and residual quantiles -- this is what replaces the
    ~94 ridge fits the browser runs on load."""
    run = query(
        "SELECT * FROM fit_run WHERE status='live' ORDER BY fitted_at DESC LIMIT 1", one=True
    )
    if not run:
        return jsonify({"error": "no live fit_run"}), 503

    coefficients = query(
        """
        SELECT COALESCE(b.name, '__fleet__') AS boat, c.coef_index, c.coef_value
        FROM model_coef c LEFT JOIN boat b ON b.boat_id = c.boat_id
        WHERE c.fit_run_id = %s ORDER BY c.boat_id, c.coef_index
        """,
        (run["fit_run_id"],),
    )
    grouped: dict[str, list[float]] = {}
    for row in coefficients:
        grouped.setdefault(row["boat"], []).append(float(row["coef_value"]))

    residuals = query(
        """
        SELECT COALESCE(b.name, '__fleet__') AS boat, r.training_n, r.validation_n,
               r.mode, r.q10, r.q25, r.q75, r.q90
        FROM model_residual r LEFT JOIN boat b ON b.boat_id = r.boat_id
        WHERE r.fit_run_id = %s
        """,
        (run["fit_run_id"],),
    )
    intervals = {
        row["boat"]: {
            "trainingN": row["training_n"],
            "validationN": row["validation_n"],
            "mode": row["mode"],
            "q10": float(row["q10"]), "q25": float(row["q25"]),
            "q75": float(row["q75"]), "q90": float(row["q90"]),
        }
        for row in residuals
    }
    return jsonify_cached({
        "fitRunId": run["fit_run_id"],
        "fittedAt": run["fitted_at"].isoformat(),
        "lambda": float(run["lambda_value"]),
        "splitDate": run["split_date"].isoformat(),
        "trainedThrough": run["trained_through"].isoformat() if run["trained_through"] else None,
        "rowsTrained": run["rows_trained"],
        "featureOrder": ["intercept", "waterTemp", "waterTempSquared", "swell",
                         "swellSquared", "PM", "recent12", "PMxWaterTemp", "tideChange"],
        "metrics": {
            "mae": float(run["mae"]) if run["mae"] is not None else None,
            "rmse": float(run["rmse"]) if run["rmse"] is not None else None,
            "within2": float(run["within2"]) if run["within2"] is not None else None,
        },
        "coefficients": grouped,
        "intervals": intervals,
    })


@app.route("/api/forecast")
def forecast():
    rows = query(
        """
        SELECT obs_date, period, epa, score, typical_low, typical_high, planning_low,
               planning_high, range_n, wind_kt, wind_dir, seas_ft, period_sec, sst_f,
               generated_at
        FROM forecast_period ORDER BY obs_date, period
        """
    )
    days: dict[str, dict] = {}
    generated = None
    for row in rows:
        date = row["obs_date"].isoformat()
        generated = generated or (row["generated_at"].isoformat() if row["generated_at"] else None)
        day = days.setdefault(date, {"date": date, "periods": []})
        day["periods"].append({
            "period": row["period"],
            "epa": float(row["epa"]) if row["epa"] is not None else None,
            "score": float(row["score"]) if row["score"] is not None else None,
            "typicalLow": float(row["typical_low"]) if row["typical_low"] is not None else None,
            "typicalHigh": float(row["typical_high"]) if row["typical_high"] is not None else None,
            "planningLow": float(row["planning_low"]) if row["planning_low"] is not None else None,
            "planningHigh": float(row["planning_high"]) if row["planning_high"] is not None else None,
            "rangeN": row["range_n"],
            "windKt": row["wind_kt"],
            "windDir": row["wind_dir"],
            "seasFt": float(row["seas_ft"]) if row["seas_ft"] is not None else None,
            "periodSec": float(row["period_sec"]) if row["period_sec"] is not None else None,
            "sstF": float(row["sst_f"]) if row["sst_f"] is not None else None,
        })
    return jsonify_cached({"generatedAt": generated, "days": list(days.values())})


@app.route("/api/analysis")
def analysis():
    """Weather series joined to the catch rollup, at the requested grain."""
    period = request.args.get("period", "all")
    species = request.args.get("species", "all")
    start, end = request.args.get("from"), request.args.get("to")

    clauses, params = ["1=1"], []
    if period in ("AM", "PM"):
        clauses.append("a.period = %s")
        params.append(period)
    if start:
        clauses.append("a.obs_date >= %s")
        params.append(start)
    if end:
        clauses.append("a.obs_date <= %s")
        params.append(end)

    if species == "all":
        catch = "(SELECT SUM(r.encounters) FROM catch_rollup r WHERE r.obs_date=a.obs_date AND r.period=a.period)"
    else:
        catch = ("(SELECT SUM(r.encounters) FROM catch_rollup r JOIN species s ON s.species_id=r.species_id"
                 " WHERE r.obs_date=a.obs_date AND r.period=a.period AND s.name=%s)")
        params.insert(0, species)

    rows = query(
        f"""
        SELECT a.obs_date, a.period, a.water_temp_f, a.air_temp_f, a.pressure_hpa,
               a.swell_ft, a.tide_delta_ft, a.tide_swing_ft, {catch} AS encounters
        FROM analysis_row a WHERE {' AND '.join(clauses)}
        ORDER BY a.obs_date, a.period
        """,
        tuple(params),
    )
    for row in rows:
        for key in ("water_temp_f", "air_temp_f", "pressure_hpa", "swell_ft",
                    "tide_delta_ft", "tide_swing_ft"):
            if row[key] is not None:
                row[key] = float(row[key])
        row["encounters"] = int(row["encounters"] or 0)
    return jsonify_cached({"rows": isoformat_dates(rows)})


# ------------------------------------------------------------------ live query

@app.route("/api/trips")
def trips():
    """Paged raw trips. The only endpoint that touches trip rows directly, and
    the only thing the (currently unreachable) #historical panel needs."""
    clauses, params = ["1=1"], []
    for arg, sql in (("boat", "b.name = %s"), ("landing", "l.name = %s"),
                     ("period", "t.period = %s")):
        value = request.args.get(arg)
        if value and value != "all":
            clauses.append(sql)
            params.append(value)
    if request.args.get("from"):
        clauses.append("t.trip_date >= %s")
        params.append(request.args["from"])
    if request.args.get("to"):
        clauses.append("t.trip_date <= %s")
        params.append(request.args["to"])
    if request.args.get("year"):
        clauses.append("YEAR(t.trip_date) = %s")
        params.append(request.args["year"])
    species = request.args.get("species")
    if species and species != "all":
        clauses.append(
            "EXISTS (SELECT 1 FROM trip_species ts JOIN species s ON s.species_id=ts.species_id"
            " WHERE ts.trip_id=t.trip_id AND s.name=%s)"
        )
        params.append(species)

    where = " AND ".join(clauses)
    joins = "FROM trip t JOIN boat b ON b.boat_id=t.boat_id JOIN landing l ON l.landing_id=t.landing_id"
    total = query(f"SELECT COUNT(*) AS n {joins} WHERE {where}", tuple(params), one=True)["n"]

    try:
        page = max(1, int(request.args.get("page", 1)))
        size = min(PAGE_SIZE_MAX, max(1, int(request.args.get("pageSize", PAGE_SIZE_DEFAULT))))
    except ValueError:
        return jsonify({"error": "page and pageSize must be integers"}), 400
    direction = "ASC" if request.args.get("sort") == "asc" else "DESC"

    rows = query(
        f"""
        SELECT t.trip_date, t.period, b.name AS boat, l.name AS landing, t.anglers,
               t.kept, t.released, t.encounters, t.epa, t.trip_no
        {joins} WHERE {where}
        ORDER BY t.trip_date {direction}, b.name, t.period, t.trip_no
        LIMIT %s OFFSET %s
        """,
        tuple(params) + (size, (page - 1) * size),
    )
    for row in rows:
        row["epa"] = float(row["epa"]) if row["epa"] is not None else None
    return jsonify({
        "rows": isoformat_dates(rows),
        "total": total,
        "page": page,
        "pageSize": size,
    })


# ---------------------------------------------------------------------- ingest

@app.route("/api/ingest/boat-tracks", methods=["POST"])
def ingest_boat_tracks():
    """Receives the ShipFinder scrape from GitHub Actions.

    Playwright cannot run on PythonAnywhere (README.md), so that one job stays
    on Actions and POSTs its result here rather than committing it to git.
    Idempotent: replaying a payload updates in place and reports 0 inserted.
    """
    expected = os.environ.get("BITECAST_INGEST_TOKEN")
    if not expected:
        return jsonify({"error": "ingest not configured"}), 503
    supplied = request.headers.get("Authorization", "")
    if not supplied.startswith("Bearer ") or not hmac.compare_digest(supplied[7:], expected):
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("tracks"), list):
        return jsonify({"error": "expected {tracks: [...]}"}), 400

    rows = []
    for track in payload["tracks"]:
        try:
            rows.append((
                str(track["mmsi"]),
                track.get("day") or payload.get("day"),
                track["ts"],
                round(float(track["lat"]), 5),
                round(float(track["lon"]), 5),
                track.get("speedKt"),
            ))
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "each track needs mmsi, ts, lat, lon"}), 400

    connection = dbmod.connect()
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM boat_track")
            before = cur.fetchone()[0]
            if rows:
                cur.executemany(
                    "INSERT INTO boat_track (mmsi, track_day, ts, lat, lon, speed_kt)"
                    " VALUES (%s,%s,%s,%s,%s,%s) AS new"
                    " ON DUPLICATE KEY UPDATE speed_kt = COALESCE(boat_track.speed_kt, new.speed_kt)",
                    rows,
                )
            cur.execute("SELECT COUNT(*) FROM boat_track")
            after = cur.fetchone()[0]
            inserted = after - before
            cur.execute(
                "INSERT INTO ingest_log (received_at, source, payload_day, inserted, updated, skipped)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
                 payload.get("source", "shipfinder"), payload.get("day"),
                 inserted, len(rows) - inserted, 0),
            )
        connection.commit()
    finally:
        connection.close()
    return jsonify({"inserted": inserted, "updated": len(rows) - inserted, "skipped": 0})


@app.route("/api/health")
def health():
    try:
        row = query("SELECT COUNT(*) AS n FROM trip", one=True)
        return jsonify({"ok": True, "trips": row["n"]})
    except Exception as error:  # surfaced so a broken DB config is obvious
        return jsonify({"ok": False, "error": str(error)}), 503


application = app

if __name__ == "__main__":
    # No reloader: it forks a child process, which confuses external process
    # managers into thinking the server never bound.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5001)), use_reloader=False)
