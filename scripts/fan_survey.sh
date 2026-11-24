#!/bin/sh
# Fan mode, zone duties, fan/temp sensors + thresholds, HDD/NVMe/GPU/HBA temps
# Run as root on the TrueNAS box: (printf '%s\n' "$SUDO_PW"; cat THIS) | ssh truenas "sudo -S -p '' sh -s"
echo "=== fan mode (00 std,01 full,02 optimal,04 heavyIO) ==="; ipmitool raw 0x30 0x45 0x00
echo "=== zone duties (hex %) ==="; for z in 0 1; do printf "zone$z: "; ipmitool raw 0x30 0x70 0x66 0x00 $z; done
echo "=== fans + thresholds ==="; ipmitool sensor | grep -iE '^FAN'
echo "=== temps + thresholds ==="; ipmitool sensor | grep -iE 'Temp'
echo "=== HDD temps ==="; for d in /dev/sd?; do t=$(smartctl -A $d | awk '/Temperature_Celsius/{print $10} /Current Drive Temperature/{print $4}'); printf "%s:%s " $(basename $d) "$t"; done; echo
echo "=== NVMe temps ==="; for d in /dev/nvme?; do printf "%s:%s " $(basename $d) "$(smartctl -A $d | awk '/^Temperature:/{print $2}')"; done; echo
echo "=== GPU ==="; nvidia-smi --query-gpu=temperature.gpu,fan.speed,power.draw --format=csv,noheader
echo "=== HBA temps ==="; for c in 0 1 2; do storcli64 /c$c show temperature 2>/dev/null | grep -i temp; done; ls /sys/class/hwmon/*/name | xargs -I{} sh -c 'echo "{}: $(cat {})"' | head -20
echo "=== chassis ==="; dmidecode -t chassis | grep -E 'Manufacturer|Type|Version'; dmidecode -t system | grep -E 'Product|Manufacturer'
echo "=== smfc present? ==="; ls /mnt/apps/scripts 2>&1 | head; docker images --format '{{.Repository}}:{{.Tag}}' | grep -i smfc
