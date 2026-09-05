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
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extend_history import (  # noqa: E402
    BASE, EXCLUDED_LANDINGS, page_report_date, parse_fish_page, request_bytes,
)

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
# Manually-maintained editorial hint, not derived from the data -- update or
# remove this as the season actually changes. Set 2026-09-05: yellowtail is
# in season and a big draw for San Diego anglers right now, so it's worth
# naming even when it isn't the day's top species by raw count.
SEASONAL_NOTE = (
    "Yellowtail are in season right now and a big draw for San Diego anglers "
    "-- if yellowtail shows up anywhere in today's species list, call it out "
    "by name (with its count and, if notable, which boat), even if it isn't "
    "the single highest-count species. Don't force it in if it's genuinely "
    "absent from the list."
)

AI_SUMMARY_SYSTEM_PROMPT = (
    "You write short daily bite-report summaries for a San Diego sportfishing "
    "site, in the brisk, specific, slightly informal tone real landing dock-"
    "totals writeups use: lead with what stood out, name real boats and "
    "species with their actual counts, no hype or exclamation-point overload, "
    "no generic filler ('a great day was had by all'). Use only the date/"
    "weekday and numbers given -- never compute or guess a weekday yourself. "
    "2-4 sentences. Output only the report itself -- no preamble like 'Here "
    "is a summary', no markdown, no quotation marks around it.\n\n" + SEASONAL_NOTE
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
            "byPeriod": {"AM": 0, "PM": 0},
            "anglers": 0, "kept": 0, "released": 0, "encounters": 0, "species": {},
        })
        boat["periods"].add(trip["period"])
        boat["byPeriod"][trip["period"]] = boat["byPeriod"].get(trip["period"], 0) + trip["encounters"]
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
        top_five = sorted(boat["species"].items(), key=lambda kv: -kv[1])[:5]
        boat_list.append({
            "boat": boat["boat"], "landing": boat["landing"],
            "period": "Both" if len(boat["periods"]) > 1 else next(iter(boat["periods"]), "—"),
            "anglers": boat["anglers"], "kept": boat["kept"], "released": boat["released"],
            "encounters": boat["encounters"],
            "topSpecies": top_five[0][0] if top_five else None,
            "periods": boat["byPeriod"],  # {"AM": n, "PM": n} -- for a split bar
            "topSpeciesList": [{"species": sp, "count": count} for sp, count in top_five],
            "_species": boat["species"],  # full breakdown, used below for the matrix then dropped
        })
        for sp, count in boat["species"].items():
            if hot is None or count > hot["count"]:
                hot = {"boat": boat["boat"], "species": sp, "count": count}
    boat_list.sort(key=lambda r: -r["encounters"])

    # A small boat x species matrix for a heatmap: top boats by their own
    # encounters, top species by fleet-wide encounters, cross-referenced --
    # capped on both axes so the grid stays legible rather than exhaustive.
    matrix_boats = [b["boat"] for b in boat_list[:8]]
    matrix_species = [s["species"] for s in species_list[:8]]
    matrix_cells = [[next((b for b in boat_list if b["boat"] == boat_name), {}).get("_species", {}).get(sp, 0)
                      for sp in matrix_species] for boat_name in matrix_boats]
    matrix = {"boats": matrix_boats, "species": matrix_species, "cells": matrix_cells}
    for b in boat_list:
        del b["_species"]

    return totals, species_list, boat_list, hot, matrix


def build_summary_prompt(date, weekday, totals, species_list, boat_list, hot):
    lines = [
        # Giving the weekday explicitly, rather than leaving the model to work
        # it out from the ISO date, avoids exactly the failure mode observed
        # 2026-09-05: a fluent but wrong "Thursday" for an actual Friday.
        f"Date: {date} ({weekday})",
        f"Trips: {totals['trips']}  Anglers: {totals['anglers']}  "
        f"Fish: {totals['encounters']} (kept {totals['kept']}, released {totals['released']})",
    ]
    if species_list:
        lines.append("By species: " + ", ".join(f"{s['species']} {s['encounters']}" for s in species_list[:8]))
    if boat_list:
        lines.append("By boat: " + ", ".join(
            f"{b['boat']} ({b['landing']}) {b['encounters']} fish, mostly {b['topSpecies']}" for b in boat_list[:8]
        ))
    if hot:
        lines.append(f"Best single result: {hot['count']} {hot['species']} aboard {hot['boat']}.")
    return "\n".join(lines)


def generate_ai_summary(date, weekday, totals, species_list, boat_list, hot):
    """Real AI-written bite report via the Claude API. Returns None (never
    raises) if ANTHROPIC_API_KEY isn't set or the call fails for any reason
    -- the stats/infographics half of this page must ship regardless of
    whether the summary comes through."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 220,
        "system": AI_SUMMARY_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": build_summary_prompt(date, weekday, totals, species_list, boat_list, hot)}],
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
        text = "".join(block.get("text", "") for block in data.get("content", [])).strip()
        return text or None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        print(f"WARNING: AI summary generation failed, publishing without it: {exc}", flush=True)
        return None


def main():
    now = datetime.now(TZ)
    today = now.date()
    raw = fetch_today(today)
    actual_date = page_report_date(raw)
    # SanDiegoFishReports silently serves the most recently *posted* day when
    # today's own results aren't up yet, rather than an empty page. Rather
    # than discard that and show nothing until tonight's numbers land, treat
    # whatever day it actually returned as the one being summarized -- its
    # own correct date -- so the page keeps showing the last real day's
    # catch instead of going blank in between. carried_over tells the
    # frontend to say so plainly rather than imply this is today's own data.
    effective_date = actual_date or today.isoformat()
    carried_over = effective_date != today.isoformat()
    trips = parse_fish_page(effective_date, raw)
    trips = [t for t in trips if t["landing"] not in EXCLUDED_LANDINGS]
    totals, species_list, boat_list, hot, matrix = summarize(trips)
    weekday = datetime.strptime(effective_date, "%Y-%m-%d").strftime("%A")
    ai_summary = None if not totals["trips"] else generate_ai_summary(
        effective_date, weekday, totals, species_list, boat_list, hot,
    )

    payload = {
        "generated": now.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": effective_date,
        "requestedDate": today.isoformat(),
        "carriedOver": carried_over,  # true: today's own results aren't posted yet -- this is the last day that has real data
        "totals": totals,
        "bySpecies": species_list,
        "byBoat": boat_list,
        "hotBite": hot,
        "matrix": matrix,  # small boat x species grid for the heatmap
        "aiSummary": ai_summary,  # null if ANTHROPIC_API_KEY isn't set, the call failed, or there's nothing to summarize
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"latest catch {effective_date} (requested {today.isoformat()}): {totals['trips']} trips, "
          f"{totals['encounters']} fish, carriedOver={carried_over}, aiSummary={'yes' if ai_summary else 'no'}", flush=True)


if __name__ == "__main__":
    main()
