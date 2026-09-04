"""BiteCast WSGI entry point for the PythonAnywhere web app.

Point that account's WSGI config (/var/www/<domain>_wsgi.py) at this module --
see README.md's PythonAnywhere section.

This used to serve the site as a pure static mirror, with GitHub Pages as
canonical. It now exposes the Flask application in scripts/app.py, which serves
the same page shell off disk but backs its data with MySQL instead of a 10.65 MB
JSON literal inlined into index.html.

Database credentials come from the environment (BITECAST_DB_*), never the repo;
see scripts/db.py. The web app needs those set in the PythonAnywhere dashboard.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import app as application  # noqa: E402  (path setup must precede import)

__all__ = ["application"]
