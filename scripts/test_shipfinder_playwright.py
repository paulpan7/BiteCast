"""Tests for the pure CSV-parsing/merge logic in shipfinder_playwright.py.

The Playwright browser-automation half can't be exercised without live
ShipFinder credentials, so this covers only what's testable offline: turning
a downloaded CSV into the bundle's point shape, and merging fresh points
into a boat's retained history without duplicating or over-retaining.

Run with: python3 -m unittest test_shipfinder_playwright -v
"""
from __future__ import annotations

import csv
import subprocess
import tempfile
import unittest
from pathlib import Path

from shipfinder_playwright import git_commit_and_push, login, merge_points, parse_points, parse_timestamp


def write_csv(rows: list[dict]) -> Path:
    fd = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    writer = csv.DictWriter(fd, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    fd.close()
    return Path(fd.name)


# Confirmed against a real ShipFinder export (2026-09-02): GB18030-encoded,
# full-width Chinese parens in the header, plain decimal Longitude/Latitude
# columns alongside deg-minute ones, and an explicitly UTC-labeled column.
REAL_HEADER = (
    "Longitude(deg-minute),Latitude(deg-minute),Longitude,Latitude,"
    "Ship speed(kn),Ship course,Ship heading,"
    "Ship Turning Rate（deg/second）,Navigation status,"
    "Last update（CST）,Last update（UTC）"
)


def write_real_format_csv(rows: list[str]) -> Path:
    """rows: raw data-row strings (already comma-joined), matching REAL_HEADER's
    column order. Encoded exactly as the real export is (GB18030, CRLF)."""
    fd = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    content = "\r\n".join([REAL_HEADER, *rows, ""])
    fd.write(content.encode("gb18030"))
    fd.close()
    return Path(fd.name)


class ParseTimestampTests(unittest.TestCase):
    def test_iso_with_z(self):
        self.assertIsNotNone(parse_timestamp("2026-09-02T18:40:36Z"))

    def test_space_separated(self):
        self.assertIsNotNone(parse_timestamp("2026-09-02 18:40:36"))

    def test_naive_treated_as_utc_by_default(self):
        parsed = parse_timestamp("2026-09-02 18:40:36")
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_naive_tz_is_configurable(self):
        # FLEETCAST_CSV_NAIVE_TZ (module attribute CSV_NAIVE_TZ) lets a naive CSV
        # timestamp be reinterpreted as Pacific instead of UTC, in case
        # ShipFinder's real export turns out to omit an offset and report local
        # time -- see the TIMEZONE CAVEAT in shipfinder_playwright.py's docstring.
        import shipfinder_playwright as module
        from zoneinfo import ZoneInfo
        original = module.CSV_NAIVE_TZ
        module.CSV_NAIVE_TZ = ZoneInfo("America/Los_Angeles")
        try:
            parsed = module.parse_timestamp("2026-09-02 11:40:36")
            self.assertEqual(parsed.utcoffset().total_seconds(), -7 * 3600)  # PDT in September
        finally:
            module.CSV_NAIVE_TZ = original

    def test_empty_returns_none(self):
        self.assertIsNone(parse_timestamp(""))

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_timestamp("not a date"))


class RealShipfinderFormatTests(unittest.TestCase):
    """Regression coverage for the confirmed real export format -- catches a
    silent parse failure if a future ShipFinder export changes encoding or
    column names, since that would otherwise skip every row with no error."""

    def test_real_format_row_is_parsed(self):
        path = write_real_format_csv([
            "117-13.803W,32-40.753N,-117.230058,32.679218,009.1,173.3,157,327.67,Unknown,2026-09-01 22:23:28,2026-09-01 14:23:28",
        ])
        points = parse_points(path)
        self.assertEqual(len(points), 1)
        p = points[0]
        self.assertAlmostEqual(p["lat"], 32.679218)
        self.assertAlmostEqual(p["lon"], -117.230058)
        self.assertAlmostEqual(p["speed"], 9.1)
        self.assertAlmostEqual(p["course"], 173.3)
        self.assertAlmostEqual(p["heading"], 157)
        self.assertEqual(p["status"], "Unknown")

    def test_utc_column_is_trusted_directly_not_guessed(self):
        # The UTC column's value, not the CST one, must end up in p["utc"] --
        # and treated as UTC regardless of CSV_NAIVE_TZ.
        path = write_real_format_csv([
            "117-13.803W,32-40.753N,-117.230058,32.679218,009.1,173.3,157,327.67,Unknown,2026-09-01 22:23:28,2026-09-01 14:23:28",
        ])
        p = parse_points(path)[0]
        self.assertEqual(p["utc"], "2026-09-01T14:23:28Z")
        self.assertEqual(p["local"], "2026-09-01T07:23:28-07:00")  # UTC-7 in September (PDT)

    def test_header_only_file_yields_no_points(self):
        path = write_real_format_csv([])
        self.assertEqual(parse_points(path), [])

    def test_empty_heading_becomes_none(self):
        path = write_real_format_csv([
            "116-18.312W,30-7.302N,-116.3052,30.1217,009.4,159,,0,Under way using engine,2026-09-02 21:07:18,2026-09-02 13:07:18",
        ])
        self.assertIsNone(parse_points(path)[0]["heading"])

    def test_moored_status_is_preserved(self):
        path = write_real_format_csv([
            "118-23.074W,32-49.002N,-118.384567,32.816708,000.6,358.3,88,12,Moored,2026-09-03 14:23:21,2026-09-03 06:23:21",
        ])
        self.assertEqual(parse_points(path)[0]["status"], "Moored")


class ParsePointsTests(unittest.TestCase):
    def test_standard_columns(self):
        path = write_csv([
            {"latitude": "32.7", "longitude": "-117.2", "timestamp": "2026-09-02T18:40:36Z", "speed": "8.6", "course": "119.2", "status": "Under way"},
            {"latitude": "32.71", "longitude": "-117.21", "timestamp": "2026-09-02T18:42:36Z", "speed": "2.0", "course": "90.0", "status": "Under way"},
        ])
        points = parse_points(path)
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["utc"], "2026-09-02T18:40:36Z")
        self.assertEqual(points[0]["local"], "2026-09-02T11:40:36-07:00")
        self.assertEqual(points[0]["speed"], 8.6)
        self.assertEqual(points[0]["status"], "Under way")

    def test_sorted_chronologically_regardless_of_row_order(self):
        path = write_csv([
            {"lat": "32.71", "lon": "-117.21", "time": "2026-09-02T18:42:36Z", "sog": "2.0"},
            {"lat": "32.70", "lon": "-117.20", "time": "2026-09-02T18:40:36Z", "sog": "8.6"},
        ])
        points = parse_points(path)
        self.assertEqual([p["utc"] for p in points], ["2026-09-02T18:40:36Z", "2026-09-02T18:42:36Z"])

    def test_row_missing_lat_lon_is_skipped(self):
        path = write_csv([
            {"lat": "", "lon": "-117.2", "time": "2026-09-02T18:40:36Z", "sog": "8.6"},
            {"lat": "32.7", "lon": "-117.2", "time": "2026-09-02T18:41:36Z", "sog": "8.6"},
        ])
        self.assertEqual(len(parse_points(path)), 1)

    def test_missing_speed_and_course_become_none_not_zero(self):
        path = write_csv([{"lat": "32.7", "lon": "-117.2", "time": "2026-09-02T18:40:36Z"}])
        points = parse_points(path)
        self.assertIsNone(points[0]["speed"])
        self.assertIsNone(points[0]["course"])

    def test_missing_status_defaults_to_unknown(self):
        path = write_csv([{"lat": "32.7", "lon": "-117.2", "time": "2026-09-02T18:40:36Z"}])
        self.assertEqual(parse_points(path)[0]["status"], "Unknown")


class MergePointsTests(unittest.TestCase):
    def test_new_points_are_added(self):
        existing = [{"utc": "2026-09-01T12:00:00Z", "lat": 32.7, "lon": -117.2}]
        fresh = [{"utc": "2026-09-02T12:00:00Z", "lat": 32.71, "lon": -117.21}]
        merged = merge_points(existing, fresh, retention_cutoff="2026-08-01T00:00:00Z")
        self.assertEqual(len(merged), 2)

    def test_duplicate_report_is_not_added_twice(self):
        existing = [{"utc": "2026-09-01T12:00:00Z", "lat": 32.7, "lon": -117.2}]
        fresh = [{"utc": "2026-09-01T12:00:00Z", "lat": 32.7, "lon": -117.2}]
        merged = merge_points(existing, fresh, retention_cutoff="2026-08-01T00:00:00Z")
        self.assertEqual(len(merged), 1)

    def test_result_is_sorted_by_time(self):
        existing = [{"utc": "2026-09-02T12:00:00Z", "lat": 32.7, "lon": -117.2}]
        fresh = [{"utc": "2026-09-01T12:00:00Z", "lat": 32.71, "lon": -117.21}]
        merged = merge_points(existing, fresh, retention_cutoff="2026-08-01T00:00:00Z")
        self.assertEqual([p["utc"] for p in merged], ["2026-09-01T12:00:00Z", "2026-09-02T12:00:00Z"])

    def test_points_older_than_retention_are_dropped(self):
        existing = [
            {"utc": "2026-01-01T12:00:00Z", "lat": 32.7, "lon": -117.2},
            {"utc": "2026-09-01T12:00:00Z", "lat": 32.71, "lon": -117.21},
        ]
        merged = merge_points(existing, fresh=[], retention_cutoff="2026-08-01T00:00:00Z")
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["utc"], "2026-09-01T12:00:00Z")


class LoginCredentialCheckTests(unittest.TestCase):
    """login() itself needs a real Playwright page and can't be exercised
    offline, but the credential check must fail before ever touching the
    page -- verified here by passing None and confirming it never gets used."""

    def setUp(self):
        import os
        self._env = {k: os.environ.pop(k, None) for k in ("SHIPFINDER_EMAIL", "SHIPFINDER_PASSWORD")}

    def tearDown(self):
        import os
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v

    def test_missing_credentials_raises_before_touching_page(self):
        with self.assertRaisesRegex(RuntimeError, "SHIPFINDER_EMAIL"):
            login(page=None)

    def test_missing_password_only_still_raises(self):
        import os
        os.environ["SHIPFINDER_EMAIL"] = "someone@example.com"
        with self.assertRaisesRegex(RuntimeError, "SHIPFINDER_PASSWORD"):
            login(page=None)


def make_local_repo_with_remote():
    """A real local repo + bare 'remote', so git_commit_and_push's add/commit/
    push can be exercised end-to-end without touching the network or GitHub."""
    root = Path(tempfile.mkdtemp())
    remote = root / "remote.git"
    work = root / "work"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Test"], check=True)
    # a bare repo has no branches yet; make an initial commit so push has something to fast-forward
    (work / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(work), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "seed"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "-u", "origin", "HEAD"], check=True)
    return work, remote


class GitCommitAndPushTests(unittest.TestCase):
    def test_commits_and_pushes_a_changed_file(self):
        work, remote = make_local_repo_with_remote()
        bundle = work / "bundle.json"
        bundle.write_text('{"generated":"now"}')
        git_commit_and_push(work, [bundle], "Refresh bundle")
        log = subprocess.run(["git", "-C", str(remote), "log", "-1", "--format=%s"], capture_output=True, text=True, check=True)
        self.assertEqual(log.stdout.strip(), "Refresh bundle")

    def test_no_changes_is_a_silent_noop_not_an_error(self):
        work, remote = make_local_repo_with_remote()
        bundle = work / "bundle.json"
        bundle.write_text('{"generated":"now"}')
        git_commit_and_push(work, [bundle], "First push")
        before = subprocess.run(["git", "-C", str(remote), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout
        git_commit_and_push(work, [bundle], "Should not create a commit")  # identical content again
        after = subprocess.run(["git", "-C", str(remote), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
