#!/usr/bin/env bash
# Auto-deploy: if GitHub's main has moved, update this checkout and restart
# labelpi. Run every minute by labelpi-deploy.timer (see install.sh);
# safe to run by hand too. Logs to the journal: journalctl -t labelpi-deploy
#
#   1. fetch main; nothing new -> exit quietly
#   2. fast-forward to it (never merges; local edits make it stop and say so)
#   3. requirements.txt changed -> pip install (slow on a Pi Zero, so only then)
#   4. smoke test: the app must import and load its config
#   5. OK -> restart the service. Failed -> go back to the previous commit,
#      and remember the bad one so it isn't retried every minute.
set -euo pipefail

cd "$(dirname "$0")/.."
FAILED_FILE=".deploy-failed-commit" # gitignored

log() { logger -t labelpi-deploy -- "$*"; echo "$*"; }

# Never run twice at once (a slow pip install can outlast a minute).
exec 9>/tmp/labelpi-deploy.lock
flock -n 9 || exit 0

git fetch --quiet origin main
old=$(git rev-parse HEAD)
new=$(git rev-parse origin/main)
[ "$old" = "$new" ] && exit 0
if [ -f "$FAILED_FILE" ] && [ "$(cat "$FAILED_FILE")" = "$new" ]; then
    exit 0 # this commit already failed; wait for a newer one
fi

log "updating ${old:0:7} -> ${new:0:7}"
if ! git merge --ff-only --quiet origin/main; then
    log "ERROR: can't fast-forward - are there local changes? (git status)"
    exit 1
fi

requirements_changed() { ! git diff --quiet "$old" "$new" -- requirements.txt; }

smoke_test() {
    # Importing and building the app checks the code and config/printers.toml
    # without starting the server or touching the printers.
    .venv/bin/python -c "from labelpi.app import create_app; create_app()"
}

roll_back() {
    log "ERROR: $1 - going back to ${old:0:7}"
    git reset --quiet --hard "$old"
    if requirements_changed; then
        .venv/bin/pip install --quiet -r requirements.txt || true
    fi
    echo "$new" >"$FAILED_FILE"
    exit 1
}

if requirements_changed; then
    log "requirements.txt changed - installing (this can take a while on a Zero)"
    .venv/bin/pip install --quiet -r requirements.txt || roll_back "pip install failed"
fi
smoke_test || roll_back "smoke test failed"

rm -f "$FAILED_FILE"
sudo -n systemctl restart labelpi
log "deployed $(git log -1 --format='%h %s')"
