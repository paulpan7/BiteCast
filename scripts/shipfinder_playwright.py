"""Nightly ShipFinder export for Fleetcast.

Runs at ~11:00 PM Pacific and captures each boat's trailing 24 hours of AIS
reports (per FLEETCAST_MMSI_FILE), merging them into one rolling bundle at
data/fleetcast/bundle.json shaped as:
    {"generated": "<UTC ISO>", "tracks": {"<mmsi>": {"name", "mmsi", "points": [...]}}}
where each point is {"lat","lon","speed","course","heading","status","utc","local"}
("local" is the Pacific-zoned ISO timestamp, DST-aware via zoneinfo).

The Boat Tracks tab fetches that single file and does calendar-day
filtering client-side, picking one day (in Pacific time) at a time from
whatever history is retained (FLEETCAST_RETENTION_DAYS, default 30 days).

CSV FORMAT (confirmed against a real export, 2026-09-02): the file is
GB18030-encoded (ShipFinder is a Chinese AIS aggregator; parenthesized
header segments use full-width Chinese parens, and "CST" in the CST column
means China Standard Time, UTC+8, not Central), with these columns:
    Longitude(deg-minute), Latitude(deg-minute), Longitude, Latitude,
    Ship speed(kn), Ship course, Ship heading,
    Ship Turning Rate（deg/second）, Navigation status,
    Last update（CST）, Last update（UTC）
parse_points() reads the plain decimal Longitude/Latitude columns (not the
deg-minute ones) and parses "Last update（UTC）" directly as UTC -- that
column is explicitly labeled by the source, not inferred. A boat with no
data for the requested window comes back as a header-only file (1 line).
parse_timestamp()'s CSV_NAIVE_TZ assumption (below) is a fallback for any
other, unlabeled timestamp column and isn't exercised by the confirmed
format above.

The input file is intentionally kept outside the repository on PythonAnywhere.
Expected columns: boat_name,mmsi (a header row is optional).
"""
from __future__ import annotations

import csv, hashlib, json, os, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.getenv("FLEETCAST_DATA_DIR", "data/fleetcast"))
MMSI_FILE = Path(os.getenv("FLEETCAST_MMSI_FILE", str(ROOT / "mmsi.csv")))
SITE_URL = os.getenv("SHIPFINDER_URL", "https://www.shipfinder.com")
TZ = ZoneInfo(os.getenv("FLEETCAST_TIMEZONE", "America/Los_Angeles"))
CSV_NAIVE_TZ = ZoneInfo(os.getenv("FLEETCAST_CSV_NAIVE_TZ", "UTC"))  # see TIMEZONE CAVEAT above
BUNDLE_PATH = ROOT / "bundle.json"
WINDOW_HOURS = 24
RETENTION_DAYS = int(os.getenv("FLEETCAST_RETENTION_DAYS", "30"))

def read_fleet():
    raw = MMSI_FILE.read_text().splitlines()
    has_header = bool(raw and re.search(r"boat|mmsi|name", raw[0], re.I))
    if has_header:
        with MMSI_FILE.open(newline="") as fh: rows = list(csv.DictReader(fh))
    else:  # permit a simple one-column file with no header
        rows = [{"boat_name": line.split(",")[0].strip(), "mmsi": (line.split(",")[1].strip() if "," in line else "")} for line in raw if line.strip()]
    return [{"boat_name": r.get("boat_name") or r.get("boat") or r.get("name"), "mmsi": (r.get("mmsi") or "").strip()} for r in rows]

def slug(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")

def export_window(page, boat, start_date, end_date, out):
    page.goto(SITE_URL, wait_until="domcontentloaded")
    search = page.get_by_placeholder(re.compile("search.*vessel|vessel.*search", re.I)).first
    search.fill(boat["mmsi"] or boat["boat_name"])
    search.press("Enter")
    page.wait_for_load_state("networkidle")
    # The MMSI file is authoritative; the UI is used only to reach the export.
    page.get_by_text(re.compile("history|track|playback", re.I)).first.click()
    page.get_by_label(re.compile("start|from", re.I)).fill(start_date.isoformat())
    page.get_by_label(re.compile("end|to|through", re.I)).fill(end_date.isoformat())
    with page.expect_download(timeout=90_000) as info:
        page.get_by_role("button", name=re.compile("export|download", re.I)).click()
    download = info.value
    target = out / f"{slug(boat['boat_name'])}-{start_date.isoformat()}_{end_date.isoformat()}.csv"
    download.save_as(str(target))
    if target.stat().st_size < 20:
        raise RuntimeError("downloaded file is empty")
    return target

def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def parse_timestamp(raw, assume_tz=None):
    """Parse a timestamp whose UTC-ness isn't explicitly labeled by its column.

    A value with an explicit offset (%z, or a trailing Z) is trusted as-is.
    A bare value is assumed to be in `assume_tz` (CSV_NAIVE_TZ, UTC by
    default, if not given) -- a fallback for any column other than
    ShipFinder's own "Last update（UTC）", which parse_points() below passes
    assume_tz=timezone.utc for directly since that column is labeled UTC by
    the source rather than guessed.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    tz = CSV_NAIVE_TZ if assume_tz is None else assume_tz
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
        except ValueError:
            continue
    return None

def parse_points(csv_path):
    """Read a ShipFinder export CSV into the bundle's point shape.

    GB18030-encoded per the confirmed real export (see module docstring).
    Reads the plain decimal Longitude/Latitude columns and the UTC-labeled
    timestamp column directly; falls back to the previously-guessed generic
    column names in case a different export variant shows up. Timestamps
    are formatted without a fractional-second component so that plain
    string comparison (used for merge/retention filtering) stays a correct
    proxy for chronological order.
    """
    points = []
    with csv_path.open(encoding="gb18030", newline="", errors="replace") as fh:
        for row in csv.DictReader(fh):
            try:
                lat = float(row.get("Latitude") or row.get("latitude") or row.get("lat"))
                lon = float(row.get("Longitude") or row.get("longitude") or row.get("lon") or row.get("lng"))
            except (TypeError, ValueError):
                continue
            utc_label = row.get("Last update（UTC）")
            utc = parse_timestamp(utc_label, assume_tz=timezone.utc) if utc_label else \
                parse_timestamp(row.get("timestamp") or row.get("time") or row.get("datetime") or row.get("utc"))
            if utc is None:
                continue
            utc = utc.astimezone(timezone.utc).replace(microsecond=0)
            local = utc.astimezone(TZ)
            points.append({
                "lat": lat, "lon": lon,
                "speed": _to_float(row.get("Ship speed(kn)") or row.get("speed") or row.get("sog")),
                "course": _to_float(row.get("Ship course") or row.get("course") or row.get("cog")),
                "heading": _to_float(row.get("Ship heading") or row.get("heading")),
                "status": (row.get("Navigation status") or row.get("status") or row.get("nav_status") or "Unknown").strip() or "Unknown",
                "utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "local": local.isoformat(),
            })
    points.sort(key=lambda p: p["utc"])
    return points

def load_bundle():
    if BUNDLE_PATH.exists():
        try:
            return json.loads(BUNDLE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"generated": None, "tracks": {}}

def merge_points(existing, fresh, retention_cutoff):
    """Merge freshly downloaded points into a boat's retained history.

    Deduplicates by (utc, lat, lon) so a re-run within the same window (a
    retry, or overlap from the two-calendar-day export span) doesn't create
    duplicate reports, then drops anything older than the retention window.
    """
    seen = {(p["utc"], round(p["lat"], 5), round(p["lon"], 5)) for p in existing}
    merged = list(existing)
    for p in fresh:
        key = (p["utc"], round(p["lat"], 5), round(p["lon"], 5))
        if key in seen:
            continue
        seen.add(key)
        merged.append(p)
    merged = [p for p in merged if p["utc"] >= retention_cutoff]
    merged.sort(key=lambda p: p["utc"])
    return merged

def main():
    from playwright.sync_api import sync_playwright  # heavy optional dep; only needed here

    # Stable per-run jitter means a retry does not create a second random delay.
    now = datetime.now(TZ)
    delay = int(hashlib.sha256(now.date().isoformat().encode()).hexdigest()[:8], 16) % (45 * 60)
    time.sleep(delay)

    window_end = datetime.now(TZ)
    window_start = window_end - timedelta(hours=WINDOW_HOURS)
    window_start_utc = window_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    retention_cutoff = (window_end - timedelta(days=RETENTION_DAYS)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    out = ROOT / "raw" / window_end.date().isoformat(); out.mkdir(parents=True, exist_ok=True)
    bundle = load_bundle()
    manifest = {
        "date": window_end.date().isoformat(),
        "windowStart": window_start.isoformat(), "windowEnd": window_end.isoformat(),
        "started_at": datetime.now(timezone.utc).isoformat(), "boats": [],
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, downloads_path=str(out / "tmp"))
        context = browser.new_context(storage_state=os.getenv("SHIPFINDER_AUTH_STATE") or None, accept_downloads=True)
        page = context.new_page()
        for boat in read_fleet():
            try:
                # The window straddles midnight (an 11pm run covers "yesterday" and
                # "today"), so request both calendar days; window_start_utc trims
                # the export down to exactly the trailing 24 hours below.
                path = export_window(page, boat, window_start.date(), window_end.date(), out)
                points = parse_points(path)
                fresh = [p for p in points if p["utc"] >= window_start_utc]
                mmsi = boat["mmsi"]
                existing = bundle["tracks"].get(mmsi, {}).get("points", [])
                bundle["tracks"][mmsi] = {
                    "name": boat["boat_name"], "mmsi": mmsi,
                    "points": merge_points(existing, fresh, retention_cutoff),
                }
                manifest["boats"].append({**boat, "status": "ok", "file": str(path.relative_to(ROOT)), "newReports": len(fresh)})
            except Exception as exc:
                manifest["boats"].append({**boat, "status": "error", "error": str(exc)})
        browser.close()

    bundle["generated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ROOT.mkdir(parents=True, exist_ok=True)
    BUNDLE_PATH.write_text(json.dumps(bundle, separators=(",", ":")))
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    (ROOT / "manifests").mkdir(parents=True, exist_ok=True)
    (ROOT / "manifests" / f"{window_end.date().isoformat()}.json").write_text(json.dumps(manifest, indent=2))

if __name__ == "__main__":
    main()
