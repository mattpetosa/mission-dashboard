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
#   ./deploy/deploy.sh --force  # sync even if the webroot looks hand-edited
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEBROOT="/var/www/mission.mhpserver.cc"
SERVICE="mission-backend"
PORT=8793
SRC="www"

cd "$REPO"

STATIC_ONLY=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --static) STATIC_ONLY=1 ;;
        --force)  FORCE=1 ;;
        *) echo "unknown option: $arg (want --static and/or --force)" >&2; exit 2 ;;
    esac
done


[[ -n "$(git status --porcelain)" ]] && echo "note: working tree is dirty — deploying uncommitted changes"

# Refuse to empty the webroot for a source that does not look like the site.
count=$(find "$REPO/$SRC" -type f 2>/dev/null | wc -l)
if [[ ! -f "$REPO/$SRC/index.html" || "$count" -lt 3 ]]; then
    echo "REFUSING: $SRC/ has $count files and no usable index.html." >&2
    echo "That does not look like the site. Nothing was changed." >&2
    exit 1
fi

# Has anyone edited the SERVED copy directly? Until this repo existed those
# directories WERE the source, so the habit is months old — and the sync below
# deletes the webroot outright, which would throw the work away with no error
# and no copy anywhere. Two signals, both cheap:
#
#   extra  — a file served that the repo has never heard of
#   newer  — a served file with a later mtime than its source twin. cp -a
#            preserves timestamps, so after a clean deploy the two match
#            exactly; the webroot being ahead means somebody typed into it.
#
# Editing the SOURCE makes the source newer, which is the normal case and
# deliberately does not trigger. VERSION is excluded — this script writes it.
extra=$(comm -23 \
    <(cd "$WEBROOT" && find . -type f | grep -vx './VERSION' | sort) \
    <(cd "$REPO/$SRC" && find . -type f | sort))
newer=$(cd "$WEBROOT" && find . -type f ! -name VERSION -print0 \
        | while IFS= read -r -d '' f; do
              # `if`, not `[[ … ]] && printf`: the && form returns 1 whenever the
              # test is false, so the last file decides the exit status of the
              # whole substitution and `set -e` kills the script. That failure
              # blocked every deploy, not just hand-edited ones.
              if [[ -f "$REPO/$SRC/$f" && "$f" -nt "$REPO/$SRC/$f" ]]; then
                  printf '%s\n' "$f"
              fi
          done)

if [[ -n "$extra$newer" && $FORCE -eq 0 ]]; then
    echo "REFUSING: $WEBROOT looks edited by hand." >&2
    [[ -n "$extra" ]] && { echo "  files served but not in $SRC/:" >&2
                           printf '    %s\n' $extra >&2; }
    [[ -n "$newer" ]] && { echo "  files newer than their $SRC/ copy:" >&2
                           printf '    %s\n' $newer >&2; }
    cat >&2 <<EOF

Deploying would delete those. Copy anything worth keeping into $SRC/ first:
    cp $WEBROOT/<file> $REPO/$SRC/<file>

Then commit it. If they really are disposable, re-run with --force.
EOF
    exit 1
fi

echo "==> syncing $SRC/ ($count files) -> $WEBROOT"
find "$WEBROOT" -mindepth 1 -delete
cp -a "$REPO/$SRC/." "$WEBROOT/"

# Record what was actually deployed, fetchable at /VERSION. Written after the
# copy, or the sync above would delete it. `--dirty` is the important flag:
# it marks a deploy made from uncommitted changes, which is the state you most
# want to know about later and the one nobody remembers being in.
{
  echo "version:  $(git describe --tags --always --dirty 2>/dev/null || echo unknown)"
  echo "commit:   $(git rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "deployed: $(date -Is)"
} > "$WEBROOT/VERSION"

if [[ $STATIC_ONLY -eq 0 ]]; then
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

echo "deployed $(git describe --tags --always --dirty 2>/dev/null || echo 'working tree')"
