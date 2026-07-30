#!/bin/bash
# Wrapper for the cron entries. cd into the checkout so python-dotenv finds .env
# and station_state.json is written next to it, no matter what working directory
# cron hands us.
#
# Serialisation lives in windguru.py, not here: it takes a per-station lockfile
# and a run that arrives while the same station is still being crawled exits 0.
set -euo pipefail
cd "$(dirname "$0")"

# Keeps the fetch off the exact minute boundary, giving the upstream station
# time to publish the tick cron just woke us for.
sleep 15

exec ./.venv/bin/python ./windguru.py --station "$1"
