#!/usr/bin/env bash
#
# Keep PythonAnywhere's MySQL in step with the repository, unattended.
#
# Nothing connects git to MySQL on its own. The scrapers and the forecast job
# write index.html and push; without this, MySQL keeps serving whatever snapshot
# it was loaded with and drifts further every day.
#
# Set this up as a PythonAnywhere scheduled task (Tasks tab):
#
#     /home/fleetcast/BiteCast/scripts/pa_refresh.sh
#
# Schedule it a little after the forecast job (12:30 and 00:30 UTC) so it picks
# up each refresh. Credentials come from ~/.fleetcast_env, which lives outside
# the repo; see README.
#
# Safe to run repeatedly: the migration reloads the fact tables inside one
# transaction, so a reader mid-run sees the old data or the new, never a
# half-empty database.

set -euo pipefail

REPO="${FLEETCAST_REPO:-$HOME/BiteCast}"
ENV_FILE="${FLEETCAST_ENV_FILE:-$HOME/.fleetcast_env}"
PYTHON="$REPO/.venv/bin/python"

log() { printf '%s  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

log "refresh starting"

if [ ! -f "$ENV_FILE" ]; then
  log "ERROR: $ENV_FILE not found -- see README, 'Keeping PythonAnywhere in sync'"
  exit 1
fi
# shellcheck disable=SC1090
. "$ENV_FILE"

if [ ! -x "$PYTHON" ]; then
  log "ERROR: no virtualenv at $PYTHON -- create it with requirements-server.txt"
  exit 1
fi

cd "$REPO"

before="$(git rev-parse HEAD)"
git pull --ff-only origin main
after="$(git rev-parse HEAD)"

if [ "$before" = "$after" ]; then
  log "repository already current at ${after:0:8}"
else
  log "pulled ${before:0:8} -> ${after:0:8}"
fi

# Always re-sync even when git did not move: the database can be behind for
# other reasons (a failed earlier run, a manual change), and the migration
# verifies itself, so a redundant run is cheap insurance rather than waste.
log "syncing MySQL from index.html"
"$PYTHON" scripts/migrate_db_blob.py

log "refitting model"
"$PYTHON" scripts/model_fit.py --status live

log "refresh complete"
