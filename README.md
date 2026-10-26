# NetMonitor dashboard

Web dashboard for the NetMonitor device at `http://192.168.0.246/`. It shows current status, history, DNS failures and the device's own event log.

## Run with Docker (recommended)

```bash
docker compose up -d --build           # dashboard at http://<this machine>:8080/
```

* **Image:** built from `python:slim`, which always tracks the latest Python release. `--build` pulls the newest base image each time, so rebuild to update.
* **Data:** all synced data lives in the `netmon-data` Docker volume, mounted at `/data`, and survives rebuilds and restarts. It is the only long-term copy of the history (see *Power loss*), so back it up now and then:

  ```bash
  docker run --rm --user "$(id -u):$(id -g)" -v monitor-io-ui_netmon-data:/data:ro -v "$PWD":/backup netmon-dashboard tar czf /backup/netmon-data.tgz -C /data .
  ```

* **Settings:** set these in `compose.yaml` under `environment`:
  * `NETMON_DEVICE`: device URL
  * `NETMON_MEMORY_DAYS`: days of full readings kept in memory
  * `TZ`: the device's time zone
  * To use a different host port, change the left side of `ports`. To make the dashboard local-only, use `127.0.0.1:8080:8080`.
* **Logs and status:** `docker compose logs -f` shows the logs. The container has a health check on `/api/status`.

## Run directly

```bash
python3 server.py                      # http://127.0.0.1:8080/
python3 server.py --host 0.0.0.0 --port 8080 --device http://192.168.0.246/
```

Every option can also be set as an environment variable (`NETMON_DEVICE`, `NETMON_HOST`, `NETMON_PORT`, `NETMON_CACHE`, `NETMON_MEMORY_DAYS`).
Python 3 standard library only. The page loads uPlot from jsdelivr.

## Why a server
The device's lighttpd sends no CORS headers, so a browser page on another origin cannot read the CSVs.
`server.py` mirrors the device's files into `./cache/` and serves a JSON API plus `static/`:

* Past days are downloaded once, then read from disk.
* Today's CSV and the event summary are fetched incrementally with HTTP `Range` requests.
* If the device goes offline, the dashboard keeps serving cached data and shows a banner.

| Endpoint | Purpose |
|---|---|
| `/api/status` | Latest sample, parsed `Latest_NetMonitor_Results.log`, DNS state, last-24h counters |
| `/api/series?start&end&points` | Latency and loss per target, plus DNS failure counts (raw or bucketed) |
| `/api/incidents?start&end` | Grouped incidents: internet down, gateway down, DNS, packet loss ≥30%, monitoring gaps |
| `/api/daily` | Per-day coverage, latency and loss per target, DNS and outage time |
| `/api/events?start&end&only=dns|loss&offset&limit` | Rows from `NetMonitor_Event_Summary.csv` |

`start` and `end` are Unix timestamps in seconds (default: the last 24 hours). `points` is capped at 5000 and `limit` at 1000; a value that isn't a number gets a 400 response.

## Memory use
Only the newest 90 days are kept fully in memory (`--memory-days N` to change it).

* **Older days:** each one gets a compact summary, written next to its cache file as `<name>.agg.json` (~65 KB per day). The summary holds that day's row in the daily table, 5-minute averages and the pieces of any incidents. The daily table, incident lists and long-range charts read these instead of the raw rows.
* **Zooming into older days:** when you zoom into a short stretch of older days (finer than 5-minute detail), those days' full rows are loaded on demand. The 7 most recently used days stay loaded.

With summaries, memory grows by roughly 30 MB per year of history instead of ~1.4 GB.

## Power loss on the device
When the device loses power it deletes all its files and starts logging to a new file once power is back.
The dashboard's `cache/` directory therefore becomes the long-term history, so don't delete it.

* Past days that vanish from the device stay in the cache.
* If a file comes back with a first row newer than everything cached (today's CSV or the event summary), or older than the cached copy's first row (the device clock was reset), it is treated as a restart, not a rewrite.
  The rows already held are moved to `<name>.archive` and the new file's rows are added after them.
* The outage appears as a "Monitoring gap" incident.
* Until its clock is set, the device may log to a daily CSV with the wrong date.
  Files dated after tomorrow, or before a gap of more than a year in the history (e.g. `NetMonitor_2000-01-01_*.csv`), are still cached but left out of the dashboard, and are listed in the "Ignoring" note under Current status.

## CSV format and DNS failures
Each row has `Date,Time,Timezone,IPAddress`, followed by 7-column blocks
(`Target,Transmit,Receive,LossPct,DelayMin,DelayAvg,DelayMax`).
When a hostname target can't be resolved, the device leaves out that target's block and appends a `DNS:Failure(s)` block with six empty fields:

| Columns | Meaning |
|---|---|
| 46 | normal: 6 targets |
| 32 | 3 IP targets + DNS marker (every hostname failed) |
| 39 | 4 targets + marker (two hostnames failed) |
| 46 | 5 targets + marker (one hostname failed): **same width as a normal row** |

So the parser walks the blocks instead of trusting the column count. The hostnames that failed are the hostname targets missing from the row.

Each DNS incident gets a likely cause:

* **Internet outage**: public IPs were also at 100% loss.
* **Gateway outage**: the gateway was unreachable.
* **DNS-only**: public IPs answered but name lookups failed.

The event summary file contains repeated header lines; these are skipped.

Rows appear to be stamped when a probe cycle *finishes*. A failing cycle takes about 2 minutes because of DNS timeouts. So each incident is treated as starting at the previous sample.

## License
MIT. See [LICENSE](LICENSE).
