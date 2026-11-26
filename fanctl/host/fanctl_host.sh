#!/bin/bash
# fanctl host helper - runs every minute as root from a TrueNAS cron job.
# Lives in /mnt/apps/scripts (root-only), NOT in /mnt/apps/fanctl, so the container can't change what root runs.
#  1. ZFS + lsblk JSON for the drive-bay view (the container has no zpool binary)
#  2. watchdog: fanctl heartbeat stale while the app should be running -> both fan zones 100 % via ipmitool.
#     App deliberately STOPPED -> no override, UNLESS CPU/DIMM temps get hot (thermal guard).
#  3. email any notifications fanctl queued in logs/notify.jsonl, via TrueNAS mail.send (no API key needed).
#     The queue offset only advances after a successful send, so a mail outage delays emails instead of losing them.
#
# TrueNAS runs cron jobs through sudo with log_subcmds, so EVERY program this script execs becomes a journal line
# (the journal is capped at 50 MB). The every-minute path therefore uses bash builtins only (read, EPOCHSECONDS,
# test -nt) plus flock; ZFS/lsblk refresh every 5 min, log rotation hourly. ~26 -> ~3 execs per quiet minute.
L=/mnt/apps/fanctl/logs
ST=/mnt/apps/scripts/.fanctl_host
[ -d "$ST" ] || { mkdir -p "$ST"; chmod 700 "$ST"; }
PFX="[${HOSTNAME%%.*} fanctl]"
now=${EPOCHSECONDS:-$(date +%s)}
min=$((now / 60))
read -r up _ < /proc/uptime; up=${up%.*}
# guard thresholds while the app is STOPPED (a bit under fanctl's emergency limits: CPU 90 / DIMM 82)
GUARD_CPU=88
GUARD_DIMM=80

# one run at a time; every external call below has a timeout, so a run can't hang for long
exec 9>"$ST/lock"
if ! flock -n 9; then echo "$(ts) previous run still active, skipped" >> "$ST/errors.log"; exit 0; fi

# keep the host-side logs bounded (1 MB, one old copy) - hourly is plenty
if [ $((min % 60)) -eq 0 ]; then
  for f in "$ST/watchdog.log" "$ST/errors.log"; do
    [ -f "$f" ] && [ "$(stat -c %s "$f")" -gt 1048576 ] && mv -f "$f" "$f.1"
  done
fi
ts() { printf '%(%a %b %e %H:%M:%S %Z %Y)T' -1; }   # builtin date

num() { case "$1" in ''|*[!0-9]*) echo 0 ;; *) echo "$1" ;; esac; }

mail() {  # mail "subject" "text" -> 0 sent (or no address configured), 1 failed
  local q to
  # dashboard link in emails (override in the cron job's environment if needed)
  [ -n "$FANCTL_URL" ] || export FANCTL_URL="http://$(hostname -I 2>/dev/null | awk '{print $1}'):31090/"
  if ! q=$(timeout 30 midclt call user.query '[["username","=","truenas_admin"]]' 2>/dev/null); then
    echo "$(ts) mail: user.query failed: $1" >> "$ST/errors.log"; return 1
  fi
  to=$(printf '%s' "$q" | python3 -c 'import json,sys;print((json.load(sys.stdin)[0].get("email") or ""))' 2>/dev/null)
  [ -n "$to" ] || return 0
  SUBJ="$PFX $1" TEXT="$2" TO="$to" python3 -c 'import json,os;print(json.dumps({"subject":os.environ["SUBJ"],"text":os.environ["TEXT"],"to":[os.environ["TO"]]}))' > "$ST/mail.json"
  if timeout 90 midclt call -j mail.send "$(cat "$ST/mail.json")" >/dev/null 2>&1; then
    rm -f "$ST/mail.json"; return 0
  fi
  echo "$(ts) mail.send failed (will retry): $1" >> "$ST/errors.log"
  rm -f "$ST/mail.json"; return 1
}

force100() {
  timeout 20 ipmitool raw 0x30 0x45 0x01 0x01 >/dev/null 2>&1
  timeout 20 ipmitool raw 0x30 0x70 0x66 0x01 0x00 0x64 >/dev/null 2>&1
  timeout 20 ipmitool raw 0x30 0x70 0x66 0x01 0x01 0x64 >/dev/null 2>&1
}

# 1. ZFS state for the bay view, every 5 min (zpool -j keeps leaf 'state'; -L would drop it on ZFS 2.3.0).
#    The dashboard calls it stale after 300 s; a run is ~1 s, so 5 min stays fresh. Also refreshed right after boot.
if { [ $((min % 5)) -eq 0 ] || [ ! -s "$L/zpool.json" ] || [ "$up" -lt 900 ]; } &&
   timeout 30 /usr/sbin/zpool status -j > "$L/.zpool.tmp" && timeout 30 /usr/bin/lsblk -J -o NAME,PARTUUID,SERIAL > "$L/.lsblk.tmp"; then
  chown fanctl:fanctl "$L/.zpool.tmp" "$L/.lsblk.tmp"; chmod 640 "$L/.zpool.tmp" "$L/.lsblk.tmp"
  [ -f "$L/notify.jsonl" ] && chmod 640 "$L/notify.jsonl"
  mv -f "$L/.lsblk.tmp" "$L/lsblk.json"; mv -f "$L/.zpool.tmp" "$L/zpool.json"
elif [ -e "$L/.zpool.tmp" ] || [ -e "$L/.lsblk.tmp" ]; then
  rm -f "$L/.zpool.tmp" "$L/.lsblk.tmp"
fi

# 2. watchdog (an empty/garbled heartbeat counts as infinitely old)
hb=; { read -r hb < "$L/heartbeat"; } 2>/dev/null; hb=$(num "$hb"); age=$((now - hb))
if [ "$age" -gt 120 ]; then
  state=$(timeout 30 midclt call app.query '[["name","=","fanctl"]]' 2>/dev/null | python3 -c 'import json,sys;a=json.load(sys.stdin);print(a[0]["state"] if a else "MISSING")' 2>/dev/null)
  [ -n "$state" ] || state=UNKNOWN
  if [ "$state" = "STOPPED" ]; then
    # deliberately stopped: don't override the user... unless it gets hot (then 100 % until the app runs again)
    hot=$(timeout 30 ipmitool sensor 2>/dev/null | awk -F'|' -v c="$GUARD_CPU" -v d="$GUARD_DIMM" '
      function t(s){ gsub(/^ +| +$/, "", s); return s }
      { n = t($1); v = t($2) }
      v ~ /^[0-9.]+$/ && ((n == "CPU Temp" && v + 0 >= c) || ((n == "P1_DIMMA~D Temp" || n == "P1_DIMME~H Temp") && v + 0 >= d)) { printf "%s %s C; ", n, v }')
    if [ -n "$hot" ] || [ -f "$ST/stopped_hot" ]; then
      force100
      if [ ! -f "$ST/stopped_hot" ]; then
        echo "$(ts) thermal guard: app STOPPED and hot ($hot) -> 100 %" >> "$ST/watchdog.log"
        touch "$ST/stopped_hot"
      fi
      [ -f "$ST/stopped_hot.mailed" ] || { mail "THERMAL GUARD: fanctl stopped and hot, fans at 100 %" "fanctl is STOPPED and $hot(guard CPU >= $GUARD_CPU, DIMM >= $GUARD_DIMM). The host set both fan zones to 100 % and keeps them there until the app is running again." && touch "$ST/stopped_hot.mailed"; }
    elif [ ! -f "$ST/stopped.mailed" ]; then
      mail "fanctl is STOPPED - fans static" "The fanctl app is stopped, so nothing is adjusting the fans (they stay where fanctl left them on exit, never below the floors). The host still forces 100 % if CPU >= $GUARD_CPU or a DIMM bank >= $GUARD_DIMM C. Start the app to restore control." && touch "$ST/stopped.mailed"
    fi
  else
    force100
    if [ ! -f "$ST/tripped" ]; then
      echo "$(ts) watchdog tripped: heartbeat age $age s, app $state, uptime $up s" >> "$ST/watchdog.log"
      touch "$ST/tripped"
    fi
    # right after boot the app is still starting: force 100 % quietly; email once it's past 10 min uptime
    if [ "$up" -gt 600 ] && [ ! -f "$ST/tripped.mailed" ]; then
      mail "WATCHDOG: fans forced to 100 %" "fanctl heartbeat is $age s old and the app state is $state, so the host watchdog set both fan zones to 100 %. It hands control back automatically once fanctl is running again." && touch "$ST/tripped.mailed"
    fi
  fi
else
  if [ -f "$ST/tripped" ]; then
    echo "$(ts) watchdog released" >> "$ST/watchdog.log"
    [ -f "$ST/tripped.mailed" ] && mail "Watchdog released - fanctl back in control" "fanctl heartbeat is fresh again; it has taken the fans back."
  fi
  [ -f "$ST/stopped_hot" ] && echo "$(ts) thermal guard released (app running)" >> "$ST/watchdog.log"
  rm -f "$ST/tripped" "$ST/tripped.mailed" "$ST/stopped.mailed" "$ST/stopped_hot" "$ST/stopped_hot.mailed"
fi

# 3. queued notifications -> one email per run
Q="$L/notify.jsonl"
# skip unless the queue changed since everything was last sent (builtin -nt; the marker is only touched when caught up);
# a full check hourly anyway, in case an append raced the marker
if [ -f "$Q" ] && { [ "$Q" -nt "$ST/notify.seen" ] || [ ! -e "$ST/notify.seen" ] || [ $((min % 60)) -eq 0 ]; }; then
  size=$(stat -c %s "$Q"); off=; { read -r off < "$ST/notify.offset"; } 2>/dev/null; off=$(num "$off")
  [ "$size" -lt "$off" ] && off=0
  if [ "$size" -gt "$off" ]; then
    tail -c +$((off + 1)) "$Q" | head -c $((size - off)) > "$ST/new.jsonl"
    python3 /mnt/apps/scripts/fanctl_mailfmt.py "$ST/new.jsonl" > "$ST/mail.parts"
    if [ ! -s "$ST/mail.parts" ] || mail "$(head -1 "$ST/mail.parts")" "$(tail -n +2 "$ST/mail.parts")"; then
      echo "$size" > "$ST/notify.offset"; off=$size
    fi
    rm -f "$ST/new.jsonl" "$ST/mail.parts"
  fi
  # rotate only when everything is sent; fanctl opens the file per write, so it simply re-creates it.
  # Anything appended between the size check and the move is copied back, not lost.
  if [ "$off" -gt 1048576 ] && [ "$(stat -c %s "$Q")" -eq "$off" ]; then
    mv -f "$Q" "$Q.1"
    extra=$(( $(stat -c %s "$Q.1") - off ))
    [ "$extra" -gt 0 ] && tail -c "$extra" "$Q.1" >> "$Q" && chown fanctl:fanctl "$Q"
    echo 0 > "$ST/notify.offset"; off=0
  fi
  [ "$off" -ge "$(stat -c %s "$Q" 2>/dev/null || echo 0)" ] && : > "$ST/notify.seen"
fi
