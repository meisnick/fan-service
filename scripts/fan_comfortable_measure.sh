#!/bin/sh
# Set user comfortable duties (z1 0x36, z0 0x54) and log RPM every 15 s for 60 s
# Run as root on the TrueNAS box: (printf '%s\n' "$SUDO_PW"; cat THIS) | ssh truenas "sudo -S -p '' sh -s"
uptime
ipmitool raw 0x30 0x70 0x66 0x01 0x01 0x36
ipmitool raw 0x30 0x70 0x66 0x01 0x00 0x54
for i in 1 2 3 4; do sleep 15; echo "t=$((i*15))s $(ipmitool sensor | awk -F'|' '/^FAN[125A] /{gsub(/ /,"",$1);gsub(/ /,"",$2);printf "%s=%s ",$1,$2}')"; done
for z in 0 1; do printf "zone$z duty: "; ipmitool raw 0x30 0x70 0x66 0x00 $z; done
ipmitool sensor | grep -E 'CPU Temp|DIMM' | cut -d'|' -f1,2
