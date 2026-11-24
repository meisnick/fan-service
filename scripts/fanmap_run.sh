#!/bin/bash
# fanmap: zone -> temperature mapping under controlled load. Log: /mnt/apps/tmp/fanmap/log.csv
# Run detached as root: systemd-run --unit fanmap --collect /bin/bash /mnt/apps/tmp/fanmap/run.sh  (~50 min, noisy)
# Guard: DIMM>=80 or CPU>=88 -> fans 100%, load off, 3 min cooldown. Exit always restores z0 84% / z1 54%.
D=/mnt/apps/tmp/fanmap; LOG=$D/log.csv; EV=$D/events.log
C=fanmap-stress
set_z(){ ipmitool raw 0x30 0x70 0x66 0x01 $1 $(printf '0x%02x' $2) >/dev/null; Z[$1]=$2; }
declare -A Z
ev(){ echo "$(date +%T) $*" | tee -a $EV; }
cleanup(){ docker rm -f $C >/dev/null 2>&1; set_z 0 84; set_z 1 54; ev "cleanup: load stopped, fans comfortable (z0 84 z1 54)"; }
trap cleanup EXIT
trap 'ev "TERM/INT received"; exit 1' TERM INT
ipmitool raw 0x30 0x45 0x01 0x01 >/dev/null   # Full mode
DISKS=$(ls /dev/sd? | xargs -n1 basename)
NV=$(for h in /sys/class/hwmon/hwmon*; do [ "$(cat $h/name)" = nvme ] && echo $h; done)
K10=$(for h in /sys/class/hwmon/hwmon*; do [ "$(cat $h/name)" = k10temp ] && echo $h; done)
hdr="ts,phase,load,z0,z1,FAN1,FAN2,FAN5,FANA,CPU,System,Peripheral,CPU_VRM,SOC_VRM,VRMABCD,VRMEFGH,DIMM_AD,DIMM_EH,GPU1_ipmi,Tctl,gpu"
for h in $NV; do hdr="$hdr,$(basename $(readlink -f $h/device))"; done
for d in $DISKS; do hdr="$hdr,$d"; done
hdr="$hdr,hdd_min,hdd_avg,hdd_max"
echo "$hdr" > $LOG
LOAD=0; PHASE=init
sample(){
  S=$(ipmitool sensor 2>/dev/null)
  g(){ echo "$S" | awk -F'|' -v n="$1" '{k=$1; gsub(/^ +| +$/,"",k)} k==n{v=$2; gsub(/ /,"",v); print v+0; exit}'; }
  row="$(date +%s),$PHASE,$LOAD,${Z[0]},${Z[1]},$(g FAN1),$(g FAN2),$(g FAN5),$(g FANA),$(g 'CPU Temp'),$(g 'System Temp'),$(g 'Peripheral Temp'),$(g 'CPU_VRM Temp'),$(g 'SOC_VRM Temp'),$(g 'VRMABCD Temp'),$(g 'VRMEFGH Temp'),$(g 'P1_DIMMA~D Temp'),$(g 'P1_DIMME~H Temp'),$(g 'GPU1 Temp')"
  row="$row,$(( $(cat $K10/temp1_input) / 1000 )),$(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader 2>/dev/null)"
  for h in $NV; do row="$row,$(( $(cat $h/temp1_input) / 1000 ))"; done
  ht=""; for d in $DISKS; do t=$(smartctl -A /dev/$d | awk '/Temperature_Celsius/{print $10;exit} /Current Drive Temperature/{print $4;exit}'); ht="$ht $t"; row="$row,$t"; done
  row="$row,$(echo $ht | tr ' ' '\n' | awk 'NR==1{mn=$1;mx=$1} {s+=$1; if($1<mn)mn=$1; if($1>mx)mx=$1} END{printf "%d,%.1f,%d",mn,s/NR,mx}')"
  echo "$row" >> $LOG
  DA=$(g 'P1_DIMMA~D Temp'); DE=$(g 'P1_DIMME~H Temp'); CT=$(g 'CPU Temp')
  if [ "${DA%.*}" -ge 80 ] || [ "${DE%.*}" -ge 80 ] || [ "${CT%.*}" -ge 88 ]; then
    ev "GUARD TRIP dimm=$DA/$DE cpu=$CT -> fans 100, load off"; set_z 0 100; set_z 1 100; docker rm -f $C >/dev/null 2>&1; LOAD=0
    PHASE=guard_cooldown; for i in $(seq 12); do sleep 15; sample_nog; done; exit 2
  fi
}
sample_nog(){ local LOADX=$LOAD; echo "$(date +%s),$PHASE,$LOAD,${Z[0]},${Z[1]},guard" >> $LOG; }
hold(){ PHASE=$1; local secs=$2; ev "phase $PHASE z0=${Z[0]} z1=${Z[1]} load=$LOAD for ${secs}s"; local end=$(( $(date +%s) + secs )); while [ $(date +%s) -lt $end ]; do sample; sleep 12; done; }
ev "start"
docker pull -q alpine:3.20 >/dev/null 2>&1
# P0 idle RPM map (short holds)
set_z 0 84
for p in 25 35 45 54 65 75 85 100; do set_z 1 $p; hold P0_z1_$p 25; done
set_z 1 54
for p in 40 50 60 70 84 100; do set_z 0 $p; hold P0_z0_$p 25; done
set_z 0 84; set_z 1 54
# start load
docker run -d --name $C --cpus 24 alpine:3.20 sh -c "apk add --no-cache stress-ng >/dev/null 2>&1 && stress-ng --cpu 16 --vm 8 --vm-bytes 3G --timeout 45m" >/dev/null
sleep 20; docker ps --filter name=$C --format '{{.Status}}' | tee -a $EV; LOAD=1
hold P1_soak 480
set_z 0 84; for p in 100 75 54 40; do set_z 1 $p; hold P2_z1_$p 300; done
set_z 1 54; for p in 100 60; do set_z 0 $p; hold P3_z0_$p 300; done
docker rm -f $C >/dev/null 2>&1; LOAD=0
set_z 0 84; set_z 1 54
hold P4_cooldown 180
ev "done"
