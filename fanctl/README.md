# fanctl: zoned fan controller + thermal dashboard

Runs on NAS as the TrueNAS custom app **`fanctl`**. Dashboard: **http://<nas>:31090** (follows the OS light/dark theme).

## What it does
- Every 10 s it reads all BMC sensors (`ipmitool sensor`), CPU Tctl and the 6 NVMe drives (hwmon). Every 60 s **a separate thread** reads the 24 HDDs (`smartctl -n standby`, which never wakes a sleeping disk), SES and BMC platform info, so a hung smartctl/sg_ses can never delay the fan loop. If that scan stalls for more than 10 min, an alert is raised. A drive whose SMART read fails shows as **error**, not standby; standby is only reported when smartctl says STANDBY/SLEEP.
- It drives each zone to the **max** demand of its curves: up immediately, down at most 5 %/min (the step is capped even after failed reads), with 3 °C hysteresis (not applied below a curve's first point, so a zone can't stick above its floor). It never goes below the floor (your old static "comfortable" setting). A "hysteresis hold" reason names the sensor that's holding it.
- Every duty write checks ipmitool's exit code. A failed write is logged, emailed and retried on the next loop, and the duty isn't recorded as applied. Every 60 s the duty is also read back from the BMC and re-set on any mismatch.
  - Zone 0 (CPU/rear: FAN1 and FAN2 = NH-U9 NF-A9s, FAN5 = 2x rear NF-A8): CPU, DIMM E–H, VRM EFGH, GPU.
  - Zone 1 (fan wall: FANA = 3x NF-F12 iPPC-3000 on a splitter): DIMM A–D, Peripheral, HDD max, NVMe max, HBA, SES. (The DIMM E–H "assist" curve was removed in v2: the fan wall warms bank E–H. See docs/FANS.md.)
- It re-asserts fan mode Full and the duties every 60 s, so a BMC reset can't leave the fans at the wrong setting.
- **Failsafe:** a required sensor missing, 3 failed reads in a row, or a crash → both zones 100 %. A graceful stop (app stop/update) leaves each zone at **max(floor, current duty)**, so a stop during an emergency or a hot spell never lowers the fans.
- Alert banner + log line if a DIMM bank holds ≥ 80 °C for 2 minutes.
- **Drive bays** are drawn as the CSE-846 front: 6 rows x 4 columns of horizontal carriers between the rack ears, each with a blue "present" LED and a red status LED (solid = fault, blinking = identify). Slots come from the SES backplane (LSI SAS3x40, `/sys/class/enclosure/19:0:24:0`).
  - **Bay order (verified 2026-11-24 against the drive serial labels):** the CSE-846 / SAS3x40 numbers slots **column-major from the bottom-left, counting upward**: slot 0 bottom-left, 5 top-left, 6 bottom of column 2 … 23 top-right. That's the default in code and in `layout.json`. **Calibrate bay order** (2 clicks with the locate LED) is there if the backplane ever changes.
  - **Health per drive follows SMART's own verdict, plus ZFS and temperature.** **fail** = SMART overall FAILED, a SMART attribute below threshold *now*, ZFS FAULTED/UNAVAIL/REMOVED/OFFLINE, or temp ≥ 55 °C. **warn** = an attribute that failed in the *past*, ZFS read/write/checksum errors, or temp ≥ 45 °C. Raw counters (reallocated, pending, CRC, grown defects, uncorrected errors, power-on hours) appear in the details panel as **information only**. To warn on one anyway, add e.g. `"pending_warn": 1` to `config.json` `health`. A standby disk is never woken; its last SMART read is kept.
  - **Fault LED:** with `auto_fault_led` on (default), failed drives get their SES fault LED lit, and it's cleared when they recover. A pool member that **disappears** is traced to its last-known bay (`slotmap.json` keeps slot→dev→serial and partuuid→dev) and that empty bay is flagged MISSING with the fault LED lit. Click a bay for SMART details, **Identify** (blink) and **Fault LED On / Off / Auto** (manual overrides are kept in `leds.json`).
  - ZFS state comes from **a TrueNAS cron job** ("fanctl: zpool status JSON…", root, runs every minute, refreshes ZFS every 5 min). It writes `logs/zpool.json` (`zpool status -j`) and `logs/lsblk.json` (partuuid→disk); the container has no zpool binary. Note: on ZFS 2.3.0, `-L` drops the leaf `state` field, so don't add it. The page warns if the file is more than 5 minutes old.
- The heat map is iLO-style: IDW-interpolated temperatures over a top-down plan of the chassis, traced from photos, in 2D or a rotatable 3D surface. **Edit positions** (2D) lets you drag sensors and save to `layout.json`.

## Fan curves tab (`#curves`) and safety
- **Editor:** one chart per curve (temperature 20–100 °C against duty). It shows the zone floor (shaded), the "must reach 100 % by" cap (red dashed), the emergency temperature (red, EMERG), and a live marker with the current temperature and the duty it asks for. Drag points, double-click to add, right-click to delete, or type values. Change a curve's sensor, add or remove curves, mark optional, set floors, hysteresis and ramp-down. Zone cards stay visible on this tab.
- **Validate & preview** shows errors and warnings and the duty each zone would run at right now. **Apply** re-validates, asks for confirmation and needs the **edit token** (keep a copy in your password manager; source of truth `/mnt/apps/fanctl/config/edit_token`. To rotate it, delete that file, restart the app and update your copy; remembered in the browser's localStorage). Each apply writes `config/backups/config-<ts>.json` (last 20 kept) and an audit line in fanctl.log. **Revert** loads a backup into the editor, and you apply it like any change. **Export** downloads the running config; **Import** loads a JSON file into the editor (validated, nothing changes until Apply). Only zones/curves/floors/hysteresis/ramp are taken from an import: intervals, paths and failsafe duty can't be changed this way.
- **Safety (`config/safety.json`, shell-only):**
  - floors ≥ 30 % (stall-safe)
  - points strictly increasing in temperature, duty never decreasing, last point 100 %
  - required inputs can't be removed or made optional (zone 0: CPU Temp, DIMM E–H; zone 1: DIMM A–D)
  - per-sensor caps by which a curve must reach 100 % (CPU 90, DIMMs 80, HDD 55, NVMe 78, …)
  - hysteresis 1–8 °C, ramp-down 1–30 %/min
- The same validation runs on **hand edits of config.json** (plus bounds on runtime keys: `failsafe_duty` must be 100, zone `max` 100, `interval_s` 2–30, `read_fail_limit` 1–10, `reassert_s` 10–300, `hdd_interval_s` 30–3600). An invalid edit is rejected, logged and emailed, and the old config keeps running. An invalid or unparseable config.json at **startup** runs the built-in curves and emails. A required input can never be marked optional. Points are rounded before they're checked. A partial safety.json is deep-merged with the defaults, so it can't drop a limit.
- **Emergency override** (independent of the curves): CPU ≥ 90, DIMM ≥ 82, HDD ≥ 55, NVMe ≥ 80 °C, or any BMC temperature in critical state → all zones 100 % until **that sensor** is 5 °C below its own limit. Each sensor is tracked separately, and a missing sample never releases it. Logged; the zone reason shows EMERGENCY.

## Watchdog + email notifications (host side)
- **`host/fanctl_host.sh`** is installed as **`/mnt/apps/scripts/fanctl_host.sh`** (root:root 700, outside the app folder so the container can't change what root runs) together with `fanctl_mailfmt.py`. TrueNAS **cron job #1** (root, every minute) runs it. It:
  1. dumps `zpool status -j` + `lsblk` JSON for the bay view, every 5 minutes (and every run for the first 15 min after boot)
  2. **watchdog:** fanctl writes `logs/heartbeat` every loop. If it's more than 120 s old (or empty or garbled) and the app isn't deliberately STOPPED, the script sets fan mode Full and both zones to 100 % via ipmitool, every minute until the heartbeat is fresh again, and emails (quietly for the first 10 min after boot). A deliberate app stop gets one "fanctl is STOPPED" email, plus a **thermal guard**: while STOPPED, CPU ≥ 88 or a DIMM bank ≥ 80 °C forces both zones to 100 % until the app runs again.
  - One run at a time (`flock`). Every midclt, zpool and ipmitool call has a timeout.
  - **Kept cheap on purpose:** TrueNAS runs cron jobs through sudo with `log_subcmds`, so every program the script starts becomes a journal line, and the journal is capped at 50 MB. A quiet minute uses only bash builtins plus `flock` (about 5 journal lines, down from 26); the ZFS refresh and log rotation run less often, and the notification queue is only read when it changed.
  3. **email:** fanctl queues events in `logs/notify.jsonl` (emergency on/off, failsafe on/off, DIMM/BMC-threshold alerts, a drive or NVMe going to/from fail, config rejected, duty write failing; same event at most every 30 min). The script batches new lines into one email per minute via TrueNAS `mail.send` to truenas_admin's address. The read offset only advances after a successful send, so a mail outage delays emails instead of losing them. The queue is rotated (moved to `.1`) only once everything in it has been sent. **No API key in the container.**
  - State/logs: `/mnt/apps/scripts/.fanctl_host/` (`watchdog.log`, `errors.log`, `notify.offset`).
  - Tested 2026-11-24: test email sent; a faked stale heartbeat tripped the watchdog (both zones went to 0x64) and it released on the next run; fanctl reasserted 84/54 within 60 s; 3 mail.send jobs SUCCESS.
- **LED / layout endpoints need the edit token too** (`/api/locate`, `/api/fault`, `/api/layout`); the browser prompts once and remembers it.

## Layout on the box
| Path | Owner | Mounted as | Contents |
|---|---|---|---|
| `/mnt/apps/fanctl/app` | fanctl:fanctl 750 | `/app` (ro) | `fanctl.py`, `static/index.html` |
| `/mnt/apps/fanctl/config` | fanctl:fanctl 770 | `/config` | `config.json` (curves, floors, health thresholds; hot-reloaded), `layout.json` (positions, bay order), `slotmap.json`, `leds.json` |
| `/mnt/apps/fanctl/logs` | fanctl:fanctl 770 | `/logs` | `fanctl.log` (events incl. LED changes, rotated 5 MB x5), `history/YYYY-MM-DD.jsonl` (every sample, 14 days), `zpool.json` + `lsblk.json` (from cron) |
| `/mnt/apps/fanctl/build/Dockerfile` | | | image `fanctl:1` (debian bookworm-slim + python3, ipmitool, smartmontools, sg3-utils; nvidia-smi via the NVIDIA runtime) |

Account: user **fanctl uid 1004**, group **fanctl gid 3005** (db ids user via midclt / group 119), nologin, home `/mnt/apps/fanctl`. The container runs **root + privileged**: it needs `/dev/ipmi0`, raw SMART and writable SES sysfs (same reason as Scrutiny). It runs with `umask 007` and chowns every file it creates to PUID/PGID 1004/3005.

## Operate
- Tune curves: edit `/mnt/apps/fanctl/config/config.json`. It's reloaded within 10 s, no restart needed. `"dry_run": true` computes but doesn't write duties.
- Update code: copy `app/` to `/mnt/apps/fanctl/app/`. HTML changes are live on reload; `fanctl.py` changes need `midclt call -j app.redeploy fanctl` (or Apps → fanctl → Restart).
- Rebuild image: `docker build -t fanctl:1 /mnt/apps/fanctl/build`, then redeploy.
- Compose used for `app.create`: [compose.json](compose.json) (`custom_app: true`, `pull_policy: never` for the local image). `x-portals` was ignored by 25.04, so there's no Web UI button.
- Stop the controller: Apps → fanctl → Stop. The zones stay at max(floor, current duty), normally 84 / 54. The host thermal guard still forces 100 % if it gets hot.
- Local UI dev: `python fanctl.py --mock --data ./mockdata --port 18090`.

## Logs, retention, storage
**History database:** `logs/history.db` (SQLite, WAL, `app/histdb.py`). Series table + three tiers:

| Tier | Resolution | Kept | Content |
|---|---|---|---|
| `raw` | every 10 s sample | 7 days | all BMC sensors (temps, fans, volts), Tctl, GPU, hba/ses/hdd/nvme maxima, zone duties |
| `m1` | 1-min avg / min / max | 30 days | rolled up from raw every minute (incremental, idempotent) |
| `m15` | 15-min avg / min / max | 2 years | rolled up from m1 |

Per-drive temperatures are stored **by serial** (`hdd:<serial>`, `nvme:<serial>`, `hba:<pci>`) once a minute, so long-term drive trends survive sdX renames. Hourly: retention deletes + `incremental_vacuum` + WAL checkpoint. Expected steady state ≈ 200 MB. On first start the JSONL day files are imported. `/api/history?minutes=N&series=a,b` picks the tier automatically (≤ 2 d raw, ≤ 30 d m1, else m15), returns ~720 points with avg (`s`) and **peak** (`x`) per bucket; the chart draws faint dashed peak lines so short spikes stay visible in long ranges. `/api/history/series` lists the series plus DB stats.

Flat files alongside:
| File | Bound |
|---|---|
| `logs/fanctl.log` | rotates at 5 MB, 5 kept (≤ 30 MB) |
| `logs/history/YYYY-MM-DD.jsonl` | plain-text audit copy of every sample; `history_days` (14) kept; ≈ 4.6 MB/day |
| `logs/notify.jsonl` | host script rotates to `.1` after 1 MB, once everything is sent |
| container stdout (Docker json-file) | capped in compose: 5 MB x 3 |
| `config/backups/` | last 20 configs |
| `config/*.json` state | atomic tmp + rename writes |
| `/mnt/apps/scripts/.fanctl_host/*.log` | rotated at 1 MB, one old copy |

The `apps` dataset is snapshotted daily (kept 2 weeks). Container RSS ~25 MB (the history lives in SQLite, not RAM).

## API
`GET /api/state`, `GET /api/history?minutes=N`, `GET /api/config` (config + safety + inputs + live values + demand + backups), `POST /api/config/validate|apply|revert` (apply/revert need `token`), `POST /api/locate {slot,on}`, `POST /api/fault {slot,mode:on|off|auto}`, `POST /api/layout {layout}`. Reads need no auth (LAN only). Every mutating endpoint needs the edit token (constant-time compare; an empty or short token file locks them all). POST bodies are capped at 1 MB and must be JSON objects. All server-supplied text is HTML-escaped in the UI.
