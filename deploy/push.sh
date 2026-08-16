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
# Excludes, and three of them are not about size.
#
#   .keys/  the SSH private key that authorises this very deploy. A tarball of the repository
#           root sweeps it up by default, and shipping it to the host it opens would be the
#           worst kind of quiet mistake — it works perfectly and leaves the key on the box.
#   .env    developer credentials. The host reaches Bedrock through its instance profile and
#           needs no key at all; see deploy/careloop.env.
#   *.sqlite3*  the server's durable source of truth, WAL sidecars included. Shipping a local
#           copy would overwrite it with a developer's scratch database.
#   e2e/    the end-to-end suite. Test code has no business on the instance: it is not what is
#           being served, it names ports and hosts the deployment does not use, and the one thing
#           worse than shipping it is shipping it and having somebody run it there.
#
# The patterns are unanchored on purpose: `./**/__pycache__` is a bashism tar does not expand,
# so a leading `./` here would have matched only the repository root.
TAR=$(mktemp -t careloop-XXXXXX.tar.gz)
trap 'rm -f "$TAR"' EXIT
tar --exclude='.keys' \
    --exclude='.env' \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='node_modules' \
    --exclude='__pycache__' \
    --exclude='*.sqlite3*' \
    --exclude='*.pem' \
    --exclude='frontend/reference' \
    --exclude='e2e' \
    -czf "$TAR" .

echo "== confirming nothing secret is in the tarball"
if tar -tzf "$TAR" | grep -Ei '(^|/)(\.env$|\.keys/|.*\.pem$|.*\.sqlite3)' ; then
    echo "refusing to ship: the tarball contains something it must not" >&2
    exit 1
fi

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
REMOTE

echo "== checking the origin"
ssh "${SSH_OPTS[@]}" "$TARGET" 'curl -fsS http://127.0.0.1:8000/api/health && echo && curl -fsS http://127.0.0.1:8100/clinic/slots | head -c 200 && echo'

# The origin answering is liveness, and liveness is not what breaks. What breaks is the class of
# defect that only exists once the CDN, the units and the built bundle are in front of the code —
# a stripped session header, a cache policy serving one visitor's cases to another, a unit that
# lost --isolate-sessions, a shell naming a bundle that was never shipped. CI cannot see any of
# those, because CI runs the code and not the deployment.
#
# Through the distribution, never the origin: a check against 127.0.0.1 passes in exactly the
# situations this exists to catch. Read-only apart from the two worlds it mints for itself, so it
# is safe to run while somebody is using the demo.
#
# CARELOOP_DEMO_URL overrides the distribution; skipping is deliberate and must be deliberate.
echo "== verifying the deployment through the CDN"
if [ "${SKIP_DEPLOY_VERIFY:-}" = "1" ]; then
    echo "   SKIPPED by SKIP_DEPLOY_VERIFY=1 — the deploy is unverified"
else
    # The repo's venv first, on either platform, then whatever is on PATH. The script imports only
    # the standard library, so any 3.12 will do; this is about finding one rather than about which.
    PY=""
    for candidate in "$ROOT/.venv/Scripts/python.exe" "$ROOT/.venv/bin/python" python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
    done
    [ -n "$PY" ] || { echo "no python found to run scripts/verify_deploy.py" >&2; exit 1; }
    "$PY" "$ROOT/scripts/verify_deploy.py" ${CARELOOP_DEMO_URL:+"$CARELOOP_DEMO_URL"}
fi
echo "done"
