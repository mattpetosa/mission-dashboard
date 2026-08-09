#!/usr/bin/env bash
#
# Push the working tree to the running site.
#
# The repo is the source of truth; /var/www/mission.mhpserver.cc is a deploy target.
# Nothing should be edited in the served directory — this script overwrites it,
# so an edit made there disappears on the next deploy.
#
#   ./deploy/deploy.sh          # sync static + restart the API
#   ./deploy/deploy.sh --static # static only, no restart
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEBROOT="/var/www/mission.mhpserver.cc"
SERVICE="mission-backend"
PORT=8793
SRC="www"

cd "$REPO"

[[ -n "$(git status --porcelain)" ]] && echo "note: working tree is dirty — deploying uncommitted changes"

# Refuse to empty the webroot for a source that does not look like the site.
count=$(find "$REPO/$SRC" -type f 2>/dev/null | wc -l)
if [[ ! -f "$REPO/$SRC/index.html" || "$count" -lt 3 ]]; then
    echo "REFUSING: $SRC/ has $count files and no usable index.html." >&2
    echo "That does not look like the site. Nothing was changed." >&2
    exit 1
fi

echo "==> syncing $SRC/ ($count files) -> $WEBROOT"
find "$WEBROOT" -mindepth 1 -delete
cp -a "$REPO/$SRC/." "$WEBROOT/"

if [[ "${1:-}" != "--static" ]]; then
    echo "==> restarting $SERVICE"
    sudo systemctl restart "$SERVICE"
    sleep 2
    systemctl is-active --quiet "$SERVICE" || {
        echo "FAILED: $SERVICE did not come back up" >&2
        sudo journalctl -u "$SERVICE" -n 30 --no-pager >&2
        exit 1
    }
fi

echo "==> verifying the API is listening on $PORT"
# Any HTTP response means the app is up; the endpoint set differs per project,
# so this deliberately checks reachability rather than a particular route.
curl -sf -o /dev/null --max-time 5 "http://127.0.0.1:$PORT/" \
  || curl -s -o /dev/null --max-time 5 "http://127.0.0.1:$PORT/" \
  || { echo "FAILED: nothing answering on 127.0.0.1:$PORT" >&2; exit 1; }

echo "deployed $(git rev-parse --short HEAD 2>/dev/null || echo 'working tree')"
