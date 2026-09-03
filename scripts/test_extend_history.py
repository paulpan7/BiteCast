"""Fixture-based tests for the SanDiegoFishReports scraper in extend_history.py.

Run with: python3 -m unittest scripts/test_extend_history.py -v
(or `python3 -m pytest scripts/test_extend_history.py -v` if pytest is installed)

These pin down the expected parse of a row shaped like the live site's markup,
so a future markup change shows up as a failing test instead of a silent drop
in parsed trips.
"""
from __future__ import annotations

import unittest

from extend_history import clean, fingerprint, parse_fish_page, parse_species


# A single day's report table, trimmed to the markup parse_fish_page actually
# reads: id='report-container', <tr> rows with exactly 3 <td> cells (boat/landing
# links + city, trip details, species counts).
SAMPLE_REPORT = """
<div id='report-container'>
<table>
<tr>
<td><a href="/charter_boats/american-angler">American Angler</a>
<a href="/landings/point-loma-sportfishing">Point Loma Sportfishing</a>
San Diego, CA</td>
<td>1/2 Day AM<br>22 Anglers</td>
<td>18 Calico Bass, 4 Calico Bass Released, 2 Barred Sand Bass, 1 Yellowfin Tuna (over 20 lbs)</td>
</tr>
<tr>
<td><a href="/charter_boats/pacific-voyager">Pacific Voyager</a>
<a href="/landings/seaforth-sportfishing">Seaforth Sportfishing</a>
San Diego, CA</td>
<td>1/2 Day PM<br>1,250 Anglers</td>
<td>3 Rockfish, 1,200 Sculpin</td>
</tr>
<tr>
<td>No boat link here, should be skipped</td>
<td>1/2 Day AM<br>5 Anglers</td>
<td>2 Bonito</td>
</tr>
<tr>
<td><a href="/charter_boats/no-period">No Period</a>
<a href="/landings/some-landing">Some Landing</a>
San Diego, CA</td>
<td>5 Anglers</td>
<td>2 Bonito</td>
</tr>
</table>
</div>
"""


class ParseSpeciesTests(unittest.TestCase):
    def test_kept_and_released_are_split(self):
        result = parse_species("18 Calico Bass, 4 Calico Bass Released")
        self.assertEqual(result, [{"species": "Calico Bass", "kept": 18, "released": 4}])

    def test_thousands_separator_in_count(self):
        result = parse_species("1,200 Sculpin")
        self.assertEqual(result, [{"species": "Sculpin", "kept": 1200, "released": 0}])

    def test_parenthetical_weight_note_is_stripped(self):
        result = parse_species("1 Yellowfin Tuna (over 20 lbs)")
        self.assertEqual(result, [{"species": "Yellowfin", "kept": 1, "released": 0}])

    def test_name_map_normalizes_common_aliases(self):
        result = parse_species("2 Barred Sand Bass, 1 California Yellowtail")
        by_species = {r["species"]: r for r in result}
        self.assertIn("Sand Bass", by_species)
        self.assertIn("Yellowtail", by_species)

    def test_duplicate_species_in_same_line_are_merged(self):
        result = parse_species("10 Rockfish, 5 Rockfish Released, 2 Rockfish")
        self.assertEqual(result, [{"species": "Rockfish", "kept": 12, "released": 5}])

    def test_garbage_fragment_yields_no_rows(self):
        self.assertEqual(parse_species("no counts here at all"), [])

    def test_empty_string(self):
        self.assertEqual(parse_species(""), [])


class CleanTests(unittest.TestCase):
    def test_strips_tags_and_unescapes_entities(self):
        self.assertEqual(clean("<b>18 Calico&nbsp;Bass</b>"), "18 Calico\xa0Bass")

    def test_br_becomes_newline(self):
        self.assertEqual(clean("1/2 Day AM<br>22 Anglers"), "1/2 Day AM\n22 Anglers")


class ParseFishPageTests(unittest.TestCase):
    def setUp(self):
        self.trips = parse_fish_page("2026-01-15", SAMPLE_REPORT)

    def test_only_well_formed_rows_are_kept(self):
        # 4 rows in the fixture: one missing a boat link, one missing "1/2 Day AM/PM"
        # -- both must be silently dropped rather than raising or corrupting output.
        self.assertEqual(len(self.trips), 2)

    def test_first_trip_fields(self):
        trip = self.trips[0]
        self.assertEqual(trip["boat"], "American Angler")
        self.assertEqual(trip["landing"], "Point Loma Sportfishing")
        self.assertEqual(trip["period"], "AM")
        self.assertEqual(trip["anglers"], 22)
        self.assertEqual(trip["date"], "2026-01-15")

    def test_species_counts_roll_up_into_kept_released_encounters(self):
        trip = self.trips[0]
        by_species = {s["species"]: s for s in trip["species"]}
        self.assertEqual(by_species["Calico Bass"], {"species": "Calico Bass", "kept": 18, "released": 4})
        self.assertEqual(by_species["Sand Bass"]["kept"], 2)
        self.assertEqual(trip["kept"], 21)
        self.assertEqual(trip["released"], 4)
        self.assertEqual(trip["encounters"], 25)

    def test_epa_is_encounters_per_angler(self):
        trip = self.trips[0]
        self.assertAlmostEqual(trip["epa"], round(25 / 22, 3))

    def test_thousands_separator_in_angler_count(self):
        trip = self.trips[1]
        self.assertEqual(trip["anglers"], 1250)
        self.assertEqual(trip["period"], "PM")

    def test_no_report_container_yields_no_trips(self):
        self.assertEqual(parse_fish_page("2026-01-15", "<html><body>nothing here</body></html>"), [])

    def test_zero_anglers_gives_null_epa_not_a_crash(self):
        raw = """
        <div id='report-container'><table>
        <tr><td><a href="/charter_boats/x">X</a><a href="/landings/y">Y</a>City</td>
        <td>1/2 Day AM<br>0 Anglers</td><td>1 Bonito</td></tr>
        </table></div>
        """
        trips = parse_fish_page("2026-01-15", raw)
        self.assertEqual(len(trips), 1)
        self.assertIsNone(trips[0]["epa"])


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_regardless_of_species_order(self):
        base = {
            "date": "2026-01-15", "boat": "American Angler", "landing": "Point Loma Sportfishing",
            "period": "AM", "anglers": 22,
        }
        trip_a = {**base, "species": [{"species": "Calico Bass", "kept": 18, "released": 4}, {"species": "Sand Bass", "kept": 2, "released": 0}]}
        trip_b = {**base, "species": [{"species": "Sand Bass", "kept": 2, "released": 0}, {"species": "Calico Bass", "kept": 18, "released": 4}]}
        self.assertEqual(fingerprint(trip_a), fingerprint(trip_b))

    def test_fingerprint_differs_on_different_counts(self):
        base = {
            "date": "2026-01-15", "boat": "American Angler", "landing": "Point Loma Sportfishing",
            "period": "AM", "anglers": 22,
        }
        trip_a = {**base, "species": [{"species": "Calico Bass", "kept": 18, "released": 4}]}
        trip_b = {**base, "species": [{"species": "Calico Bass", "kept": 19, "released": 4}]}
        self.assertNotEqual(fingerprint(trip_a), fingerprint(trip_b))


if __name__ == "__main__":
    unittest.main()
