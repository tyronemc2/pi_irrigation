#!/usr/bin/env bash
# Install or update the greenhouse dashboard on a Raspberry Pi.
#   sudo ./deploy/install.sh               install / update
#   sudo ./deploy/install.sh --tailscale   also set up HTTPS via Tailscale (needed for
#                                          a full app install on Android, and for
#                                          checking the greenhouse away from home)
# Safe to run again after `git pull`: your config and schedules are kept.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/pi_irrigation}"
CONF_DIR="${CONF_DIR:-/etc/pi_irrigation}"
STATE_DIR="${STATE_DIR:-/var/lib/pi_irrigation}"
SERVICE_DIR="${SERVICE_DIR:-/etc/systemd/system}"
SKIP_SYSTEM="${SKIP_SYSTEM:-0}"   # 1 = no apt/user/systemd (used for testing)
PORT=8080
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WANT_TAILSCALE=0
[[ "${1:-}" == "--tailscale" ]] && WANT_TAILSCALE=1

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if [[ "$SKIP_SYSTEM" != 1 && $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 $*"; exit 1
fi

if [[ "$SKIP_SYSTEM" != 1 ]]; then
  say "Installing system packages"
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip curl avahi-daemon >/dev/null
  if ! id irrigation >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin irrigation
  fi
fi

say "Copying app to $APP_DIR"
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/irrigation"   # drop old code; keeps the .venv
tar -C "$SRC" --exclude=.git --exclude=.venv --exclude='__pycache__' --exclude=tests \
  --exclude=firmware -cf - . | tar -C "$APP_DIR" -xf -

say "Setting up Python environment"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

mkdir -p "$CONF_DIR" "$STATE_DIR"
if [[ ! -f "$CONF_DIR/config.yaml" ]]; then
  cp "$SRC/config.example.yaml" "$CONF_DIR/config.yaml"
  say "Created $CONF_DIR/config.yaml - edit it before relying on the system"
  NEW_CONFIG=1
else
  echo "Keeping existing $CONF_DIR/config.yaml"
  NEW_CONFIG=0
fi

say "Checking config"
(cd "$APP_DIR" && "$APP_DIR/.venv/bin/python" - "$CONF_DIR/config.yaml") <<'PY'
import sys
from irrigation.config import load_config, ConfigError
try:
    c = load_config(sys.argv[1])
    print(f"  OK: {len(c['zones'])} zones, controller {c['controller']['mode']} at {c['controller']['url']}")
except ConfigError as e:
    print(f"  Config problem: {e}"); sys.exit(1)
PY

if [[ "$SKIP_SYSTEM" != 1 ]]; then
  chown -R irrigation:irrigation "$STATE_DIR"
  chown root:irrigation "$CONF_DIR/config.yaml"
  chmod 640 "$CONF_DIR/config.yaml"   # may hold the API key and ntfy topic

  say "Installing service"
  install -m 644 "$SRC/deploy/pi-irrigation.service" "$SERVICE_DIR/pi-irrigation.service"
  systemctl daemon-reload
  systemctl enable pi-irrigation >/dev/null 2>&1
  systemctl restart pi-irrigation
  sleep 3
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null; then
    echo "  Dashboard is running."
  else
    echo "  The service didn't answer. See: journalctl -u pi-irrigation -n 50"
  fi

  if [[ $WANT_TAILSCALE == 1 ]]; then
    say "Setting up Tailscale HTTPS"
    command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
    tailscale status >/dev/null 2>&1 || tailscale up
    tailscale serve --bg "$PORT"
    TS_NAME="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
    echo "  HTTPS address: https://$TS_NAME"
    echo "  (If this asked you to enable HTTPS, do that in the Tailscale admin page and run again.)"
  fi

  LAN_IP="$(hostname -I | awk '{print $1}')"
  say "Done"
  echo "  On your home WiFi:  http://$LAN_IP:$PORT   or   http://$(hostname).local:$PORT"
  [[ $WANT_TAILSCALE == 1 ]] && echo "  Anywhere (installs as an app on Android and iPhone): https://$TS_NAME"
  [[ $NEW_CONFIG == 1 ]] && echo "  Next: sudo nano $CONF_DIR/config.yaml  then  sudo systemctl restart pi-irrigation"
fi
