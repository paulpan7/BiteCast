"""BiteCast static-site launcher for the PythonAnywhere WSGI configuration.

GitHub Pages is the canonical site; this just mirrors the same static
checkout on PythonAnywhere. There's no build step and nothing dynamic --
every request is served straight from files already on disk, kept current
by a separate `git pull` (see README.md's PythonAnywhere section), not by
anything this launcher does at request time.
"""
from pathlib import Path

from flask import Flask, send_from_directory

SITE_DIR = Path(__file__).resolve().parent.parent

app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    return send_from_directory(SITE_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(SITE_DIR, path)


application = app
