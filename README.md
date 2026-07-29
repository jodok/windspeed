# Windspeeds

This Python script crawls weather-station websites and uploads the readings to
windguru.cz. The upload API is documented at
<https://stations.windguru.cz/upload_api.php>.

It runs on **`app-btlg-civ-01`**, a VM in the CampusVäre Proxmox estate, out of
a plain git checkout under `~/sandbox/windspeed`, driven by systemd timers. It
moved there from a crontab on Jodok's Mac on 2026-07-30 — see
[Migration](#migration-from-the-mac) for the cutover, and
`NamcheAI/infra`'s `ansible/playbooks/btlg.yml` for how the host itself is
provisioned.

## Stations

| Station            | Cadence | Upstream                                    |
| ------------------ | ------- | ------------------------------------------- |
| `kressbronn`       | 2 min   | wetter-kressbronn.de (HTML table)           |
| `lindau-lsc`       | 5 min   | meteo-services.com gateway                  |
| `rohrspitz`        | 5 min   | meteobridge `livedataxml.cgi`               |
| `altenrhein`       | 10 min  | MeteoSwiss (6 measurement tables per run)   |
| `rohrspitz-zamg`   | 10 min  | GeoSphere `tawes-v1-10min`                  |
| `praia-bela-vista` | 10 min  | iKitesurf / WeatherFlow widget              |
| `praia-da-rainha`  | 15 min  | IPMA hourly observations                    |

A station's cadence lives in its timer (`systemd/windspeed@<station>.timer`) and
nowhere else. Adding a station means a `stations` entry in `windguru.py`, a
timer file, a password in the `.env`, and a re-run of `./install.sh`.

Note that `interval` in the `stations` dict is the value reported *to* windguru,
not the poll cadence — the two deliberately differ for several stations.

## Install

On a Linux host, as the user the pollers should run as:

```bash
git clone https://github.com/jodok/windspeed.git ~/sandbox/windspeed
```

```bash
cd ~/sandbox/windspeed && ./install.sh
```

`install.sh` creates the virtualenv, installs the requirements, migrates any
legacy state file, then templates the units into `/etc/systemd/system` and
enables the timers. It reads the run user and the checkout path from its own
environment rather than taking configuration, and it is idempotent — re-run it
after a `git pull` to pick up a changed unit or cadence.

To take it all back out again (the checkout, `.venv` and `state/` are left
alone):

```bash
./install.sh --uninstall
```

## Secrets

Each station's upload password goes in a `.env` file in the checkout; see
`.env.example` for the shape. The seven values live in 1Password, and the file
is deliberately *not* created by `install.sh` — nothing in this repo should be
able to fetch or write them.

Without a `.env` the crawl still runs and the upload fails at the hash step, so
a missing file shows up as every station failing at once.

## Operating

```bash
systemctl list-timers 'windspeed*'
```

| Task                          | Command                                              |
| ----------------------------- | ---------------------------------------------------- |
| Poll one station now          | `systemctl start windspeed@rohrspitz`                |
| Follow one station's log      | `journalctl -fu windspeed@rohrspitz`                 |
| Errors across all stations    | `journalctl -u 'windspeed@*' -p warning --since -1d` |
| Check freshness now           | `systemctl start windspeed-stale`                    |
| Pause one station             | `systemctl disable --now windspeed@kressbronn.timer` |

Run a station by hand, outside systemd, with `./windguru.sh <station>`.

### State

Each station's last successful upload time is a separate file under `state/`,
written atomically. It used to be a single `station_state.json` that every run
rewrote whole, which two concurrent stations could interleave — the three
stations on a 10-minute cadence fired simultaneously under cron, so the race was
reachable in normal operation. One file per station removes it by construction.

`WINDSPEED_STATE_DIR` overrides the location; the units set it explicitly.

### Alerting

The contract is that **a failed poll is not an alert and staleness is**:

- A poll that fails — upstream down, parse error, refused connection — logs to
  the journal and exits 0. Upstreams are down for hours at a time and the next
  attempt is 2–15 minutes away.
- `windspeed-stale.timer` asks once a day whether any station has gone 24 hours
  without a successful upload. If so `windguru.py --check-stale` exits non-zero,
  and the `OnFailure=` handler (`windspeed-mail@.service`) mails root the unit's
  journal through the host's exim.

This is a change from the Mac, where `check_stale_updates()` ran at the top of
*every* poll and cron mailed on any output at all — one dead station generated
roughly 700 messages and log lines a day, which is most of what the old 35 MB
`windspeed.log` contained.

Mail delivery depends on the local MTA that `infra`'s `common_mta` role
installs, with `root` aliased to a real mailbox. On a host without it the
pollers work and the freshness alert goes nowhere.

## Migration from the Mac

The cutover, in order. The old and new hosts can both run for a while — windguru
takes whichever upload arrives, and both send the same readings — so there is no
hard switchover moment.

1. **Provision the VM.** Merge the `infra` change that adds `app-btlg-civ-01`
   to `civ_vms` and `ansible/playbooks/btlg.yml`; the pipeline applies on merge
   and the host comes up on the tailnet with the base roles and an MTA.
2. **Check in.**
   ```bash
   ssh ansible@app-btlg-civ-01.khumbu.namche.net
   ```
3. **Clone and install** as above. Expect a warning about the missing `.env`.
4. **Copy the secrets** into `~/sandbox/windspeed/.env` from 1Password — seven
   `WINDSPEED_PASS_*` values.
5. **Carry the state over**, so the freshness check does not report every
   station stale on day one:
   ```bash
   scp ~/sandbox/windspeed/station_state.json ansible@app-btlg-civ-01.khumbu.namche.net:sandbox/windspeed/
   ```
   Re-run `./install.sh` on the VM to split it into `state/<station>.json`.
6. **Verify** one station end to end, then the freshness check:
   ```bash
   systemctl start windspeed@rohrspitz && journalctl -u windspeed@rohrspitz -n 20
   ```
7. **Stop the Mac's crontab** once the VM has been uploading cleanly for a
   cycle. Remove the seven `windguru.sh` lines with `crontab -e`, leaving the
   unrelated `bees.sh` line in place.
8. **Add the uptime probe.** Once the VM has enrolled, get its tailnet address
   and add `ansible/monitors.d/btlg.yml` to `infra` so the estate watches that
   the host answers:
   ```yaml
   ---
   monitors:
     - name: app-btlg-civ-01
       module: tcp_connect
       target: <tailnet-ip>:22
       labels:
         service: app-btlg-civ-01
         product: windspeed
         audience: private
         env: production
   ```

### Rolling back

Re-enable the crontab lines on the Mac and `./install.sh --uninstall` on the
VM. Nothing in the migration is one-way: the state files are derived, the
secrets exist in 1Password, and both hosts upload to the same place.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python windguru.py --station rohrspitz
```

`crawl_data()` is importable and does no uploading, which is the quickest way to
check a parser against a live upstream after a site changes its markup.
