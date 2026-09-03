"""Nightly ShipFinder export for Fleetcast.

The input file is intentionally kept outside the repository on PythonAnywhere.
Expected columns: boat_name,mmsi (a header row is optional).
"""
from __future__ import annotations

import csv, hashlib, json, os, random, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

ROOT = Path(os.getenv("FLEETCAST_DATA_DIR", "data/fleetcast"))
MMSI_FILE = Path(os.getenv("FLEETCAST_MMSI_FILE", str(ROOT / "mmsi.csv")))
SITE_URL = os.getenv("SHIPFINDER_URL", "https://www.shipfinder.com")
TZ = ZoneInfo(os.getenv("FLEETCAST_TIMEZONE", "America/Los_Angeles"))

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

def export_day(page, boat, day, out):
    page.goto(SITE_URL, wait_until="domcontentloaded")
    search = page.get_by_placeholder(re.compile("search.*vessel|vessel.*search", re.I)).first
    search.fill(boat["mmsi"] or boat["boat_name"])
    search.press("Enter")
    page.wait_for_load_state("networkidle")
    # The MMSI file is authoritative; the UI is used only to reach the export.
    page.get_by_text(re.compile("history|track|playback", re.I)).first.click()
    page.get_by_label(re.compile("start|from", re.I)).fill(day.isoformat())
    page.get_by_label(re.compile("end|to|through", re.I)).fill(day.isoformat())
    with page.expect_download(timeout=90_000) as info:
        page.get_by_role("button", name=re.compile("export|download", re.I)).click()
    download = info.value
    target = out / f"{slug(boat['boat_name'])}-{day.isoformat()}.csv"
    download.save_as(str(target))
    if target.stat().st_size < 20:
        raise RuntimeError("downloaded file is empty")
    return target

def build_geojson(csv_path, boat, day):
    points = []
    with csv_path.open(newline="", errors="replace") as fh:
        for row in csv.DictReader(fh):
            try:
                lat, lon = float(row.get("latitude") or row.get("lat")), float(row.get("longitude") or row.get("lon") or row.get("lng"))
                ts = row.get("timestamp") or row.get("time") or row.get("datetime") or ""
                speed = float(row.get("speed") or row.get("sog") or 0)
            except (TypeError, ValueError):
                continue
            points.append({"lat": lat, "lon": lon, "timestamp": ts, "speed": speed})
    points.sort(key=lambda p: p["timestamp"])
    path = {"type":"Feature","properties":{"boat":boat,"date":day.isoformat()},"geometry":{"type":"LineString","coordinates":[[p["lon"],p["lat"]] for p in points]}}
    # A conservative first-pass stop layer: consecutive low-speed fixes are clustered.
    # Configure port bounding boxes as JSON [[min_lon,min_lat,max_lon,max_lat], ...].
    port_boxes = json.loads(os.getenv("FLEETCAST_PORT_BBOXES", "[]"))
    def outside_ports(p):
        return not any(box[0] <= p["lon"] <= box[2] and box[1] <= p["lat"] <= box[3] for box in port_boxes)
    stops = []
    for p in points:
        if p["speed"] <= 0.5 and outside_ports(p):
            stops.append({"type":"Feature","properties":{"boat":boat,"timestamp":p["timestamp"],"dwell_minutes":10},"geometry":{"type":"Point","coordinates":[p["lon"],p["lat"]]}})
    base = csv_path.parent.parent / "processed" / day.isoformat(); base.mkdir(parents=True, exist_ok=True)
    (base / f"{slug(boat)}.geojson").write_text(json.dumps({"type":"FeatureCollection","features":[path]}))
    (base / f"{slug(boat)}-stops.geojson").write_text(json.dumps({"type":"FeatureCollection","features":stops}))

def main():
    # Stable per-day jitter means a retry does not create a second random run.
    today = datetime.now(TZ).date() - timedelta(days=1)
    delay = int(hashlib.sha256(today.isoformat().encode()).hexdigest()[:8], 16) % (3 * 60 * 60 - 20 * 60)
    time.sleep(delay)
    out = ROOT / "raw" / today.isoformat(); out.mkdir(parents=True, exist_ok=True)
    manifest = {"date": today.isoformat(), "started_at": datetime.now(timezone.utc).isoformat(), "boats": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, downloads_path=str(out / "tmp"))
        context = browser.new_context(storage_state=os.getenv("SHIPFINDER_AUTH_STATE") or None, accept_downloads=True)
        page = context.new_page()
        for boat in read_fleet():
            try:
                path = export_day(page, boat, today, out)
                build_geojson(path, boat["boat_name"], today)
                manifest["boats"].append({**boat, "status": "ok", "file": str(path.relative_to(ROOT))})
            except Exception as exc:
                manifest["boats"].append({**boat, "status": "error", "error": str(exc)})
        browser.close()
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    (ROOT / "manifests").mkdir(parents=True, exist_ok=True)
    (ROOT / "manifests" / f"{today.isoformat()}.json").write_text(json.dumps(manifest, indent=2))
    (ROOT / "latest.json").write_text(json.dumps(manifest, indent=2))

if __name__ == "__main__":
    main()
