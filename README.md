# Windspeeds

this python script crawls websites and pushes them to windguru.cz.
The documentation of the upload API is available at <https://stations.windguru.cz/upload_api.php>

## install

`install.sh` is the install contract on app-btlg-civ-01, where this repo is a
hand-managed payload and Ansible only owns the VM base (see NamcheAI/infra
`ansible/playbooks/btlg.yml`). It is idempotent -- re-run it after a `git pull`
to pick up new dependencies.

```bash
./install.sh            # venv + deps + logrotate + cron
./install.sh --no-cron  # everything except the crontab entries
```

It creates the virtualenv, installs `requirements.txt`, seeds a placeholder
`.env` if there is none (never overwriting an existing one), installs a
logrotate drop-in for `windspeed.log`, and adds one cron entry per station.

Fill in `.env` with the per-station windguru upload passwords -- one
`WINDSPEED_PASS_<STATION>` each, see `.env.example` -- then verify with a single
manual run:

```bash
./windguru.sh rohrspitz
```

## dependencies

Every pin in `requirements.txt` is a pure-python wheel. app-btlg-civ-01
deliberately ships no compiler and no `-dev` headers, so a dependency without a
wheel for the host interpreter cannot be installed there at all. Check that
before adding one.

## concurrency

`windguru.py` takes a lockfile before it does any work, **one per station**
(`/tmp/windguru-<station>.lock`, override with `WINDSPEED_LOCK_TEMPLATE`). A run
that arrives while the same station is still being crawled prints a warning and
exits 0, so cron records no failure for a skipped beat.

The lock is per station and not global on purpose: the stations are independent
and a slow crawl of one must not make the others skip. It exists mainly for the
sleeping-host case -- cron releases every missed tick at once on wake, which on
the sibling `bees` workload produced 27 invocations within 3 seconds.

Note that the lock does **not** serialise *different* stations, and the
schedules align: at :00 and :30 all seven fire together. On a single-core host,
offset the cron minutes instead -- see the comment above `SCHEDULES` in
`install.sh` for a set of offsets that leaves each station's cadence unchanged.

## logs

`windspeed.log` is rotated by the drop-in `install.sh` writes to
`/etc/logrotate.d/windspeed`: daily, or sooner if it passes 10M, keeping 7
compressed generations. It uses `copytruncate` because the cron entries hold the
file open through `>>` for the life of each run.

## crontab

`install.sh` writes these; they are listed here for reference.

```bash
*/2  * * * * /home/admin/sandbox/windspeed/windguru.sh kressbronn       >> /home/admin/sandbox/windspeed/windspeed.log 2>&1
*/5  * * * * /home/admin/sandbox/windspeed/windguru.sh lindau-lsc       >> /home/admin/sandbox/windspeed/windspeed.log 2>&1
*/5  * * * * /home/admin/sandbox/windspeed/windguru.sh rohrspitz        >> /home/admin/sandbox/windspeed/windspeed.log 2>&1
*/10 * * * * /home/admin/sandbox/windspeed/windguru.sh altenrhein       >> /home/admin/sandbox/windspeed/windspeed.log 2>&1
*/10 * * * * /home/admin/sandbox/windspeed/windguru.sh rohrspitz-zamg   >> /home/admin/sandbox/windspeed/windspeed.log 2>&1
*/10 * * * * /home/admin/sandbox/windspeed/windguru.sh praia-bela-vista >> /home/admin/sandbox/windspeed/windspeed.log 2>&1
*/15 * * * * /home/admin/sandbox/windspeed/windguru.sh praia-da-rainha  >> /home/admin/sandbox/windspeed/windspeed.log 2>&1
```

## manual virtual environment

`install.sh` does this for you; by hand it is:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```
