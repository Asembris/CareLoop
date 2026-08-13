#!/usr/bin/env bash
# Build the frontend locally and ship the tree to the instance. Runs LOCALLY, from a checkout.
#
#   deploy/push.sh ec2-user@ec2-1-2-3-4.compute-1.amazonaws.com [path/to/key.pem]
#
# The frontend is built here rather than on the server, so Node never goes on the box: the
# artefact that gets served is the bundle that was tested, and a host that cannot build one
# cannot accidentally serve a different one.
set -euo pipefail

TARGET=${1:?usage: deploy/push.sh <ssh-target> [identity-file]}
KEY=${2:-}
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
[ -n "$KEY" ] && SSH_OPTS+=(-i "$KEY")

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== building the frontend"
( cd frontend && npm run build )
test -f frontend/dist/index.html || { echo "no frontend/dist/index.html after build" >&2; exit 1; }

echo "== packing"
# Excludes: node_modules and .git are large and unneeded; .env must never leave this machine;
# the SQLite files and their WAL sidecars are the *server's* state, and shipping a local copy
# would overwrite the durable source of truth with a developer's scratch database.
TAR=$(mktemp -t careloop-XXXXXX.tar.gz)
trap 'rm -f "$TAR"' EXIT
tar --exclude='./frontend/node_modules' \
    --exclude='./.git' \
    --exclude='./.venv' \
    --exclude='./.env' \
    --exclude='./**/__pycache__' \
    --exclude='./*.sqlite3*' \
    --exclude='./frontend/reference' \
    -czf "$TAR" .

echo "== shipping $(du -h "$TAR" | cut -f1)"
scp "${SSH_OPTS[@]}" "$TAR" "$TARGET:/tmp/careloop.tar.gz"

echo "== unpacking and bootstrapping"
ssh "${SSH_OPTS[@]}" "$TARGET" bash -s <<'REMOTE'
set -euo pipefail
sudo mkdir -p /opt/careloop/app
# --overwrite rather than clearing the directory: /opt/careloop/state is a sibling, not a child,
# so nothing durable is inside the tree being replaced.
sudo tar -xzf /tmp/careloop.tar.gz -C /opt/careloop/app --overwrite
rm -f /tmp/careloop.tar.gz
sudo bash /opt/careloop/app/deploy/bootstrap.sh
sudo systemctl restart careloop-demoworld careloop-api
REMOTE

echo "== checking"
ssh "${SSH_OPTS[@]}" "$TARGET" 'curl -fsS http://127.0.0.1:8000/api/health && echo && curl -fsS http://127.0.0.1:8100/clinic/slots | head -c 200 && echo'
echo "done"
