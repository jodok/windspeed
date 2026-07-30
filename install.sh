#!/usr/bin/env bash
# Install (or remove) windspeed's systemd timers on a Linux host.
#
#   ./install.sh              # set up the venv and install + enable every timer
#   ./install.sh --uninstall  # stop, disable and remove them again
#
# Run it as the user the pollers should run as, from the checkout they should
# run out of -- both are read from the environment rather than configured, so
# there is one less thing to keep in sync:
#
#   ssh ansible@app-btlg-civ-01.khumbu.namche.net
#   git clone https://github.com/jodok/windspeed.git ~/sandbox/windspeed
#   cd ~/sandbox/windspeed && ./install.sh
#
# It needs sudo for the unit files and `systemctl` only; everything under the
# checkout is done as the invoking user. It is idempotent: re-run it after a
# `git pull` to pick up a changed unit or cadence.
set -euo pipefail

RUN_USER="$(id -un)"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR=/etc/systemd/system
SYSTEMD_SRC="$REPO_DIR/systemd"

log() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() {
  printf '\033[31merror:\033[0m %s\n' "$*" >&2
  exit 1
}

# The timer files present in systemd/ ARE the station list -- adding a station
# means committing a windspeed@<station>.timer, not editing this script.
station_timers() {
  local f
  for f in "$SYSTEMD_SRC"/windspeed@*.timer; do
    [ -e "$f" ] || continue
    basename "$f"
  done
}

all_timers() {
  station_timers
  echo windspeed-stale.timer
}

# --- uninstall ---------------------------------------------------------------
if [ "${1:-}" = "--uninstall" ]; then
  log "Stopping and disabling timers"
  # shellcheck disable=SC2046  # deliberate word splitting over unit names
  sudo systemctl disable --now $(all_timers) 2>/dev/null || true
  log "Removing unit files"
  for unit in $(all_timers) windspeed@.service windspeed-stale.service windspeed-mail@.service; do
    sudo rm -f "$UNIT_DIR/$unit"
  done
  sudo systemctl daemon-reload
  log "Removed. The checkout, .venv and state/ are untouched."
  exit 0
fi

[ "${1:-}" = "" ] || die "usage: $0 [--uninstall]"
[ "$RUN_USER" != root ] || die "run this as the user the pollers should run as, not root"
[ -d "$SYSTEMD_SRC" ] || die "$SYSTEMD_SRC not found -- run this from the checkout"
command -v systemctl >/dev/null || die "no systemctl; this installer is for Linux hosts"

# --- prerequisites ----------------------------------------------------------
log "Checking prerequisites"
python3 -c 'import venv' 2>/dev/null \
  || die "python3-venv is missing (ansible installs it via common_extra_packages; 'sudo apt install python3-venv' by hand)"
# Not fatal: the pollers work fine without mail, but the freshness alert has
# nowhere to go, which is the one thing that would fail silently.
[ -x /usr/sbin/sendmail ] \
  || warn "/usr/sbin/sendmail not found -- windspeed-stale.service can run but its alert mail will be lost. On the fleet this comes from ansible's common_mta role."

# --- virtualenv -------------------------------------------------------------
if [ ! -x "$REPO_DIR/.venv/bin/python" ]; then
  log "Creating .venv"
  python3 -m venv "$REPO_DIR/.venv"
fi
log "Installing requirements"
"$REPO_DIR/.venv/bin/pip" install --quiet --upgrade pip
# --only-binary :all: turns this host's "no compiler, no -dev headers" from an
# assumption into an enforced one. Without it, a dependency that ships no wheel
# for the host's interpreter falls back to a source build, which here fails
# somewhere inside a C compile with an error that has nothing to do with the
# real cause. With it, pip says plainly that no wheel is available.
"$REPO_DIR/.venv/bin/pip" install --quiet --upgrade --only-binary :all: -r "$REPO_DIR/requirements.txt"

# --- secrets ----------------------------------------------------------------
# Every station's upload password comes from here; without it each poll fails at
# the hash step. Deliberately NOT created or fetched by this script: it holds
# seven secrets and belongs in 1Password (op://... -- see README.md).
if [ ! -f "$REPO_DIR/.env" ]; then
  warn ".env is missing -- every upload will fail until it exists. See README.md 'Secrets'."
fi

# --- state ------------------------------------------------------------------
# One file per station now, instead of the single station_state.json that
# concurrent stations used to clobber. Split a legacy file if one is present:
# both on an in-place upgrade and on a state directory copied over from the Mac
# during the migration.
mkdir -p "$REPO_DIR/state"
LEGACY="$REPO_DIR/station_state.json"
if [ -f "$LEGACY" ]; then
  log "Migrating $LEGACY to state/<station>.json"
  "$REPO_DIR/.venv/bin/python" - "$LEGACY" "$REPO_DIR/state" <<'PY'
import json, pathlib, sys

legacy, dest = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
data = json.loads(legacy.read_text())
for station, unixtime in data.items():
    target = dest / f"{station}.json"
    if target.exists():
        print(f"    keeping newer {target.name}")
        continue
    target.write_text(json.dumps({"unixtime": unixtime}))
    print(f"    wrote {target.name} ({unixtime})")
PY
  mv "$LEGACY" "$LEGACY.migrated"
  log "Renamed the legacy file to station_state.json.migrated"
fi

# --- units ------------------------------------------------------------------
# The two service templates carry @@WINDSPEED_USER@@ / @@WINDSPEED_DIR@@
# placeholders because systemd cannot take User= or WorkingDirectory= from an
# EnvironmentFile. Substituting at install time is what lets the same units work
# for any user and any checkout path.
log "Installing unit files into $UNIT_DIR (as $RUN_USER, from $REPO_DIR)"
for unit in windspeed@.service windspeed-stale.service; do
  sed -e "s|@@WINDSPEED_USER@@|$RUN_USER|g" \
    -e "s|@@WINDSPEED_DIR@@|$REPO_DIR|g" \
    "$SYSTEMD_SRC/$unit" | sudo tee "$UNIT_DIR/$unit" >/dev/null
  sudo chmod 0644 "$UNIT_DIR/$unit"
done
# No placeholders in these: the mailer runs as root and the timers reference
# units by name only.
for unit in windspeed-mail@.service $(all_timers); do
  sudo install -m 0644 -o root -g root "$SYSTEMD_SRC/$unit" "$UNIT_DIR/$unit"
done

log "Reloading systemd and enabling timers"
sudo systemctl daemon-reload
# shellcheck disable=SC2046  # deliberate word splitting over unit names
sudo systemctl enable --now $(all_timers)

log "Done. Next runs:"
systemctl list-timers --all 'windspeed*' --no-pager
cat <<EOF

  Poll one station now:   systemctl start windspeed@rohrspitz
  Watch a station's log:  journalctl -fu windspeed@rohrspitz
  Check freshness now:    systemctl start windspeed-stale && systemctl status windspeed-stale
  Remove everything:      ./install.sh --uninstall
EOF
