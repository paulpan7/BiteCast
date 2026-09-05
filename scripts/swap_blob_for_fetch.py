#!/usr/bin/env python3
"""Replace index.html's inlined `const DB=` literal with a loaded data script.

The literal is ~10.65 MB of the file's 10.86 MB, so this cannot be an ordinary
find-and-replace -- the search string alone would be the whole blob. This script
locates it by its delimiters and rewrites the file in place.

WHY A <script src> AND NOT fetch()
----------------------------------
The obvious approach -- make the block `<script type="module">` and use a
top-level `await fetch(...)` -- is wrong here, and quietly so.

The page has two inline scripts, and the second one (the FleetTrack AIS viewer)
calls helpers declared at the top level of the first, `fmt` and `esc` among
them. Two classic scripts share one top-level script scope, so that works today.
Converting the first to a module moves its declarations into module scope, and
the second script's references silently become ReferenceErrors -- swallowed by
FleetTrack's own .catch, which then reports "No boat-track bundle found yet."
The bundle fetch returns 200 the whole time; nothing looks broken except the
panel. This was observed, not theorised.

Static analysis could not reliably enumerate which identifiers are shared (most
candidates turn out to be FleetTrack's own locals), so the safe move is to not
change scoping at all.

A classic external <script src> blocks parsing until it has executed, so by the
time the inline script runs the data is already there. Script scope, execution
order and every declaration stay exactly as they are; the only thing that
changes is where the bytes come from.

Usage:
    python3 scripts/swap_blob_for_fetch.py [--index index.html]
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"

MARKER = "const DB="
SCRIPT_OPEN = "<script>"

# Loaded before the inline script; defines window.__FLEETCAST_DB__.
DATA_SCRIPT = '<script src="api/db.js"></script>\n'

REPLACEMENT = """const DB = window.__FLEETCAST_DB__ || (() => {
  const main = document.querySelector('main');
  if (main && !document.getElementById('fleetcastDataError')) {
    const notice = document.createElement('p');
    notice.id = 'fleetcastDataError';
    notice.className = 'notice';
    notice.textContent = 'Live data is unavailable right now, so this page cannot render. '
      + 'The data service did not respond.';
    main.insertBefore(notice, main.firstChild);
  }
  throw new Error('FleetCast data service unavailable');
})();"""


def swap(source: str) -> str:
    blob_start = source.index(MARKER)
    blob_end = source.index(";\n", blob_start) + 1  # keep the newline, drop the ';'

    script_open = source.rindex(SCRIPT_OPEN, 0, blob_start)
    if source.count(SCRIPT_OPEN, script_open, blob_start) != 1:
        raise RuntimeError("unexpected markup between <script> and const DB=")

    return (
        source[:script_open]
        + DATA_SCRIPT
        + SCRIPT_OPEN
        + source[script_open + len(SCRIPT_OPEN):blob_start]
        + REPLACEMENT
        + source[blob_end:]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default=str(INDEX))
    args = parser.parse_args()

    path = Path(args.index)
    source = path.read_text(encoding="utf-8")
    if MARKER not in source:
        raise SystemExit("index.html has no `const DB=` literal -- already swapped?")

    before = len(source.encode("utf-8"))
    swapped = swap(source)
    after = len(swapped.encode("utf-8"))
    path.write_text(swapped, encoding="utf-8")

    print(f"index.html: {before:,} -> {after:,} bytes "
          f"({100 * (before - after) / before:.1f}% smaller)")


if __name__ == "__main__":
    main()
