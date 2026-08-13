#!/usr/bin/env bash
# Prepare an Amazon Linux 2023 arm64 host to run CareLoop. Runs ON the instance, as a sudoer.
#
# Idempotent: safe to re-run, and push.sh does re-run it on every deploy so that a host rebuilt
# from scratch and a host updated in place converge on the same thing. A bootstrap that only
# works once is a bootstrap nobody dares run.
set -euo pipefail

APP=/opt/careloop/app
VENV=/opt/careloop/venv
STATE=/opt/careloop/state
# Where uv's managed interpreters go. Under /opt rather than a home directory — see the note
# beside UV_PYTHON_INSTALL_DIR below, which is not a preference.
PYTHONS=/opt/careloop/python

echo "== packages"
sudo dnf install -y git tar gzip >/dev/null

echo "== careloop user"
if ! id careloop >/dev/null 2>&1; then
    sudo useradd --system --home-dir /opt/careloop --shell /sbin/nologin careloop
fi
sudo mkdir -p "$APP" "$STATE" "$PYTHONS"

echo "== python 3.12"
# uv rather than dnf: AL2023's python3 is not 3.12, and CareLoop pins >=3.12. uv installs a
# standalone build, which also makes the deployed interpreter the same one the tests ran on
# rather than whatever the distribution happens to ship this month.
if [ ! -x /usr/local/bin/uv ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null
fi

# UV_PYTHON_INSTALL_DIR is load-bearing, and getting it wrong fails *after* a clean install.
# By default `sudo uv python install` puts the interpreter under /root/.local/share/uv/python
# and `uv venv` symlinks venv/bin/python at it. The service then cannot start, twice over: the
# careloop user cannot read /root (mode 0700), and ProtectHome=true in the unit hides it anyway.
# systemd reports that as "Failed to locate executable .../venv/bin/python: No such file or
# directory" — which reads as a missing venv rather than as an unreadable symlink target, and
# `ls` shows the file present the whole time.
export UV_PYTHON_INSTALL_DIR=$PYTHONS
sudo mkdir -p "$PYTHONS"
sudo env UV_PYTHON_INSTALL_DIR="$PYTHONS" /usr/local/bin/uv python install 3.12 >/dev/null

echo "== venv"
# Recreate only when the interpreter is not usable. A venv rebuilt on every deploy would
# reinstall the whole dependency closure each time for nothing.
#
# "Usable" is tested **as the careloop user**, not as root. Testing as root is what let the first
# deploy through: root can read /root, so a venv symlinked into /root/.local passes as root and
# fails for the service. The two questions are different and only the second one matters.
usable() {
    sudo -u careloop "$VENV/bin/python" -c 'pass' >/dev/null 2>&1 || return 1
    case "$(readlink -f "$VENV/bin/python" 2>/dev/null)" in
        /root/*|/home/*) return 1 ;;
    esac
}
if ! usable; then
    sudo rm -rf "$VENV"
    sudo env UV_PYTHON_INSTALL_DIR="$PYTHONS" /usr/local/bin/uv venv --python 3.12 "$VENV" >/dev/null
fi
# The runtime dependencies only. Not `.[dev]`: the host does not run the test suite, and
# reportlab, pytest and the rest are a larger attack surface for no gain.
sudo /usr/local/bin/uv pip install --python "$VENV/bin/python" -e "$APP" >/dev/null

# The check the first deploy needed and did not have. Fails here, with the reason, rather than
# as systemd's "Failed to locate executable ... No such file or directory" for a file that `ls`
# shows present.
RESOLVED=$(readlink -f "$VENV/bin/python")
echo "   interpreter: $RESOLVED"
if ! usable; then
    echo "the venv interpreter is not usable as the careloop user: $RESOLVED" >&2
    echo "if it is under /root or /home, ProtectHome=true in the unit will hide it" >&2
    exit 1
fi

echo "== ownership"
sudo chown -R careloop:careloop /opt/careloop

echo "== systemd"
sudo cp "$APP/deploy/careloop-demoworld.service" /etc/systemd/system/
sudo cp "$APP/deploy/careloop-api.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable careloop-demoworld careloop-api >/dev/null
# restart, not `enable --now`: --now does nothing to a service already running, so a redeploy
# would leave the previous code serving and report success. The restart belongs here rather than
# in push.sh so that the wait below is guaranteed to be waiting on the new process.
sudo systemctl restart careloop-demoworld careloop-api

echo "== waiting for both to answer"
for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8100/clinic/slots >/dev/null 2>&1 &&
       curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
        echo "both up"
        exit 0
    fi
    sleep 2
done

echo "FAILED to come up; last 40 lines of each:" >&2
sudo journalctl -u careloop-demoworld -n 40 --no-pager >&2 || true
sudo journalctl -u careloop-api -n 40 --no-pager >&2 || true
exit 1
