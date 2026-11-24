# Installing on TrueNAS SCALE

Tested on TrueNAS SCALE 25.04 (Docker-based apps). Paths below assume a dataset at `/mnt/apps`; change them to suit.

## 1. Account and folders
Create a group and user `fanctl` (no login shell) under **Credentials**. Note the uid/gid. Then, as root:

```sh
mkdir -p /mnt/apps/fanctl/{app,config,logs,build}
cp -r fanctl/app/* /mnt/apps/fanctl/app/
cp fanctl/Dockerfile /mnt/apps/fanctl/build/
chown -R fanctl:fanctl /mnt/apps/fanctl
chmod 750 /mnt/apps/fanctl/app; chmod 770 /mnt/apps/fanctl/config /mnt/apps/fanctl/logs
```

## 2. Image
```sh
docker build -t fanctl:1 /mnt/apps/fanctl/build
```
The image is Debian slim with python3, ipmitool, smartmontools and sg3-utils. The code is bind-mounted read-only, so
updating means copying files and restarting the app; no rebuild is needed.

## 3. App
Edit [`fanctl/compose.json`](../fanctl/compose.json):
- `PUID`/`PGID` = the uid/gid from step 1, and `TZ`
- **no NVIDIA GPU?** remove `runtime` and the two `NVIDIA_*` variables

Then create it as a custom app, either in the UI (**Apps → Discover → ⋮ → Install via YAML**, pasting the same content as
YAML) or from a shell:
```sh
midclt call -j app.create "$(python3 -c 'import json;print(json.dumps({"app_name":"fanctl","custom_app":True,"custom_compose_config":json.load(open("compose.json"))}))')"
```
The container must be **privileged**: it needs `/dev/ipmi0`, raw SMART access and writable SES sysfs for the bay LEDs.

## 4. First start: dry run
The first start writes `config.json`, `layout.json`, `safety.json` and a random `edit_token` into
`/mnt/apps/fanctl/config`. Straight away:
1. Set `"dry_run": true` in `config.json`. It's reloaded within 10 s and fanctl stops writing duties.
2. Open `http://<nas>:31090`. Check that every sensor reads, and that the zone cards list the right fans.
3. Check the zone mapping on **your** board (`ipmitool raw 0x30 0x70 0x66 0x01 <zone> <pct>`: which fans move?).
4. Set floors you're comfortable with, and tune the curves on the **Fan curves** tab (the edit token is in the
   `edit_token` file). Review `safety.json` from a shell.
5. Set `"dry_run": false`.

## 5. Watchdog + email (recommended)
Copy `fanctl/host/fanctl_host.sh` and `fanctl_mailfmt.py` to a **root-only** folder outside the app dataset (so the
container can't change what root runs), e.g. `/mnt/apps/scripts` (`chmod 700`). Adjust the paths at the top of
the script, then add a cron job under **System → Advanced → Cron Jobs**: command `/mnt/apps/scripts/fanctl_host.sh`,
user root, every minute. Emails go to the `truenas_admin` user's address through TrueNAS's mail settings.

## 6. Layout
- **Sensor positions:** Dashboard → Temperature Graph → **Edit positions**, drag, Save.
- **Bay order:** **Calibrate bay order** blinks bay 0 and then bay 1, and you click where each one is; the full order
  is derived from those two clicks.
- Chassis regions, fan positions and NVMe groups are in `layout.json`.
