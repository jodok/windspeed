#!/usr/bin/env bash
# Install the windguru station uploads on a host. Idempotent -- safe to re-run
# after a `git pull` to pick up new dependencies.
#
# This repo is a HAND-MANAGED PAYLOAD on app-btlg-civ-01: Ansible owns the VM
# base (python3-venv, the MTA that carries cron's failure mail, patching) and
# stops there, so this script is the install contract. See NamcheAI/infra
# ansible/playbooks/btlg.yml for why the split is drawn there.
#
#   ./install.sh            # venv + deps + logrotate + cron
#   ./install.sh --no-cron  # everything except the crontab entries
#
# It deliberately does NOT run an upload or write .env: .env carries the
# per-station windguru passwords, which belong in the operator's hands rather
# than a script's.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$REPO_DIR/windspeed.log"
INSTALL_CRON=1

# Schedule per station, matching each source's own publishing rate.
#
# NOTE: these are the schedules as measured in production, and they align: at
# :00 and :30 all seven fire in the same second, which on tashi peaked at 7
# concurrent python processes of 18-37% CPU each. The per-station lockfile does
# NOT fix that -- by design, the stations are independent and must not block
# each other. On a 1-core host, offset them instead; the cadence is unchanged:
#   kressbronn */2 | lindau-lsc 1-56/5 | rohrspitz 2-57/5 | altenrhein 3-53/10
#   rohrspitz-zamg 4-54/10 | praia-bela-vista 5-55/10 | praia-da-rainha 6-51/15
SCHEDULES=(
  "*/2:kressbronn"
  "*/5:lindau-lsc"
  "*/5:rohrspitz"
  "*/10:altenrhein"
  "*/10:rohrspitz-zamg"
  "*/10:praia-bela-vista"
  "*/15:praia-da-rainha"
)

for arg in "$@"; do
  case "$arg" in
    --no-cron) INSTALL_CRON=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

echo "==> Installing windspeed from $REPO_DIR"

# --- venv ------------------------------------------------------------------
# Ubuntu splits `venv` out of python3; the btlg playbook installs python3-venv
# for exactly this line. A clear message beats venv's own partial failure.
if [ ! -x "$REPO_DIR/.venv/bin/python" ]; then
  echo "==> Creating virtualenv"
  python3 -m venv "$REPO_DIR/.venv" || {
    echo "ERROR: could not create a virtualenv. On Ubuntu: apt install python3-venv" >&2
    exit 1
  }
fi

# Every pin in requirements.txt is a pure-python wheel (py3-none-any), so this
# needs no compiler and no -dev headers -- which app-btlg-civ-01 deliberately
# does not have. Keep it that way: check any new dependency ships a wheel for
# the host's interpreter before adding it.
echo "==> Installing dependencies"
"$REPO_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$REPO_DIR/.venv/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

# --- .env ------------------------------------------------------------------
# Seeded once, never overwritten: re-running this script must not clobber real
# passwords with the example placeholders.
if [ ! -f "$REPO_DIR/.env" ]; then
  install -m 600 "$REPO_DIR/.env.example" "$REPO_DIR/.env"
  echo "==> Wrote a placeholder .env (0600) -- fill it in before the first run"
  NEEDS_ENV=1
else
  chmod 600 "$REPO_DIR/.env"
fi

# --- log rotation ----------------------------------------------------------
# Not optional hygiene: this log reached 35 MB unrotated on a 32 GB disk. Size
# based as well as daily, because one station whose source starts erroring
# writes far more than a normal day.
#
# copytruncate because the cron entries hold the file open through `>>` for the
# life of each run; renaming it out from under them would strand the writes.
if command -v logrotate >/dev/null 2>&1 && [ -d /etc/logrotate.d ]; then
  echo "==> Installing logrotate config (needs sudo)"
  sudo tee /etc/logrotate.d/windspeed >/dev/null <<EOF
$LOG_FILE {
    daily
    size 10M
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su $(id -un) $(id -gn)
}
EOF
else
  echo "==> Skipping logrotate (not available); watch the size of $LOG_FILE"
fi

# --- cron ------------------------------------------------------------------
# One entry per station. Overlap is safe: windguru.py takes a per-station
# lockfile and a run that arrives while that station is still being crawled
# logs a warning and exits 0, so cron records no failure for it. This is what
# keeps a sleeping host from stacking up every missed tick at once on wake.
#
# Only stdout is redirected, NOT stderr -- `>> $LOG_FILE` and no `2>&1`. cron
# mails whatever a job writes to stderr, and on app-btlg-civ-01 that mail is
# the alerting channel: btlg.yml calls common_mta load-bearing because these
# workloads have no HTTP surface to probe. Adding `2>&1` here would send the
# stale-station alert to a logfile nobody reads. The split is:
#   stdout -> $LOG_FILE : routine runs, skipped beats, transient crawl errors
#   stderr -> root mail : no/unknown station, and a station stale for 24h
if [ "$INSTALL_CRON" = "1" ]; then
  ADDED=0
  CRONTAB="$(crontab -l 2>/dev/null || true)"
  for entry in "${SCHEDULES[@]}"; do
    schedule="${entry%%:*}"
    station="${entry#*:}"
    line="$schedule * * * * $REPO_DIR/windguru.sh $station >> $LOG_FILE"
    # Trailing space is load-bearing: without it "rohrspitz" matches the
    # "rohrspitz-zamg" entry and that station never gets installed.
    if printf '%s\n' "$CRONTAB" | grep -qF "$REPO_DIR/windguru.sh $station "; then
      echo "==> Cron entry for $station already present, leaving it alone"
    else
      echo "==> Adding cron entry for $station ($schedule)"
      CRONTAB="$(printf '%s\n%s' "$CRONTAB" "$line")"
      ADDED=1
    fi
  done
  if [ "$ADDED" = "1" ]; then
    printf '%s\n' "$CRONTAB" | grep -v '^$' | crontab -
  fi
fi

echo
echo "Done."
if [ "${NEEDS_ENV:-0}" = "1" ]; then
  echo "NEXT: fill in $REPO_DIR/.env (one WINDSPEED_PASS_<STATION> per station),"
  echo "      then run ./windguru.sh rohrspitz once by hand to verify."
else
  echo "NEXT: ./windguru.sh rohrspitz   # one run by hand to verify"
fi
