#!/usr/bin/env python3
"""Twice-daily summary of today's reported catch, by species and by boat.

Independent of the historical archive in index.html: this is same-day,
ephemeral data (not training data for the forecast model), so it writes its
own small file, data/fleetcast/latest_catch.json, the same way the boat-track
bundle does, rather than touching the shared DB blob or its freeze concerns.

Run by .github/workflows/refresh-latest-catch.yml at ~8:00 PM and ~11:00 PM
Pacific -- landings post AM totals earlier in the day and PM totals after
dark, so a single run would routinely catch the day half-reported.

Reuses extend_history.py's own fetch/parse/stale-page-detection rather than
duplicating it: SanDiegoFishReports silently serves the most recent posted
day when today's isn't up yet (see page_report_date's docstring there), and
that guard already exists and is already trusted by the historical importer.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extend_history import (  # noqa: E402
    BASE, EXCLUDED_LANDINGS, page_report_date, parse_fish_page, request_bytes,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "fleetcast" / "latest_catch.json"
TZ = ZoneInfo("America/Los_Angeles")


def fetch_today(today):
    selected = today.strftime("%m-%d-%Y")
    url = f"{BASE}?select={selected}&trip_type_id=1"
    return request_bytes(url).decode("utf-8", errors="replace")


def summarize(trips):
    species: dict[str, dict] = {}
    boats: dict[str, dict] = {}
    totals = {"trips": 0, "anglers": 0, "kept": 0, "released": 0, "encounters": 0}
    for trip in trips:
        totals["trips"] += 1
        totals["anglers"] += trip["anglers"]
        totals["kept"] += trip["kept"]
        totals["released"] += trip["released"]
        totals["encounters"] += trip["encounters"]
        for item in trip["species"]:
            row = species.setdefault(item["species"], {"species": item["species"], "kept": 0, "released": 0})
            row["kept"] += item["kept"]
            row["released"] += item["released"]
        boat = boats.setdefault(trip["boat"], {
            "boat": trip["boat"], "landing": trip["landing"], "periods": set(),
            "anglers": 0, "kept": 0, "released": 0, "encounters": 0, "species": {},
        })
        boat["periods"].add(trip["period"])
        boat["anglers"] += trip["anglers"]
        boat["kept"] += trip["kept"]
        boat["released"] += trip["released"]
        boat["encounters"] += trip["encounters"]
        for item in trip["species"]:
            count = item["kept"] + item["released"]
            boat["species"][item["species"]] = boat["species"].get(item["species"], 0) + count

    for row in species.values():
        row["encounters"] = row["kept"] + row["released"]
    species_list = sorted(species.values(), key=lambda r: -r["encounters"])

    boat_list = []
    hot = None  # the single best boat+species pairing of the day, for one headline callout
    for boat in boats.values():
        top_species = max(boat["species"].items(), key=lambda kv: kv[1])[0] if boat["species"] else None
        boat_list.append({
            "boat": boat["boat"], "landing": boat["landing"],
            "period": "Both" if len(boat["periods"]) > 1 else next(iter(boat["periods"]), "—"),
            "anglers": boat["anglers"], "kept": boat["kept"], "released": boat["released"],
            "encounters": boat["encounters"], "topSpecies": top_species,
        })
        for sp, count in boat["species"].items():
            if hot is None or count > hot["count"]:
                hot = {"boat": boat["boat"], "species": sp, "count": count}
    boat_list.sort(key=lambda r: -r["encounters"])

    return totals, species_list, boat_list, hot


def main():
    now = datetime.now(TZ)
    today = now.date()
    raw = fetch_today(today)
    actual_date = page_report_date(raw)
    stale = actual_date is not None and actual_date != today.isoformat()
    trips = [] if stale else parse_fish_page(today.isoformat(), raw)
    trips = [t for t in trips if t["landing"] not in EXCLUDED_LANDINGS]
    totals, species_list, boat_list, hot = summarize(trips)

    payload = {
        "generated": now.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": today.isoformat(),
        "stale": stale,  # true: nothing posted for today yet, source fell back to an older day
        "totals": totals,
        "bySpecies": species_list,
        "byBoat": boat_list,
        "hotBite": hot,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"latest catch {today.isoformat()}: {totals['trips']} trips, {totals['encounters']} fish, stale={stale}", flush=True)


if __name__ == "__main__":
    main()
