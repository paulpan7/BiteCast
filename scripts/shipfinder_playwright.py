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

LOGIN (confirmed against the real site, 2026-09-03): passive vessel search
and the 24h/3-day track view work while logged out, but "Track playback" --
what the CSV export lives under -- requires an account, and the site's own
JS checks a *second*, separate `TrackReplay` permission after login, so a
plain free account may still be refused (contact shipfinder@elaneglobal.com
if so). Set SHIPFINDER_EMAIL and SHIPFINDER_PASSWORD (read fresh from the
environment at run time, never written to disk or logged) and this script
logs in automatically via login(); set SHIPFINDER_AUTH_STATE to a path
OUTSIDE this git repo (it becomes a saved session file, equivalent to a
login token -- never commit it) so a fresh login only happens once instead
of every night.
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
GIT_REPO_ROOT = os.getenv("FLEETCAST_GIT_REPO")  # path to the BiteCast git checkout on this machine; unset = don't push

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

LOGIN_URL = f"{SITE_URL}/home/login"

def is_logged_in(page):
    return bool(page.evaluate("() => !!(window.config && window.config.auth && window.config.auth.IsLogin)"))

def has_track_replay_permission(page):
    return bool(page.evaluate("() => !!(window.config && window.config.auth && window.config.auth.TrackReplay)"))

def login(page):
    """Log into ShipFinder with SHIPFINDER_EMAIL/SHIPFINDER_PASSWORD.

    Credentials are read fresh from the environment at call time and never
    written to disk, logged, or included in any exception message. Real
    login form, confirmed 2026-09-03: https://www.shipfinder.com/home/login,
    fields #userName / #userPWD, "Keep me signed in" checkbox #autologin
    (checked by default), submit button #submitBtn. No CAPTCHA was present
    on the static form during that check, but one could still appear under
    bot-detection heuristics this script can't anticipate.
    """
    email = os.getenv("SHIPFINDER_EMAIL")
    password = os.getenv("SHIPFINDER_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "Not logged into ShipFinder and no usable saved session. Set "
            "SHIPFINDER_EMAIL and SHIPFINDER_PASSWORD, and ideally "
            "SHIPFINDER_AUTH_STATE too (a path OUTSIDE this git repo) so the "
            "session persists across runs instead of logging in every night."
        )
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.locator("#userName").fill(email)
    page.locator("#userPWD").fill(password)
    if not page.locator("#autologin").is_checked():
        page.locator("#autologin").check()
    page.locator("#submitBtn").click()
    page.wait_for_load_state("networkidle")
    if not is_logged_in(page):
        raise RuntimeError("ShipFinder login did not succeed (wrong credentials, or the site changed its login form).")
    if not has_track_replay_permission(page):
        print("WARNING: logged in, but this account has no TrackReplay permission -- "
              "track playback/export will likely fail (contact shipfinder@elaneglobal.com "
              "or check the account's plan).")

def ensure_logged_in(context, page):
    """Reuse a saved session (loaded into `context` via storage_state before
    this is called) if it's still valid; otherwise log in fresh and, if
    SHIPFINDER_AUTH_STATE is set, save the new session for next time."""
    page.goto(SITE_URL, wait_until="domcontentloaded")
    if is_logged_in(page):
        return
    login(page)
    auth_state_path = os.getenv("SHIPFINDER_AUTH_STATE")
    if auth_state_path:
        context.storage_state(path=auth_state_path)
    else:
        print("WARNING: SHIPFINDER_AUTH_STATE is not set, so this session can't be "
              "saved -- every run will log in again. Set it to a path OUTSIDE this "
              "git repo (never commit a session file: it's equivalent to a login token).")

def export_window(page, boat, start_date, end_date, out):
    """Confirmed against the real site 2026-09-03, up through the login gate
    on "Track playback" (see login() above) -- ensure_logged_in() must run
    first. The exact click path from there to the #dateSelect/#dateSelectEnd
    date-range panel was traced via the page's own JS (shipInfoLayer /
    trackreplay), not fully driven end-to-end with a real account, so treat
    the first scheduled run as a supervised (non-headless) smoke test.
    """
    search = page.get_by_placeholder("Search ship, port...")
    search.click()
    search.fill(boat["mmsi"] or boat["boat_name"])
    page.wait_for_timeout(600)  # results are debounced, not immediate
    result = page.locator(f'a.ship_ico[mmsi="{boat["mmsi"]}"]') if boat["mmsi"] else page.locator(".ship_list a.ship_ico")
    result.first.click()
    page.get_by_text("Track playback", exact=True).first.click()
    page.locator("#dateSelect").fill(f"{start_date.isoformat()} 00:00")
    page.locator("#dateSelectEnd").fill(f"{end_date.isoformat()} 23:59")
    page.get_by_text("Search", exact=True).click()
    page.wait_for_load_state("networkidle")
    with page.expect_download(timeout=90_000) as info:
        page.get_by_text("Export", exact=True).click()
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

def git_commit_and_push(repo_root, paths, message):
    """Commit and push the given paths, so GitHub Pages picks up the fresh
    bundle the same way the validation-sync GitHub Action already commits
    its own data back to this repo.

    Assumes git on this machine is already configured with push credentials
    (an SSH deploy key, or an HTTPS credential helper) -- this just runs the
    plumbing a human would from an authenticated shell; it doesn't manage or
    embed any credential itself. A no-op, not an error, if nothing changed.
    """
    import subprocess
    repo_root = str(repo_root)
    subprocess.run(["git", "-C", repo_root, "add", *[str(p) for p in paths]], check=True)
    unchanged = subprocess.run(["git", "-C", repo_root, "diff", "--cached", "--quiet"]).returncode == 0
    if unchanged:
        print("git: no changes to commit")
        return
    subprocess.run(["git", "-C", repo_root, "commit", "-m", message], check=True)
    subprocess.run(["git", "-C", repo_root, "push"], check=True)

def main():
    from playwright.sync_api import sync_playwright  # heavy optional dep; only needed here

    # Stable per-run jitter means a retry does not create a second random delay.
    # Skipped for a manual/on-demand run (FLEETCAST_SKIP_JITTER) -- a human
    # watching a test run shouldn't wait up to 30 minutes for nothing to happen.
    # Paired with the workflow's 10:00 PM cron, this lands the scrape
    # somewhere in the 10:00-10:30 PM Pacific window each night.
    if not os.getenv("FLEETCAST_SKIP_JITTER"):
        now = datetime.now(TZ)
        delay = int(hashlib.sha256(now.date().isoformat().encode()).hexdigest()[:8], 16) % (30 * 60)
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
        auth_state_path = os.getenv("SHIPFINDER_AUTH_STATE")
        saved_session = auth_state_path if auth_state_path and Path(auth_state_path).exists() else None
        context = browser.new_context(storage_state=saved_session, accept_downloads=True)
        page = context.new_page()
        ensure_logged_in(context, page)
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

    # bundle.json is already sitting on this machine's disk at this point --
    # a PythonAnywhere web app can be pointed at FLEETCAST_DATA_DIR to serve
    # it directly with no further steps. Pushing it into the BiteCast repo
    # too (opt-in via FLEETCAST_GIT_REPO) is what makes GitHub Pages serve
    # the same file, matching how the validation-sync GitHub Action already
    # commits its own data back to this repo.
    if GIT_REPO_ROOT:
        git_commit_and_push(GIT_REPO_ROOT, [BUNDLE_PATH], f"Refresh boat tracks bundle ({window_end.date().isoformat()})")

if __name__ == "__main__":
    main()
