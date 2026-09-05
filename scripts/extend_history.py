#!/usr/bin/env python3
"""Extend FleetCast's embedded half-day archive with historical fish and NOAA data."""

from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import datetime as dt
import gzip
import html
import json
import math
import re
import statistics
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
CACHE = ROOT / "data" / "cache"
FISH_CACHE = CACHE / "fish"
NOAA_CACHE = CACHE / "noaa"
CDIP_CACHE = CACHE / "cdip"
MODEL_FREEZE_MARKER = ROOT / "data" / "validation" / "MODEL_FROZEN.json"
BASE = "https://www.sandiegofishreports.com/dock_totals/boats.php"
USER_AGENT = "FleetCast historical research refresh/1.0"
TIDE_STATION = "9410170"
EXCLUDED_LANDINGS = {
    "Oceanside Sea Center",
    "Sea Star Charters",
    "Sea Star Sportfishing",
}
ANALYSIS_SPECIES = [
    "Rockfish", "Calico Bass", "Sand Bass", "Whitefish", "Bonito",
    "Sculpin", "Vermilion Rockfish", "Sheephead", "Barracuda", "Yellowtail",
]
NAME_MAP = {
    "Bluefin Tuna": "Bluefin",
    "Yellowfin Tuna": "Yellowfin",
    "Skipjack Tuna": "Skipjack",
    "Barred Sand Bass": "Sand Bass",
    "California Yellowtail": "Yellowtail",
}


def require_model_updates_unfrozen() -> None:
    """Block any index mutation while prospective validation is active."""
    if MODEL_FREEZE_MARKER.exists():
        freeze = json.loads(MODEL_FREEZE_MARKER.read_text(encoding="utf-8"))
        raise SystemExit(
            "FleetCast model updates are frozen by data/validation/MODEL_FROZEN.json "
            f"(training data through {freeze.get('trainingDataThrough', 'the frozen cutoff')}). "
            "Use --download-only to collect source pages without changing index.html. "
            "Do not remove the freeze until the user explicitly requests model updates to resume."
        )


def daterange(start: dt.date, end: dt.date):
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


def request_bytes(url: str, attempts: int = 5) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def download_fish_day(day: dt.date) -> tuple[str, str]:
    FISH_CACHE.mkdir(parents=True, exist_ok=True)
    path = FISH_CACHE / f"{day.isoformat()}.html"
    if not path.exists() or path.stat().st_size < 500:
        selected = day.strftime("%m-%d-%Y")
        url = f"{BASE}?select={selected}&trip_type_id=1"
        path.write_bytes(request_bytes(url))
        time.sleep(0.08)
    return day.isoformat(), str(path)


def download_fish_pages(days: list[dt.date], workers: int) -> None:
    """Use curl's connection-aware parallel engine; cached pages make this resumable."""
    FISH_CACHE.mkdir(parents=True, exist_ok=True)
    pending = [day for day in days if not (FISH_CACHE / f"{day.isoformat()}.html").exists()]
    if not pending:
        print(f"fish pages: {len(days)}/{len(days)} (cached)", flush=True)
        return
    for offset in range(0, len(pending), 120):
        batch = pending[offset:offset + 120]
        command = [
            "curl", "-sS", "--fail", "--parallel", "--parallel-immediate",
            "--parallel-max", str(workers), "--retry", "4", "--retry-delay", "2",
            "--connect-timeout", "30", "--max-time", "90", "-A", USER_AGENT,
        ]
        for day in batch:
            selected = day.strftime("%m-%d-%Y")
            command.extend(["-o", str(FISH_CACHE / f"{day.isoformat()}.html"), f"{BASE}?select={selected}&trip_type_id=1"])
        subprocess.run(command, check=True)
        completed = min(offset + len(batch), len(pending))
        print(f"downloaded fish pages: {completed}/{len(pending)} new ({len(days) - len(pending)} cached)", flush=True)


def clean(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return re.sub(r"[ \t]+", " ", html.unescape(fragment)).strip()


def parse_species(fragment: str) -> list[dict]:
    text = clean(fragment).replace("\n", " ")
    # Commas serve two roles here: separating species entries ("18 Calico Bass,
    # 4 Sculpin") and thousands-grouping large counts ("1,200 Sculpin"). Only the
    # first kind is followed by whitespace, so protect digit-adjacent commas
    # before splitting, then restore them for the count regex below.
    protected = re.sub(r"(?<=\d),(?=\d)", "\0", text)
    merged: dict[str, list[int]] = {}
    for item in protected.split(","):
        item = item.replace("\0", ",").strip()
        released = bool(re.search(r"\sReleased\s*$", item, re.I))
        item = re.sub(r"\sReleased\s*$", "", item, flags=re.I)
        item = re.sub(r"\s*\([^)]*\)\s*", " ", item).strip()
        match = re.match(r"^([\d,]+)\s+(.+?)$", item)
        if not match:
            continue
        count = int(match.group(1).replace(",", ""))
        name = NAME_MAP.get(match.group(2).strip(), match.group(2).strip())
        values = merged.setdefault(name, [0, 0])
        values[1 if released else 0] += count
    return [
        {"species": name, "kept": values[0], "released": values[1]}
        for name, values in merged.items()
    ]


def page_report_date(raw: str) -> str | None:
    """Return the ISO date the fetched page actually reports on, or None.

    When the requested day has no report posted yet, SanDiegoFishReports
    silently serves the most recent available day's page instead of an
    empty one, with no indication in the visible content that it's stale.
    Callers must compare this against the requested day before trusting
    parse_fish_page's output, since parse_fish_page stamps every trip with
    whatever day it's told rather than anything derived from the page.
    """
    match = re.search(
        r'name="description"\s+content="[^"]*?-\s*([A-Za-z]+ \d{1,2}, \d{4})"', raw
    )
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def parse_fish_page(day: str, raw: str) -> list[dict]:
    report = raw.split("id='report-container'", 1)[-1]
    trips = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", report, flags=re.I | re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.I | re.S)
        if len(cells) != 3:
            continue
        details = clean(cells[1]).replace("\n", " ")
        period_match = re.search(r"1/2 Day\s+(AM|PM)\b", details, re.I)
        anglers_match = re.search(r"([\d,]+)\s+Anglers?", details, re.I)
        if not period_match or not anglers_match:
            continue
        links = re.findall(r"href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", cells[0], re.I | re.S)
        boat_link = next(((href, clean(label)) for href, label in links if "/charter_boats/" in href), None)
        landing_link = next(((href, clean(label)) for href, label in links if "/landings/" in href), None)
        if not boat_link or not boat_link[1] or not landing_link or not landing_link[1]:
            continue
        first_text = clean(cells[0]).splitlines()
        city = first_text[-1].strip() if first_text else ""
        species = parse_species(cells[2])
        kept = sum(item["kept"] for item in species)
        released = sum(item["released"] for item in species)
        anglers = int(anglers_match.group(1).replace(",", ""))
        encounters = kept + released
        trips.append({
            "date": day,
            "period": period_match.group(1).upper(),
            "anglers": anglers,
            "source_url": f"{BASE}?date={day}",
            "boat": boat_link[1],
            "boat_path": boat_link[0],
            "landing": landing_link[1],
            "landing_path": landing_link[0],
            "city": city,
            "species": species,
            "kept": kept,
            "released": released,
            "encounters": encounters,
            "epa": round(encounters / anglers, 3) if anglers else None,
        })
    return trips


def extract_db(source: str) -> tuple[dict, int, int]:
    start = source.index("const DB=") + len("const DB=")
    end = source.index(";\n", start)
    return json.loads(source[start:end]), start, end


def fingerprint(trip: dict) -> tuple:
    counts = tuple(sorted((s["species"], s["kept"], s["released"]) for s in trip["species"]))
    return trip["date"], trip["boat"], trip["landing"], trip["period"], trip["anglers"], counts


def assign_trip_numbers(trips: list[dict]) -> None:
    counters: dict[tuple, int] = defaultdict(int)
    for trip in sorted(trips, key=lambda t: (t["date"], t["boat"], t["period"])):
        key = trip["date"], trip["boat"], trip["period"]
        counters[key] += 1
        trip["tripNo"] = counters[key]


def download_noaa(station: str, year: int) -> Path | None:
    NOAA_CACHE.mkdir(parents=True, exist_ok=True)
    path = NOAA_CACHE / f"{station}h{year}.txt.gz"
    if path.exists() and path.stat().st_size > 100:
        return path
    url = f"https://www.ndbc.noaa.gov/data/historical/stdmet/{station}h{year}.txt.gz"
    try:
        path.write_bytes(request_bytes(url))
        return path
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def load_noaa_station(station: str, years: range) -> list[tuple[dt.datetime, dict]]:
    rows: list[tuple[dt.datetime, dict]] = []
    for year in years:
        path = download_noaa(station, year)
        if not path:
            continue
        with gzip.open(path, "rt", encoding="ascii", errors="replace") as handle:
            header = None
            for line in handle:
                if line.startswith("#"):
                    candidate = line.lstrip("#").split()
                    if candidate and candidate[0] in {"YY", "YYYY"}:
                        header = candidate
                    continue
                if not header:
                    continue
                values = line.split()
                if len(values) < len(header):
                    continue
                record = dict(zip(header, values))
                try:
                    year_value = int(record.get("YYYY", record.get("YY", year)))
                    if year_value < 100:
                        year_value += 2000
                    minute = int(record.get("mm", 0))
                    timestamp = dt.datetime(year_value, int(record["MM"]), int(record["DD"]), int(record["hh"]), minute, tzinfo=dt.timezone.utc)
                except (KeyError, ValueError):
                    continue
                parsed = {}
                for key in ("ATMP", "WTMP", "PRES", "WVHT", "DPD", "APD", "MWD", "WSPD", "GST"):
                    try:
                        value = float(record[key])
                    except (KeyError, ValueError):
                        continue
                    if value >= 99 and key not in {"PRES", "MWD"} or value >= 9990:
                        continue
                    parsed[key] = value
                rows.append((timestamp, parsed))
    return rows


def cdip_dataset_url(cdip_id: str, live: bool) -> str:
    if live:
        return f"https://thredds.cdip.ucsd.edu/thredds/dodsC/cdip/realtime/{cdip_id}p1_rt.nc"
    return f"https://thredds.cdip.ucsd.edu/thredds/dodsC/cdip/archive/{cdip_id}p1/{cdip_id}p1_historic.nc"


def download_cdip_station(cdip_id: str, live: bool, count: int = 20000) -> Path | None:
    """Fetch the most recent `count` wave samples (~1.5yr at CDIP's ~30min cadence)
    for a CDIP buoy via its OPeNDAP ASCII interface.

    Unlike NDBC's historical/stdmet archive -- only published once a calendar
    year is finalized, which is why swellFt has been null since 2026-05-03 --
    CDIP keeps its per-station "historic" file updated through the year, with a
    rolling realtime file underneath that. This is the only reliable swell
    source for the in-progress year until NDBC's archive catches up.
    """
    CDIP_CACHE.mkdir(parents=True, exist_ok=True)
    tag = "rt" if live else "historic"
    path = CDIP_CACHE / f"{cdip_id}p1_{tag}_{dt.date.today().isoformat()}.txt"
    if path.exists() and path.stat().st_size > 100:
        return path
    base = cdip_dataset_url(cdip_id, live)
    try:
        dds = request_bytes(f"{base}.dds").decode("ascii", errors="replace")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    match = re.search(r"waveTime = (\d+)", dds)
    if not match:
        return None
    total = int(match.group(1))
    lo, hi = max(0, total - count), total - 1
    query = f"waveTime%5B{lo}:1:{hi}%5D,waveHs%5B{lo}:1:{hi}%5D"
    try:
        payload = request_bytes(f"{base}.ascii?{query}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    path.write_bytes(payload)
    return path


def parse_cdip_ascii(path: Path) -> list[tuple[dt.datetime, dict]]:
    text = path.read_text(encoding="ascii", errors="replace")
    columns = {}
    for name in ("waveTime", "waveHs"):
        match = re.search(rf"{name}\[\d+\]\s*\n([^\n]+)", text)
        if not match:
            return []
        columns[name] = [item.strip() for item in match.group(1).split(",")]
    rows = []
    for epoch_text, hs_text in zip(columns["waveTime"], columns["waveHs"]):
        try:
            epoch, hs_meters = int(epoch_text), float(hs_text)
        except ValueError:
            continue
        if not (0 < hs_meters < 15):  # CDIP flags bad/missing samples with sentinel values
            continue
        rows.append((dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc), {"WVHT": hs_meters}))
    return rows


def load_cdip_station(cdip_id: str) -> list[tuple[dt.datetime, dict]]:
    rows: list[tuple[dt.datetime, dict]] = []
    for live in (False, True):
        path = download_cdip_station(cdip_id, live)
        if path:
            rows.extend(parse_cdip_ascii(path))
    rows.sort(key=lambda item: item[0])
    return rows


def download_tide_predictions(year: int) -> Path:
    """Download hourly NOAA CO-OPS predictions for the San Diego tide gauge."""
    NOAA_CACHE.mkdir(parents=True, exist_ok=True)
    path = NOAA_CACHE / f"coops_{TIDE_STATION}_predictions_{year}.json"
    if path.exists() and path.stat().st_size > 1000:
        return path
    params = urllib.parse.urlencode({
        "begin_date": f"{year}0101",
        "end_date": f"{year}1231",
        "station": TIDE_STATION,
        "product": "predictions",
        "datum": "MLLW",
        "time_zone": "lst_ldt",
        "interval": "h",
        "units": "english",
        "application": "FleetCast",
        "format": "json",
    })
    payload = request_bytes(f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?{params}")
    parsed = json.loads(payload)
    if "predictions" not in parsed:
        raise RuntimeError(f"NOAA tide response for {year} did not include predictions: {parsed}")
    path.write_bytes(payload)
    return path


def enrich_tides(rows: list[dict]) -> list[dict]:
    """Attach midpoint height, signed change, and total tide swing to each AM/PM row."""
    years = sorted({int(row["date"][:4]) for row in rows})
    by_date: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for year in years:
        payload = json.loads(download_tide_predictions(year).read_text(encoding="utf-8"))
        for prediction in payload.get("predictions", []):
            try:
                date_text, time_text = prediction["t"].split()
                hour = int(time_text.split(":", 1)[0])
                value = float(prediction["v"])
            except (KeyError, ValueError):
                continue
            by_date[date_text].append((hour, value))
    enriched = []
    for original in rows:
        row = dict(original)
        start_hour, end_hour, midpoint = (6, 12, 9) if row["period"] == "AM" else (12, 18, 15)
        window = sorted((hour, value) for hour, value in by_date.get(row["date"], []) if start_hour <= hour <= end_hour)
        if window:
            midpoint_value = min(window, key=lambda item: abs(item[0] - midpoint))[1]
            values = [value for _, value in window]
            row["tideHeightFt"] = round(midpoint_value, 3)
            row["tideSwingFt"] = round(max(values) - min(values), 3)
            row["tideDeltaFt"] = round(window[-1][1] - window[0][1], 3)
        enriched.append(row)
    return enriched


def nearest(observations: list[tuple[dt.datetime, dict]], timestamps: list[dt.datetime], target: dt.datetime, max_hours: float = 6) -> dict:
    if not observations:
        return {}
    position = bisect.bisect_left(timestamps, target)
    candidates = observations[max(0, position - 1):min(len(observations), position + 1)]
    best_time, best = min(candidates, key=lambda item: abs((item[0] - target).total_seconds()))
    return best if abs((best_time - target).total_seconds()) <= max_hours * 3600 else {}


def build_analysis_rows(trips: list[dict], old_rows: list[dict], start_year: int, end_year: int) -> list[dict]:
    years = range(start_year, end_year + 1)
    ljac = load_noaa_station("ljac1", years)
    waves = load_noaa_station("46232", years)  # Point Loma South -- closest buoy to the sportfishing departure grounds
    outer_waves = load_noaa_station("46225", years)  # Torrey Pines Outer -- fallback
    cdip_waves = load_cdip_station("191") if end_year >= dt.date.today().year else []  # CDIP-native Point Loma South
    cdip_outer_waves = load_cdip_station("100") if end_year >= dt.date.today().year else []  # CDIP-native Torrey Pines Outer
    ljac_times = [item[0] for item in ljac]
    wave_times = [item[0] for item in waves]
    outer_wave_times = [item[0] for item in outer_waves]
    cdip_wave_times = [item[0] for item in cdip_waves]
    cdip_outer_wave_times = [item[0] for item in cdip_outer_waves]
    old = {(row["date"], row["period"]): row for row in old_rows}
    fish: dict[tuple, list[int]] = defaultdict(lambda: [0] * len(ANALYSIS_SPECIES))
    for trip in trips:
        key = trip["date"], trip["period"]
        by_name = {item["species"]: item["kept"] + item["released"] for item in trip["species"]}
        for index, name in enumerate(ANALYSIS_SPECIES):
            fish[key][index] += by_name.get(name, 0)
    pacific = ZoneInfo("America/Los_Angeles")
    result = []
    for key in sorted(fish):
        date_text, period = key
        local_hour = 9 if period == "AM" else 15
        local = dt.datetime.combine(dt.date.fromisoformat(date_text), dt.time(local_hour), tzinfo=pacific)
        target = local.astimezone(dt.timezone.utc)
        met = nearest(ljac, ljac_times, target)
        wave = nearest(waves, wave_times, target)
        outer_wave = nearest(outer_waves, outer_wave_times, target)
        cdip_wave = nearest(cdip_waves, cdip_wave_times, target)
        cdip_outer_wave = nearest(cdip_outer_waves, cdip_outer_wave_times, target)
        row = dict(old.get(key, {"date": date_text, "period": period}))
        row["fish"] = fish[key]
        if "ATMP" in met and "airTempF" not in row:
            row["airTempF"] = round(met["ATMP"] * 9 / 5 + 32, 3)
        water = met.get("WTMP", outer_wave.get("WTMP"))
        if water is not None and "waterTempF" not in row:
            row["waterTempF"] = round(water * 9 / 5 + 32, 3)
        if "PRES" in met and "pressureHpa" not in row:
            row["pressureHpa"] = round(met["PRES"], 3)
        wave_height = wave.get("WVHT", outer_wave.get("WVHT", cdip_wave.get("WVHT", cdip_outer_wave.get("WVHT"))))
        if wave_height is not None and "swellFt" not in row:
            row["swellFt"] = round(wave_height * 3.28084, 3)
        result.append(row)
    return result


def rebuild_profiles(trips: list[dict]) -> dict:
    profiles = {}
    boats = sorted({trip["boat"] for trip in trips})
    for boat in boats:
        boat_trips = sorted((t for t in trips if t["boat"] == boat), key=lambda t: (t["date"], t["period"]))
        valid = [t["epa"] for t in boat_trips if t["epa"] is not None]
        periods = {}
        for period in ("AM", "PM"):
            values = [t["epa"] for t in boat_trips if t["period"] == period and t["epa"] is not None]
            periods[period] = {
                "n": len(values),
                "recent12": round(statistics.fmean(values[-12:]), 3) if values else None,
                "distribution": sorted(values),
            }
        profiles[boat] = {
            "tripCount": len(boat_trips),
            "landing": boat_trips[-1]["landing"],
            "periods": periods,
            "recent12": round(statistics.fmean(valid[-12:]), 3) if valid else 0,
        }
    return profiles


def rebuild_stats(trips: list[dict]) -> dict:
    years = defaultdict(int)
    for trip in trips:
        years[trip["date"][:4]] += 1
    return {
        "trips": len(trips),
        "dates": len({trip["date"] for trip in trips}),
        "boats": len({trip["boat"] for trip in trips}),
        "landings": len({trip["landing"] for trip in trips}),
        "years": dict(sorted(years.items())),
        "anglers": sum(trip["anglers"] for trip in trips),
        "encounters": sum(trip["encounters"] for trip in trips),
        "released": sum(trip["released"] for trip in trips),
    }


def apply_source_exclusions(source: str) -> dict:
    """Remove excluded landing records without re-downloading the archive."""
    db, json_start, json_end = extract_db(source)
    trips = [trip for trip in db["trips"] if trip.get("landing") not in EXCLUDED_LANDINGS]
    trips.sort(key=lambda trip: (trip["date"], trip["boat"], trip["period"]))
    assign_trip_numbers(trips)
    fish: dict[tuple, list[int]] = defaultdict(lambda: [0] * len(ANALYSIS_SPECIES))
    for trip in trips:
        key = trip["date"], trip["period"]
        by_name = {item["species"]: item["kept"] + item["released"] for item in trip["species"]}
        for index, name in enumerate(ANALYSIS_SPECIES):
            fish[key][index] += by_name.get(name, 0)
    analysis_rows = []
    for old_row in db["analysisRows"]:
        key = old_row["date"], old_row["period"]
        if key in fish:
            row = dict(old_row)
            row["fish"] = fish[key]
            analysis_rows.append(row)
    db["trips"] = trips
    db["stats"] = rebuild_stats(trips)
    db["boatProfiles"] = rebuild_profiles(trips)
    db["analysisSpecies"] = ANALYSIS_SPECIES
    db["analysisRows"] = analysis_rows
    replacement = json.dumps(db, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    INDEX.write_text(source[:json_start] + replacement + source[json_end:], encoding="utf-8")
    return db


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2017-01-01")
    parser.add_argument("--end", default="2023-12-31")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--apply-exclusions-only", action="store_true")
    parser.add_argument("--enrich-tides-only", action="store_true")
    parser.add_argument("--refresh-weather-only", action="store_true")
    args = parser.parse_args()
    if not args.download_only:
        require_model_updates_unfrozen()
    if args.apply_exclusions_only:
        db = apply_source_exclusions(INDEX.read_text(encoding="utf-8"))
        print(json.dumps(db["stats"], indent=2), flush=True)
        return
    if args.enrich_tides_only:
        source = INDEX.read_text(encoding="utf-8")
        db, json_start, json_end = extract_db(source)
        db["analysisRows"] = enrich_tides(db["analysisRows"])
        replacement = json.dumps(db, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        INDEX.write_text(source[:json_start] + replacement + source[json_end:], encoding="utf-8")
        matched = sum("tideSwingFt" in row for row in db["analysisRows"])
        print(f"tide-enriched analysis rows: {matched}/{len(db['analysisRows'])}", flush=True)
        return
    if args.refresh_weather_only:
        source = INDEX.read_text(encoding="utf-8")
        db, json_start, json_end = extract_db(source)
        db["analysisRows"] = build_analysis_rows(db["trips"], db["analysisRows"], dt.date.fromisoformat(args.start).year, dt.date.fromisoformat(args.end).year)
        replacement = json.dumps(db, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        INDEX.write_text(source[:json_start] + replacement + source[json_end:], encoding="utf-8")
        coverage = {key: sum(key in row for row in db["analysisRows"]) for key in ("waterTempF", "airTempF", "pressureHpa", "swellFt")}
        print(json.dumps(coverage, indent=2), flush=True)
        return
    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    days = list(daterange(start, end))
    download_fish_pages(days, args.workers)
    if args.download_only:
        return
    source = INDEX.read_text(encoding="utf-8")
    db, json_start, json_end = extract_db(source)
    historical = []
    for day in days:
        path = FISH_CACHE / f"{day.isoformat()}.html"
        historical.extend(parse_fish_page(day.isoformat(), path.read_text(encoding="utf-8", errors="replace")))
    merged = {}
    for trip in historical + db["trips"]:
        if not trip.get("boat") or not trip.get("landing"):
            continue
        if trip["landing"] in EXCLUDED_LANDINGS:
            continue
        merged[fingerprint(trip)] = trip
    trips = sorted(merged.values(), key=lambda t: (t["date"], t["boat"], t["period"]))
    assign_trip_numbers(trips)
    db["trips"] = trips
    db["stats"] = rebuild_stats(trips)
    db["boatProfiles"] = rebuild_profiles(trips)
    db["analysisSpecies"] = ANALYSIS_SPECIES
    db["analysisRows"] = build_analysis_rows(trips, db["analysisRows"], start.year, end.year)
    replacement = json.dumps(db, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    INDEX.write_text(source[:json_start] + replacement + source[json_end:], encoding="utf-8")
    print(json.dumps(db["stats"], indent=2), flush=True)


if __name__ == "__main__":
    main()
