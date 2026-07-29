#!/bin/bash
# Poll one station by hand, out of the checkout's own virtualenv:
#
#   ./windguru.sh rohrspitz
#
# The scheduled path does NOT go through this script any more -- systemd runs
# windguru.py directly (systemd/windspeed@.service). This is the manual/debug
# entry point, so the `sleep 15` that used to be here is gone: it existed to
# stagger cron runs off the exact minute, and the timers' RandomizedDelaySec
# does that properly now.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python windguru.py --station "$1"
