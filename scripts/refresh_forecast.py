#!/usr/bin/env python3
"""Refresh the 7-day marine forecast embedded in index.html.

The forecast is the forward-looking half of the product and it had been going
stale silently -- no committed job refreshed it, so DB.forecast sat at whatever
date it was last generated, and once its last day passed every day fell through
to the seasonal-climatology outlook without saying so.

WHAT THIS WRITES

Only weather. The page recomputes catch predictions itself: boatForecast() in
index.html reads p.sstF and p.seasFt, derives the tide change, and calls
modelPredict. The epa/score/typicalLow fields stored alongside them are
vestigial -- `.score` is read exactly zero times. So the useful thing to keep
fresh is the input weather, and the predictions follow from it.

SOURCES (all NOAA/NWS)

  waves + wind   api.weather.gov gridpoint SGX/53,12, the marine grid covering
                 the San Diego coastal waters zone (PZZ740). Provides
                 waveHeight, wavePeriod, windSpeed and windDirection, with
                 primarySwellHeight as a backup when waveHeight is sparse.

  sea surface    NDBC buoy 46225 (Torrey Pines Outer), most recent WTMP,
                 carried forward as persistence. NWS does not forecast water
                 temperature, and SST moves slowly enough over a week that
                 persistence is a defensible stand-in.

WHY 46225 AND NOT THE TIDE GAUGE

waterTempF is a model *feature*, so the forecast has to sample it the way the
training data did or the model is fed a biased input. build_analysis_rows takes
WTMP from ljac1 with 46225 as fallback. ljac1's realtime feed is now 404, and
46225 currently reads 72.0F, inside the recent training range of 71.9-74.1F.
The CO-OPS harbor gauge at station 9410170 reads 76.5F for the same moment --
about 4F warmer, being a pier in a bay rather than open water. Using it would
quietly shift a live model input outside the distribution the model learned on.

Usage:
    python3 scripts/refresh_forecast.py              # rewrite index.html
    python3 scripts/refresh_forecast.py --dry-run    # print, change nothing
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extend_history import extract_db, request_bytes

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
PACIFIC = ZoneInfo("America/Los_Angeles")

GRID_URL = "https://api.weather.gov/gridpoints/SGX/53,12"
SST_STATIONS = ("46225", "46258", "46232")  # Torrey Pines Outer first: see module docstring
FORECAST_DAYS = 7

# AM/PM half-day windows in local time, matching enrich_tides() so the forecast
# is bucketed the same way the training rows were.
WINDOWS = {"AM": (6, 12), "PM": (12, 18)}

METERS_TO_FEET = 3.28084
KMH_TO_KNOTS = 0.539957


def iso_duration_hours(text: str) -> float:
    """Hours in an ISO-8601 duration like PT6H, P1D, P1DT2H."""
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", text)
    if not match:
        raise ValueError(f"unparsable duration: {text}")
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    return days * 24 + hours + minutes / 60


def expand(series: dict) -> list[tuple[dt.datetime, float]]:
    """Flatten one NWS gridpoint field into hourly (UTC time, value) samples.

    NWS gives each value a validTime of "<instant>/<duration>", meaning the
    value holds for that whole span. Expanding to hourly lets several fields
    with different span lengths be averaged over the same half-day window.
    """
    out = []
    for entry in series.get("values", []):
        stamp, _, duration = entry["validTime"].partition("/")
        value = entry.get("value")
        if value is None:
            continue
        start = dt.datetime.fromisoformat(stamp)
        for hour in range(max(1, int(iso_duration_hours(duration)))):
            out.append((start + dt.timedelta(hours=hour), float(value)))
    return out


def window_mean(samples: list[tuple[dt.datetime, float]], day: dt.date, period: str):
    """Mean of the samples falling inside one local AM or PM window."""
    start_hour, end_hour = WINDOWS[period]
    start = dt.datetime.combine(day, dt.time(start_hour), tzinfo=PACIFIC)
    end = dt.datetime.combine(day, dt.time(end_hour), tzinfo=PACIFIC)
    inside = [v for t, v in samples if start <= t.astimezone(PACIFIC) < end]
    return sum(inside) / len(inside) if inside else None


def fetch_grid() -> dict:
    payload = json.loads(request_bytes(GRID_URL))
    properties = payload["properties"]
    fields = {}
    for name in ("waveHeight", "primarySwellHeight", "wavePeriod",
                 "windSpeed", "windDirection"):
        fields[name] = expand(properties.get(name) or {})
    return fields


def fetch_sea_surface_temp() -> tuple[float | None, str | None]:
    """Most recent buoy water temperature, in F, with the station that gave it."""
    for station in SST_STATIONS:
        try:
            text = request_bytes(
                f"https://www.ndbc.noaa.gov/data/realtime2/{station}.txt"
            ).decode("ascii", errors="replace")
        except (urllib.error.URLError, OSError):
            continue
        lines = [line for line in text.splitlines() if not line.startswith("#")]
        for line in lines[:12]:  # newest first; tolerate a few missing readings
            parts = line.split()
            if len(parts) < 15:
                continue
            try:
                celsius = float(parts[14])
            except ValueError:
                continue
            if celsius >= 99:  # NDBC missing-data sentinel
                continue
            return round(celsius * 9 / 5 + 32, 1), station
    return None, None


def build_forecast(grid: dict, sst_f: float | None) -> list[dict]:
    today = dt.datetime.now(PACIFIC).date()
    days = []
    for offset in range(FORECAST_DAYS):
        day = today + dt.timedelta(days=offset)
        periods = []
        for period in ("AM", "PM"):
            wave = window_mean(grid["waveHeight"], day, period)
            if wave is None:
                wave = window_mean(grid["primarySwellHeight"], day, period)
            wave_period = window_mean(grid["wavePeriod"], day, period)
            wind = window_mean(grid["windSpeed"], day, period)
            direction = window_mean(grid["windDirection"], day, period)
            periods.append({
                "period": period,
                "windKt": round(wind * KMH_TO_KNOTS) if wind is not None else None,
                "windDir": round(direction) if direction is not None else None,
                "seasFt": round(wave * METERS_TO_FEET, 1) if wave is not None else None,
                "periodSec": round(wave_period, 1) if wave_period is not None else None,
                "sstF": sst_f,
            })
        days.append({"date": day.isoformat(), "dow": day.strftime("%a"), "periods": periods})
    return days


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default=str(INDEX))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    grid = fetch_grid()
    sst_f, station = fetch_sea_surface_temp()
    if sst_f is None:
        raise SystemExit("no usable NDBC water temperature; refusing to write a forecast")
    forecast = build_forecast(grid, sst_f)

    covered = sum(
        1 for day in forecast for p in day["periods"] if p["seasFt"] is not None
    )
    print(f"sea surface temp: {sst_f}F from NDBC {station}")
    print(f"wave coverage: {covered}/{len(forecast) * 2} half-days")
    for day in forecast:
        cells = "  ".join(
            f"{p['period']} seas={p['seasFt']}ft wind={p['windKt']}kt" for p in day["periods"]
        )
        print(f"  {day['date']} {day['dow']}  {cells}")

    if covered == 0:
        raise SystemExit("NWS returned no wave data for any half-day; refusing to write")

    if args.dry_run:
        print("\n(dry run -- index.html untouched)")
        return

    path = Path(args.index)
    source = path.read_text(encoding="utf-8")
    db, start, end = extract_db(source)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    db["forecast"] = forecast
    db["forecastGenerated"] = now
    db["retrieved"] = now
    replacement = json.dumps(db, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    path.write_text(source[:start] + replacement + source[end:], encoding="utf-8")
    print(f"\nwrote {len(forecast)} forecast days to {path.name}")


if __name__ == "__main__":
    main()
