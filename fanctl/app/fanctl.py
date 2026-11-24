#!/usr/bin/env python3
"""fanctl - zoned fan controller + thermal dashboard for the Supermicro H12SSL-i TrueNAS box.

Zone 0 = CPU/rear (FAN1, FAN2, FAN5), zone 1 = fan wall (FANA).
Reads BMC sensors (ipmitool), HDD temps (smartctl, never wakes a sleeping disk), NVMe + Tctl (hwmon),
maps drives to backplane slots via SES (/sys/class/enclosure), and drives each zone to the max demand
of its curves. Failsafe: sensor read failure / missing required sensor / crash -> 100%.
Graceful stop (SIGTERM) -> zone floors (the old static "comfortable" setting).

Stdlib only. Run:  python3 fanctl.py --data /data [--mock] [--port 8090]
"""
import argparse, collections, glob, hmac, json, math, os, random, re, signal, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import histdb                # SQLite history store (raw / 1 min / 15 min rollups)
try:
    import mpt_temp          # LSI HBA chip temperatures via the mpt3sas ioctl (read-only)
except ImportError:          # pragma: no cover
    mpt_temp = None

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

DEFAULT_CONFIG = {
    "interval_s": 10,
    "hdd_interval_s": 60,
    "dry_run": False,
    "hysteresis_c": 3,
    "ramp_down_pct_per_min": 5,
    "failsafe_duty": 100,
    "read_fail_limit": 3,
    "reassert_s": 60,
    "history_days": 14,
    "enclosure_match": "SAS3x40",
    "zpool_json": "/logs/zpool.json",          # written every minute by a TrueNAS cron job (zpool status -jLP)
    "auto_fault_led": True,                    # light the SES fault LED on failed / missing drives
    # Drive health follows SMART's own verdict (overall status + per-attribute threshold failures).
    # Raw counters (reallocated, pending, CRC, grown defects...) are informational; set e.g. "pending_warn": 1 to warn on one.
    "health": {"temp_warn": 45, "temp_fail": 55, "zfs_errors_warn": 1},
    "alert": {"dimm_c": 80, "hold_s": 120},
    "zones": {
        "0": {"name": "CPU / rear", "fans": ["FAN1", "FAN2", "FAN5"], "floor": 84, "max": 100,
              "curves": [
                  # zone 0 at 84 % is already ~85 % of its max rpm and only ~2 °C from its ceiling: get to 100 % early
                  {"sensor": "CPU Temp", "points": [[76, 84], [80, 92], [84, 100]]},
                  {"sensor": "P1_DIMME~H Temp", "points": [[65, 84], [68, 92], [72, 100]]},
                  {"sensor": "VRMEFGH Temp", "points": [[70, 84], [80, 100]]},
                  {"sensor": "GPU1 Temp", "points": [[75, 84], [85, 100]], "optional": True}]},
        "1": {"name": "Fan wall", "fans": ["FANA"], "floor": 54, "max": 100,
              "curves": [
                  {"sensor": "P1_DIMMA~D Temp", "points": [[62, 54], [65, 65], [68, 80], [72, 100]]},
                  {"sensor": "Peripheral Temp", "points": [[48, 54], [52, 75], [56, 100]]},   # soaks at 44-45 °C
                  {"sensor": "hdd_max", "points": [[40, 54], [45, 75], [50, 100]], "optional": True},
                  {"sensor": "nvme_max", "points": [[60, 54], [70, 100]], "optional": True},
                  {"sensor": "hba_max", "points": [[80, 54], [88, 75], [95, 100]], "optional": True},
                  {"sensor": "ses_max", "points": [[75, 54], [90, 100]], "optional": True}]},
                  # no DIMM E-H "assist" here: the 11/24 map showed fan wall 54->100 % WARMS bank E-H by ~2 °C
    },
}

# Safety layer: NOT editable from the web UI (edit /config/safety.json from a shell). Every config change - UI, import
# or a hand-edited config.json - is validated against it, and the emergency override runs regardless of the curves.
DEFAULT_SAFETY = {
    "min_floor": 30,                     # stall-safe: zone 1 still spun 700 rpm at 25 % (11/24 map)
    "required": {"0": ["CPU Temp", "P1_DIMME~H Temp"], "1": ["P1_DIMMA~D Temp"]},
    "full_by": {"CPU Temp": 90, "Tctl": 92, "P1_DIMMA~D Temp": 80, "P1_DIMME~H Temp": 80, "VRMABCD Temp": 95,
                "VRMEFGH Temp": 95, "CPU_VRM Temp": 95, "SOC_VRM Temp": 95, "Peripheral Temp": 70, "System Temp": 60,
                "hdd_max": 55, "nvme_max": 78, "hba_max": 100, "ses_max": 95, "GPU1 Temp": 92, "gpu_temp": 92},
    "emergency": {"CPU Temp": 90, "P1_DIMMA~D Temp": 82, "P1_DIMME~H Temp": 82, "hdd_max": 55, "nvme_max": 80},
    "emergency_release_c": 5,
    "emergency_on_bmc_critical": True,
    "hysteresis_c": [1, 8],
    "ramp_down_pct_per_min": [1, 30],
}

CURVE_INPUTS = ["CPU Temp", "Tctl", "P1_DIMMA~D Temp", "P1_DIMME~H Temp", "VRMABCD Temp", "VRMEFGH Temp", "CPU_VRM Temp",
                "SOC_VRM Temp", "Peripheral Temp", "System Temp", "GPU1 Temp", "gpu_temp", "hdd_max", "nvme_max",
                "hba_max", "ses_max"]


def validate_config(new, base, safety):
    """-> (errors, warnings, cfg). Only zones/curves/floors/hysteresis/ramp are taken from 'new'; everything else
    comes from 'base' (the running config), so an import can't change intervals, paths or failsafe duty."""
    err, warn = [], []
    cfg = json.loads(json.dumps(base))
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    if not isinstance(new, dict) or not isinstance(new.get("zones"), dict):
        return ["config must be an object with a 'zones' object"], warn, None
    lo, hi = safety["hysteresis_c"]
    h = new.get("hysteresis_c", base.get("hysteresis_c", 3))
    if not num(h) or not lo <= h <= hi:
        err.append("hysteresis_c must be %s..%s °C" % (lo, hi))
    else:
        cfg["hysteresis_c"] = h
    lo, hi = safety["ramp_down_pct_per_min"]
    r = new.get("ramp_down_pct_per_min", base.get("ramp_down_pct_per_min", 5))
    if not num(r) or not lo <= r <= hi:
        err.append("ramp_down_pct_per_min must be %s..%s" % (lo, hi))
    else:
        cfg["ramp_down_pct_per_min"] = r
    if set(new["zones"]) != set(base["zones"]):
        err.append("zones must be exactly %s" % sorted(base["zones"]))
        return err, warn, None
    for z, bz in base["zones"].items():
        nz = new["zones"][z]
        zn = "zone %s" % z
        if not isinstance(nz, dict):
            err.append("%s: must be an object" % zn)
            continue
        floor = nz.get("floor")
        if not num(floor) or not safety["min_floor"] <= floor <= 100:
            err.append("%s: floor must be %s..100 %%" % (zn, safety["min_floor"]))
            continue
        curves = nz.get("curves")
        if not isinstance(curves, list) or not curves:
            err.append("%s: needs at least one curve" % zn)
            continue
        seen = set()
        clean = []
        for i, c in enumerate(curves):
            sn = c.get("sensor") if isinstance(c, dict) else None
            cn = "%s curve %d (%s)" % (zn, i + 1, sn if sn in CURVE_INPUTS else "?")   # never echo arbitrary text
            if not isinstance(c, dict) or c.get("sensor") not in CURVE_INPUTS:
                err.append("%s: unknown sensor" % cn)
                continue
            s = c["sensor"]
            if s in seen:
                err.append("%s: sensor used twice in this zone" % cn)
            seen.add(s)
            pts = c.get("points")
            if not isinstance(pts, list) or not 2 <= len(pts) <= 32 or not all(isinstance(p, list) and len(p) == 2 and all(num(v) for v in p) for p in pts):
                err.append("%s: needs 2..32 [temp, duty] points" % cn)
                continue
            pts = [[round(p[0], 1), round(p[1])] for p in pts]   # round FIRST so every check sees what will run
            if any(b[0] <= a[0] for a, b in zip(pts, pts[1:])):
                err.append("%s: temperatures must strictly increase" % cn)
            if any(b[1] < a[1] for a, b in zip(pts, pts[1:])):
                err.append("%s: duty may never decrease as temperature rises" % cn)
            if any(not 0 <= p[1] <= 100 for p in pts) or any(not 0 <= p[0] <= 120 for p in pts):
                err.append("%s: temp 0..120 °C, duty 0..100 %%" % cn)
            if pts[-1][1] != 100:
                err.append("%s: last point must be 100 %%" % cn)
            cap = safety["full_by"].get(s)
            if cap is not None and pts[-1][0] > cap:
                err.append("%s: must reach 100 %% by %s °C (safety cap), currently %s °C" % (cn, cap, pts[-1][0]))
            if pts[0][1] < floor:
                warn.append("%s: starts below the zone floor (%s %%), floor wins" % (cn, floor))
            opt = bool(c.get("optional"))
            if opt and s in safety["required"].get(z, []):
                err.append("%s: required input can't be optional (safety)" % cn)
            clean.append({"sensor": s, "points": pts, **({"optional": True} if opt else {})})
        for req in safety["required"].get(z, []):
            if req not in seen:
                err.append("%s: required input %s is missing (safety)" % (zn, req))
        cfg["zones"][z]["floor"] = round(floor)
        cfg["zones"][z]["curves"] = clean
        if isinstance(nz.get("name"), str) and nz["name"].strip():
            cfg["zones"][z]["name"] = nz["name"].strip()[:40]
    return err, warn, (cfg if not err else None)


RUNTIME_BOUNDS = {"interval_s": (2, 30), "hdd_interval_s": (30, 3600), "read_fail_limit": (1, 10),
                  "reassert_s": (10, 300), "history_days": (1, 365)}


def load_file_config(new, safety):
    """A config.json read from disk (startup or hand edit) -> (errors, warnings, cfg). Same curve validation as the UI,
    plus bounds on the runtime keys a hand edit may change; missing keys come from DEFAULT_CONFIG."""
    if not isinstance(new, dict) or not isinstance(new.get("zones"), dict):
        return ["config must be an object with a 'zones' object"], [], None
    if set(new["zones"]) != set(DEFAULT_CONFIG["zones"]):
        return ["zones must be exactly %s" % sorted(DEFAULT_CONFIG["zones"])], [], None
    base = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in new.items():
        if k != "zones":
            base[k] = v
    err = []
    for z, nz in new["zones"].items():
        if not isinstance(nz, dict):
            return ["zone %s: must be an object" % z], [], None
        if isinstance(nz.get("fans"), list) and all(isinstance(f, str) for f in nz["fans"]):
            base["zones"][z]["fans"] = nz["fans"]
        if nz.get("max", 100) != 100:
            err.append("zone %s: max must be 100" % z)
    for k, (lo, hi) in RUNTIME_BOUNDS.items():
        v = base.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
            err.append("%s must be %s..%s" % (k, lo, hi))
    if base.get("failsafe_duty") != 100:
        err.append("failsafe_duty must be 100")
    e, w, cfg = validate_config(new, base, safety)
    err += e
    return err, w, (cfg if not err else None)


# Top-down chassis plan, traced from photos (2026-11-24). Seen from above with the FRONT at the bottom.
# x: 0 (left when facing the front) .. 16 (right); y: 0 (front bezel) .. 26 (rear).
# Drag sensors in the UI (Edit positions) and Save to correct; saved to /data/layout.json.
DEFAULT_LAYOUT = {
    "name": "NAS",                       # shown in the page header / tab title
    "chassis": "Supermicro CSE-846 · H12SSL-i · custom 3x120 fan wall",
    "grid": {"w": 16, "h": 26},
    "regions": [
        {"name": "Drive cage - 24 bays", "x": 0, "y": 0, "w": 16, "h": 4.9, "kind": "drives"},
        {"name": "Fan wall shroud (3x NF-F12 iPPC-3000)", "x": 0, "y": 5.0, "w": 16, "h": 4.1, "kind": "fanwall"},
        {"name": "H12SSL-i", "x": 1.95, "y": 12.6, "w": 8.85, "h": 12.0, "kind": "board"},
        {"name": "NH-U9 TR4-SP3 (blows toward PSU)", "x": 6.9, "y": 15.5, "w": 4.4, "h": 5.6, "kind": "cooler"},
        {"name": "PSU be quiet! SP12 1000W", "x": 11.5, "y": 14.0, "w": 4.5, "h": 10.6, "kind": "psu"},
        {"name": "RTX A4000", "x": 5.1, "y": 12.0, "w": 0.8, "h": 12.6, "kind": "card"},
        {"name": "Konyead 4x NVMe (slot 6)", "x": 2.8, "y": 15.5, "w": 2.2, "h": 8.8, "kind": "card"},
        {"name": "Rear NF-A8 x2", "x": 7.0, "y": 21.8, "w": 5.6, "h": 2.2, "kind": "fans"},
    ],
    # header -> physical fans. FANA feeds the 3-fan wall through a splitter (one tach); FAN5 the two rear NF-A8s.
    "fans": [
        {"header": "FANA", "zone": 1, "model": "NF-F12 iPPC-3000", "x": 13.2, "y": 7.7, "r": 1.7},
        {"header": "FANA", "zone": 1, "model": "NF-F12 iPPC-3000", "x": 8.5, "y": 7.7, "r": 1.7},
        {"header": "FANA", "zone": 1, "model": "NF-F12 iPPC-3000", "x": 3.3, "y": 7.7, "r": 1.7},
        {"header": "FAN1", "zone": 0, "model": "NF-A9 on NH-U9", "x": 10.7, "y": 18.3, "r": 0.9},
        {"header": "FAN2", "zone": 0, "model": "NF-A9 on NH-U9", "x": 7.3, "y": 18.3, "r": 0.9},
        {"header": "FAN5", "zone": 0, "model": "NF-A8 rear", "x": 11.1, "y": 22.9, "r": 0.9},
        {"header": "FAN5", "zone": 0, "model": "NF-A8 rear", "x": 8.4, "y": 22.9, "r": 0.9},
        {"header": "GPU", "zone": None, "model": "RTX A4000 blower (own control)", "x": 5.5, "y": 13.0, "r": 0.6},
    ],
    "sensors": {
        "System Temp": {"x": 4.4, "y": 13.0, "loc": "System"},
        "Peripheral Temp": {"x": 2.6, "y": 13.6, "loc": "I/O board"},
        "CPU Temp": {"x": 9.1, "y": 18.0, "loc": "CPU"},
        "Tctl": {"x": 9.1, "y": 19.0, "loc": "CPU (Tctl)"},
        "P1_DIMMA~D Temp": {"x": 6.6, "y": 17.4, "loc": "Memory A-D (cooler intake side)"},
        "P1_DIMME~H Temp": {"x": 10.4, "y": 17.4, "loc": "Memory E-H (cooler exhaust side)"},
        "VRMABCD Temp": {"x": 6.4, "y": 14.0, "loc": "VRM"},
        "VRMEFGH Temp": {"x": 10.2, "y": 13.8, "loc": "VRM"},
        "CPU_VRM Temp": {"x": 7.6, "y": 13.1, "loc": "VRM"},
        "SOC_VRM Temp": {"x": 8.6, "y": 13.1, "loc": "VRM"},
        "GPU1 Temp": {"x": 5.5, "y": 18.0, "loc": "PCIe (A4000)"},
        "nvme:0000:05:00.0": {"x": 3.6, "y": 14.6, "loc": "M.2 onboard (boot)"},
        "nvme:0000:06:00.0": {"x": 3.6, "y": 15.3, "loc": "M.2 onboard (boot)"},
        "nvme:0000:01:00.0": {"x": 3.9, "y": 17.0, "loc": "Slot 6 carrier"},
        "nvme:0000:02:00.0": {"x": 3.9, "y": 18.8, "loc": "Slot 6 carrier"},
        "nvme:0000:03:00.0": {"x": 3.9, "y": 20.6, "loc": "Slot 6 carrier"},
        "nvme:0000:04:00.0": {"x": 3.9, "y": 22.4, "loc": "Slot 6 carrier"},
        "hba:0000:c3:00.0": {"x": 2.3, "y": 18.6, "loc": "PCIe HBA (IOC 0)"},
        "hba:0000:c5:00.0": {"x": 2.3, "y": 20.0, "loc": "PCIe HBA (IOC 1)"},
        "hba:0000:c6:00.0": {"x": 2.2, "y": 22.6, "loc": "PCIe HBA (SAS2116)"},
        "gpu:0000:81:00.0": {"x": 5.5, "y": 19.2, "loc": "GPU"},
        "ses:0": {"x": 11.0, "y": 4.7, "loc": "Backplane (SAS3x40)"},
        "ses:1": {"x": 4.0, "y": 4.7, "loc": "Backplane expander chip"},   # hot chip is on the left (user, 11/24)
        "hdd_col0": {"x": 2, "y": 2.5, "loc": "Drive column 1"},
        "hdd_col1": {"x": 6, "y": 2.5, "loc": "Drive column 2"},
        "hdd_col2": {"x": 10, "y": 2.5, "loc": "Drive column 3"},
        "hdd_col3": {"x": 14, "y": 2.5, "loc": "Drive column 4"},
    },
    # Front-bay grid as seen from the front: rows top->bottom, cols left->right, value = SES slot number.
    # CSE-846 / SAS3x40: column-major from the bottom-left, counting upward (slot 0 bottom-left, 5 top-left,
    # 23 top-right) - verified against the drive serial labels 2026-11-24.
    "bays": {"rows": 6, "cols": 4, "order": [[c * 6 + (5 - r) for c in range(4)] for r in range(6)]},
    # NVMe graphic: physical groups by PCIe address (order within a group = position on the board/card, adjust if needed)
    "nvme_groups": [
        {"name": "Onboard M.2 (H12SSL-i)", "kind": "board", "addresses": ["0000:05:00.0", "0000:06:00.0"]},
        {"name": "PCIe slot 6 · Konyead 4x M.2 (x4x4x4x4)", "kind": "card",
         "addresses": ["0000:01:00.0", "0000:02:00.0", "0000:03:00.0", "0000:04:00.0"]},
    ],
}

EVENTS = collections.deque(maxlen=300)
LOGFILE = None          # set in main(): <logs>/fanctl.log, rotated at 5 MB, 5 kept
OWNER = None            # (uid, gid) from PUID/PGID: every file we create is chowned to the app account


NOTIFY_FILE = None      # <logs>/notify.jsonl - sent as email by the host script (fanctl_host.sh, TrueNAS cron)
HEARTBEAT = None        # <logs>/heartbeat - the host watchdog forces 100 % if it goes stale
_notified = {}


def notify(key, subject, text, every_s=1800):
    """Queue an email for the host script. Same key at most once per every_s."""
    now = time.time()
    if NOTIFY_FILE is None or now - _notified.get(key, 0) < every_s:
        return
    _notified[key] = now
    try:
        new = not os.path.exists(NOTIFY_FILE)
        with open(NOTIFY_FILE, "a") as f:
            f.write(json.dumps({"t": int(now), "key": key, "subject": subject, "text": text}) + "\n")
        if new:
            own(NOTIFY_FILE)
            os.chmod(NOTIFY_FILE, 0o640)
    except OSError:
        pass


def heartbeat():
    if HEARTBEAT:
        try:
            tmp = HEARTBEAT + ".tmp"
            with open(tmp, "w") as f:
                f.write(str(int(time.time())))
            own(tmp)
            os.replace(tmp, HEARTBEAT)
        except OSError:
            pass


def own(path):
    if OWNER:
        try:
            os.chown(path, *OWNER)
        except OSError:
            pass


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    EVENTS.append({"t": time.time(), "msg": msg})
    if LOGFILE:
        try:
            if os.path.exists(LOGFILE) and os.path.getsize(LOGFILE) > 5 * 1024 * 1024:
                for i in range(4, 0, -1):
                    if os.path.exists("%s.%d" % (LOGFILE, i)):
                        os.replace("%s.%d" % (LOGFILE, i), "%s.%d" % (LOGFILE, i + 1))
                os.replace(LOGFILE, LOGFILE + ".1")
            new = not os.path.exists(LOGFILE)
            with open(LOGFILE, "a") as f:
                f.write(line + "\n")
            if new:
                own(LOGFILE)
        except OSError:
            pass


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        with open(path, "w") as f:
            json.dump(default, f, indent=2)
        own(path)
        return json.loads(json.dumps(default))
    except (OSError, ValueError) as e:   # malformed / unreadable: keep running on defaults, leave the file for a human
        log("WARNING: %s unreadable (%s) - using built-in defaults" % (path, e))
        return json.loads(json.dumps(default))


def run(cmd, timeout=20):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout


def interp(points, t):
    if t <= points[0][0]:
        return points[0][1]
    for (t0, d0), (t1, d1) in zip(points, points[1:]):
        if t <= t1:
            return d0 + (d1 - d0) * (t - t0) / (t1 - t0)
    return points[-1][1]


# ---------------------------------------------------------------- hardware backends
class Hardware:
    def __init__(self, cfg):
        self.cfg = cfg
        self.enc = None
        self.drive_meta = {}  # dev -> {serial, model}
        self.find_enclosure()

    # BMC sensors: name -> dict(value, unit, status, thresholds)
    def bmc(self):
        rc, out = run(["ipmitool", "sensor"], timeout=30)
        if rc != 0 or not out.strip():
            raise RuntimeError("ipmitool sensor failed rc=%s" % rc)
        res = {}
        for line in out.splitlines():
            p = [x.strip() for x in line.split("|")]
            if len(p) < 10:
                continue
            def num(x):
                try:
                    return float(x)
                except ValueError:
                    return None
            res[p[0]] = {"value": num(p[1]), "unit": p[2], "status": p[3], "raw": p[1],
                         "lnr": num(p[4]), "lcr": num(p[5]), "lnc": num(p[6]),
                         "unc": num(p[7]), "ucr": num(p[8]), "unr": num(p[9])}
        return res

    def hwmon(self):
        nv, tctl = {}, None
        for h in glob.glob("/sys/class/hwmon/hwmon*"):
            try:
                name = open(h + "/name").read().strip()
                t = int(open(h + "/temp1_input").read()) / 1000.0
            except OSError:
                continue
            if name == "k10temp":
                tctl = t
            elif name == "nvme":
                ctrl = os.path.basename(os.path.realpath(h + "/device"))
                try:
                    addr = open("/sys/class/nvme/%s/address" % ctrl).read().strip()
                    serial = open("/sys/class/nvme/%s/serial" % ctrl).read().strip()
                    model = open("/sys/class/nvme/%s/model" % ctrl).read().strip()
                except OSError:
                    addr, serial, model = ctrl, "", ""
                nv["nvme:" + addr] = {"value": t, "dev": ctrl, "serial": serial, "model": model}
        return nv, tctl

    def find_enclosure(self):
        for e in glob.glob("/sys/class/enclosure/*"):
            try:
                model = open(e + "/device/model").read().strip()
            except OSError:
                continue
            if self.cfg.get("enclosure_match", "") in model:
                self.enc = e
                return

    def slots(self):
        """[{slot, dev}] from SES."""
        out = []
        if not self.enc:
            return out
        for s in sorted(glob.glob(self.enc + "/Slot*")):
            m = re.search(r"(\d+)$", s)
            if not m:
                continue
            dev = None
            blk = glob.glob(s + "/device/block/*")
            if blk:
                dev = os.path.basename(blk[0])
            out.append({"slot": int(m.group(1)), "dev": dev, "path": s})
        return out

    def hdd_smart(self, dev):
        """-> (status, smart dict or None). status: active | standby | error. Never spins a sleeping disk up."""
        try:
            rc, out = run(["smartctl", "-j", "-n", "standby", "-i", "-H", "-A", "-l", "error", "/dev/" + dev], timeout=30)
            j = json.loads(out)
        except (ValueError, subprocess.SubprocessError, OSError):
            return "error", None
        if rc & 2 and "temperature" not in j:
            # exit bit 1 = "device open failed OR in a low-power mode": only call it standby if smartctl says so
            msgs = " ".join(str(m.get("string", "")) for m in (j.get("smartctl") or {}).get("messages") or [])
            if re.search(r"STANDBY|SLEEP", msgs, re.I):
                return "standby", None
            return "error", None
        return "active", parse_smart(j)

    def set_duty(self, zone, pct):
        rc, out = run(["ipmitool", "raw", "0x30", "0x70", "0x66", "0x01", "0x%02x" % int(zone), "0x%02x" % int(pct)])
        if rc != 0:
            raise RuntimeError("ipmitool set zone %s=%s%% rc=%s %s" % (zone, pct, rc, out.strip()[:80]))

    def get_duty(self, zone):
        rc, out = run(["ipmitool", "raw", "0x30", "0x70", "0x66", "0x00", "0x%02x" % int(zone)])
        try:
            return int(out.strip(), 16) if rc == 0 and out.strip() else None
        except ValueError:
            return None

    def ensure_full_mode(self):
        rc, out = run(["ipmitool", "raw", "0x30", "0x45", "0x00"])
        if rc == 0 and out.strip() != "01":
            rc2, _ = run(["ipmitool", "raw", "0x30", "0x45", "0x01", "0x01"])
            if rc2 != 0:
                log("fan mode was %s, setting Full FAILED rc=%s" % (out.strip(), rc2))
            log("fan mode was %s -> set Full" % out.strip())
            return True
        return False

    def locate(self, slot, on):
        if not self.enc:
            return False
        p = "%s/Slot%02d/locate" % (self.enc, slot)
        with open(p, "w") as f:
            f.write("1" if on else "0")
        return True

    def locate_state(self, slot):
        try:
            return open("%s/Slot%02d/locate" % (self.enc, slot)).read().strip() == "1"
        except (OSError, TypeError):
            return False

    def hba(self):
        return mpt_temp.read_temps() if mpt_temp else []

    # ---- BMC platform info (chassis, FRU, MC, LAN, SEL)
    def chassis(self):
        rc, out = run(["ipmitool", "chassis", "status"])
        d = {}
        for line in out.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                d[k.strip()] = v.strip()
        rc, poh = run(["ipmitool", "chassis", "poh"])
        m = re.search(r"POH Counter\s*:\s*(.*)", poh)
        d["POH"] = m.group(1).strip() if m else None
        return d

    def bmc_static(self):
        info = {}
        rc, out = run(["ipmitool", "mc", "info"])
        for k in ("Firmware Revision", "IPMI Version", "Manufacturer Name", "Product ID"):
            m = re.search(re.escape(k) + r"\s*:\s*(.*)", out)
            info[k] = m.group(1).strip() if m else None
        rc, out = run(["ipmitool", "mc", "selftest"])
        info["Selftest"] = (re.search(r"Selftest:\s*(.*)", out) or [None, None])[1]
        rc, out = run(["ipmitool", "fru"])
        for k in ("Board Mfg Date", "Board Mfg", "Board Product", "Board Serial", "Product Name", "Product Serial"):
            m = re.search(r"^\s*" + re.escape(k) + r"\s*:\s*(.*)$", out, re.M)
            if m and m.group(1).strip():
                info[k] = m.group(1).strip()
        rc, out = run(["ipmitool", "lan", "print", "1"])
        for k, lab in (("IP Address Source", "BMC IP source"), ("IP Address", "BMC IP"), ("MAC Address", "BMC MAC"), ("Default Gateway IP", "BMC gateway")):
            m = re.search(r"^" + re.escape(k) + r"\s*:\s*(.*)$", out, re.M)
            if m:
                info[lab] = m.group(1).strip()
        rc, out = run(["ipmitool", "dcmi", "power", "reading"])
        m = re.search(r"Instantaneous power reading:\s*(\d+)", out)
        info["DCMI power"] = (m.group(1) + " W") if m and m.group(1) != "0" else "not reported (ATX PSU, no PMBus)"
        return info

    def sel_info(self):
        rc, out = run(["ipmitool", "sel", "info"])
        g = lambda k: (re.search(re.escape(k) + r"\s*:\s*(.*)", out) or [None, None])[1]
        return {"entries": _int(g("Entries")), "used": g("Percent Used"), "overflow": g("Overflow")}

    def sel(self):
        """SEL entries with the BMC's real local time. The BMC keeps local wall-clock time in the SEL; ipmitool
        treats it as UTC and shifts it by the TZ offset (6 h early here), so read it with TZ=UTC and re-label."""
        p = subprocess.run(["ipmitool", "sel", "elist"], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, TZ="UTC"))
        ev = []
        for line in p.stdout.splitlines():
            f = [x.strip() for x in line.split("|")]
            if len(f) < 5:
                continue
            try:
                lt = time.strptime(f[1] + " " + f[2].split()[0], "%m/%d/%y %H:%M:%S")
                ts = time.mktime(lt)
            except ValueError:
                ts = None
            ev.append({"id": f[0], "t": ts, "sensor": f[3], "event": f[4], "dir": f[5] if len(f) > 5 else "",
                       "detail": f[6] if len(f) > 6 else ""})
        return ev

    def gpu(self):
        """nvidia-smi (injected by the NVIDIA runtime with NVIDIA_DRIVER_CAPABILITIES=utility) -> [dict]"""
        f = ["index", "name", "pci.bus_id", "temperature.gpu", "fan.speed", "power.draw", "power.limit",
             "utilization.gpu", "memory.used", "memory.total", "clocks.sm", "pstate", "temperature.gpu.tlimit"]
        rc, out = run(["nvidia-smi", "--query-gpu=" + ",".join(f), "--format=csv,noheader,nounits"], timeout=15)
        if rc != 0:
            raise RuntimeError("nvidia-smi rc=%s %s" % (rc, out.strip()[:120]))
        res = []
        for line in out.strip().splitlines():
            v = [x.strip() for x in line.split(",")]
            def num(x):
                try:
                    return float(x)
                except ValueError:
                    return None
            d = dict(zip(f, v))
            res.append({"index": d["index"], "name": d["name"], "pci": d["pci.bus_id"].lower()[4:] if d["pci.bus_id"].startswith("0000") else d["pci.bus_id"].lower(),
                        "temp": num(d["temperature.gpu"]), "fan_pct": num(d["fan.speed"]), "power_w": num(d["power.draw"]),
                        "power_limit_w": num(d["power.limit"]), "util_pct": num(d["utilization.gpu"]),
                        "mem_used_mb": num(d["memory.used"]), "mem_total_mb": num(d["memory.total"]),
                        "sm_mhz": num(d["clocks.sm"]), "pstate": d["pstate"], "tlimit_c": num(d.get("temperature.gpu.tlimit", ""))})
        return res

    def ses_temps(self):
        """Backplane expander SES temperature elements -> [°C, ...] (element order)."""
        if not self.enc:
            return []
        sg = glob.glob(self.enc + "/device/scsi_generic/sg*")
        if not sg:
            return []
        rc, out = run(["sg_ses", "-p", "2", "/dev/" + os.path.basename(sg[0])], timeout=20)
        sec = re.search(r"Element type: Temperature sensor(.*?)(?:Element type:|\Z)", out, re.S)
        if not sec:
            return []
        return [int(t) for t in re.findall(r"Element \d+ descriptor:.*?Temperature=(-?\d+) C", sec.group(1), re.S)]

    def nvme_list(self):
        out = []
        for c in sorted(glob.glob("/sys/class/nvme/nvme*")):
            rd = lambda f: (open(c + "/" + f).read().strip() if os.path.exists(c + "/" + f) else None)
            ns = sorted(os.path.basename(n) for n in glob.glob(c + "/nvme*n*") if re.search(r"n\d+$", n))
            out.append({"ctrl": os.path.basename(c), "dev": ns[0] if ns else None, "address": rd("address"),
                        "serial": rd("serial"), "model": rd("model"), "firmware": rd("firmware_rev"), "state": rd("state")})
        return out

    def nvme_smart(self, ctrl):
        rc, out = run(["smartctl", "-j", "-H", "-A", "-i", "/dev/" + ctrl], timeout=30)
        try:
            return parse_nvme(json.loads(out))
        except ValueError:
            return None

    def fault(self, slot, on):
        if not self.enc:
            return False
        with open("%s/Slot%02d/fault" % (self.enc, slot), "w") as f:
            f.write("1" if on else "0")
        return True

    def fault_state(self, slot):
        try:
            return open("%s/Slot%02d/fault" % (self.enc, slot)).read().strip() == "1"
        except (OSError, TypeError):
            return False

    def zpool(self, path):
        with open(path) as f:
            zj = json.load(f)
        parts, pser = {}, {}
        try:
            with open(os.path.join(os.path.dirname(path), "lsblk.json")) as f:
                for bd in json.load(f).get("blockdevices", []):
                    for ch in bd.get("children") or []:
                        if ch.get("partuuid"):
                            parts[ch["partuuid"]] = bd["name"]
                            pser[ch["partuuid"]] = bd.get("serial")
        except (OSError, ValueError):
            pass
        return zj, os.path.getmtime(path), parts, pser


def _int(v):
    try:
        return int(str(v).split()[0])
    except (ValueError, TypeError, IndexError):
        return None


def parse_smart(j):
    s = {"model": j.get("model_name") or ((j.get("scsi_vendor") or "") + " " + (j.get("scsi_product") or "")).strip(),
         "serial": j.get("serial_number"), "firmware": j.get("firmware_version") or j.get("scsi_revision"),
         "capacity_tb": round((j.get("user_capacity") or {}).get("bytes", 0) / 1e12, 1),
         "rpm": j.get("rotation_rate"), "proto": (j.get("device") or {}).get("protocol"),
         "passed": (j.get("smart_status") or {}).get("passed"),
         "temp": (j.get("temperature") or {}).get("current"),
         "hours": (j.get("power_on_time") or {}).get("hours")}
    attrs = {a["id"]: a for a in (j.get("ata_smart_attributes") or {}).get("table", [])}
    if attrs:
        g = lambda i: _int(attrs[i]["raw"].get("value")) if i in attrs else None
        s["attr_failing_now"] = [a["name"] for a in attrs.values() if a.get("when_failed") == "now"]
        s["attr_failed_past"] = [a["name"] for a in attrs.values() if a.get("when_failed") == "past"]
        s.update(realloc=g(5), pending=g(197), offline_unc=g(198), reported_unc=g(187), cmd_timeout=g(188), crc=g(199),
                 errlog=(j.get("ata_smart_error_log") or {}).get("summary", {}).get("count"))
    else:
        log_ = j.get("scsi_error_counter_log") or {}
        s.update(grown_defects=j.get("scsi_grown_defect_list"),
                 read_unc=(log_.get("read") or {}).get("total_uncorrected_errors"),
                 write_unc=(log_.get("write") or {}).get("total_uncorrected_errors"),
                 verify_unc=(log_.get("verify") or {}).get("total_uncorrected_errors"),
                 cycles=(j.get("scsi_start_stop_cycle_counter") or {}).get("accumulated_start_stop_cycles"))
    return s


def fmt0(v):
    return "–" if v is None else ("%d" % round(v))


def parse_nvme(j):
    h = j.get("nvme_smart_health_information_log") or {}
    duw = h.get("data_units_written")
    return {"passed": (j.get("smart_status") or {}).get("passed"), "critical_warning": h.get("critical_warning"),
            "temp": h.get("temperature") or (j.get("temperature") or {}).get("current"),
            "spare": h.get("available_spare"), "spare_thresh": h.get("available_spare_threshold"),
            "used_pct": h.get("percentage_used"), "tb_written": round(duw * 512000 / 1e12, 1) if duw is not None else None,
            "hours": h.get("power_on_hours"), "unsafe_shutdowns": h.get("unsafe_shutdowns"), "media_errors": h.get("media_errors"),
            "err_log": h.get("num_err_log_entries"), "capacity_gb": round((j.get("user_capacity") or {}).get("bytes", 0) / 1e9),
            "temp_sensors": h.get("temperature_sensors")}


def zfs_members(zj, parts):
    """zpool status -j (+ lsblk partuuid->disk) -> {dev: {pool, vdev, state, read, write, cksum}}, [non-ONLINE leaves
    whose disk is gone]. (-L would give /dev/sdX paths, but ZFS 2.3.0 then omits the leaf 'state'.)"""
    out, missing = {}, []
    def walk(pool, parent, vdevs):
        for name, v in (vdevs or {}).items():
            kids = v.get("vdevs")
            if kids:
                walk(pool, name if v.get("vdev_type") not in ("root",) else parent, kids)
                continue
            st = v.get("state") or "UNKNOWN"
            path = v.get("path") or name
            pu = path.rsplit("/", 1)[-1] if "by-partuuid" in path else (name if name in parts else None)  # REMOVED vdevs may keep only the raw uuid
            e = {"pool": pool, "vdev": parent, "state": st, "path": path, "partuuid": pu,
                 "read": _int(v.get("read_errors")) or 0, "write": _int(v.get("write_errors")) or 0,
                 "cksum": _int(v.get("checksum_errors")) or 0}
            dev = parts.get(pu) if pu else None
            if not dev:
                m = re.match(r"/dev/((?:sd[a-z]+)|(?:nvme\d+n\d+))", path)
                dev = m.group(1) if m else None
            if dev:
                out[dev] = e
            if st != "ONLINE" and (not dev or not os.path.exists("/sys/block/" + dev)):
                missing.append(e)
    for pname, p in (zj.get("pools") or {}).items():
        walk(pname, pname, p.get("vdevs"))
        for sec in ("spares", "logs", "l2cache", "special", "dedup"):
            if p.get(sec):
                walk(pname, sec, p[sec])
    return out, missing


class MockHardware(Hardware):
    """Synthetic data for UI development off-box."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.enc = "mock"
        self.drive_meta = {}
        self.duty = {0: 84, 1: 54}
        self.t0 = time.time()
        self.leds = set()

    def load(self):
        return 0.5 + 0.5 * math.sin((time.time() - self.t0) / 120.0)

    def bmc(self):
        L = self.load()
        c0 = (self.duty[0] - 84) / 16.0
        c1 = (self.duty[1] - 54) / 46.0
        def s(v, unc=None, ucr=None, unit="degrees C"):
            return {"value": round(v + random.uniform(-0.4, 0.4), 1), "unit": unit, "status": "ok",
                    "lnr": None, "lcr": None, "lnc": None, "unc": unc, "ucr": ucr, "unr": None}
        d = {
            "CPU Temp": s(55 + 28 * L - 2 * c0, 90, 95), "System Temp": s(33 + 3 * L - 2 * c1, 80, 85),
            "Peripheral Temp": s(42 + 3 * L - 4 * c1, 80, 85), "CPU_VRM Temp": s(42 + 14 * L, 95, 100),
            "SOC_VRM Temp": s(50 + 9 * L, 95, 100), "VRMABCD Temp": s(51 + 5 * L - 3 * c1, 95, 100),
            "VRMEFGH Temp": s(52 + 12 * L - 2 * c0, 95, 100), "P1_DIMMA~D Temp": s(52 + 14 * L - 6 * c1, 80, 85),
            "P1_DIMME~H Temp": s(53 + 14 * L - 3 * c0, 80, 85), "GPU1 Temp": s(37, 90, 95),
            "FAN1": s(980 + 980 * self.duty[0] / 100, None, None, "RPM"), "FAN2": s(980 + 980 * self.duty[0] / 100, None, None, "RPM"),
            "FAN5": s(1100 + 1280 * self.duty[0] / 100, None, None, "RPM"), "FANA": s(2660 * self.duty[1] / 100, None, None, "RPM"),
            "FAN3": {"value": None, "unit": "", "status": "na"}, "M2_SSD1 Temp": {"value": None, "unit": "", "status": "na"},
            "12V": dict(s(12.24, None, None, "Volts"), lcr=9.936, ucr=14.48), "5VCC": dict(s(5.11, None, None, "Volts"), lcr=4.03, ucr=5.92),
            "3.3VCC": dict(s(3.361, None, None, "Volts"), lcr=2.681, ucr=3.922), "VDDCR": dict(s(1.183, None, None, "Volts"), lcr=0.499, ucr=1.732),
            "VBAT": {"value": None, "unit": "discrete", "status": "0x0400", "raw": "0x4"}, "Chassis Intru": {"value": None, "unit": "discrete", "status": "0x0000", "raw": "0x0"},
        }
        return d

    def hwmon(self):
        L = self.load()
        nv = {}
        for i, a in enumerate(["0000:01:00.0", "0000:02:00.0", "0000:03:00.0", "0000:04:00.0", "0000:05:00.0", "0000:06:00.0"]):
            nv["nvme:" + a] = {"value": 35 + i + 3 * L, "dev": "nvme%d" % i, "serial": "NVMEMOCK%02d" % i, "model": "SAMSUNG MZVL4512HBLU"}
        return nv, 56 + 28 * L

    def slots(self):
        return [{"slot": i, "dev": None if i == 22 else "sd" + chr(97 + i), "path": None} for i in range(24)]

    def hdd_smart(self, dev):
        i = ord(dev[-1]) - 97
        sas = i % 3 == 2
        s = {"model": "HGST HUH721010AL42C0" if sas else "HGST HUH721010ALE601", "serial": "HDDMOCK%02d" % i,
             "firmware": "A3Z4" if sas else "X92C", "capacity_tb": 10.0, "rpm": 7200, "proto": "SCSI" if sas else "ATA",
             "passed": i != 9, "temp": int(28 + (i % 4 in (1, 2)) * 2 + (i // 4) * 0.6 + random.uniform(0, 1)), "hours": 35000 + i * 311}
        if sas:
            s.update(grown_defects=0, read_unc=0, write_unc=0, verify_unc=0, cycles=40)
        else:
            s.update(realloc=8 if i == 6 else 0, pending=0, offline_unc=0, reported_unc=0, cmd_timeout=0, crc=0, errlog=0)
        return "active", s

    def hba(self):
        return [{"host": "host0", "board": "SAS9300-16i", "fw": "16.00.12.00", "pci": "0000:c3:00.0", "ioc_temp_c": 68 + random.randint(0, 2)},
                {"host": "host19", "board": "SAS9300-16i", "fw": "16.00.12.00", "pci": "0000:c5:00.0", "ioc_temp_c": 66 + random.randint(0, 2)},
                {"host": "host20", "board": "SAS9200-16e", "fw": "20.00.06.00", "pci": "0000:c6:00.0", "ioc_temp_c": None}]

    def ses_temps(self):
        return [30, 66]

    def chassis(self):
        return {"System Power": "on", "Power Overload": "false", "Power Interlock": "inactive", "Main Power Fault": "false",
                "Power Control Fault": "false", "Power Restore Policy": "previous", "Chassis Intrusion": "inactive",
                "Drive Fault": "false", "Cooling/Fan Fault": "false", "POH": "0 days, 2 hours"}

    def bmc_static(self):
        return {"Firmware Revision": "1.08", "IPMI Version": "2.0", "Manufacturer Name": "Super Micro Computer Inc.",
                "Selftest": "passed", "Board Mfg": "Supermicro", "Board Serial": "BOARDMOCK", "BMC IP": "192.0.2.19",
                "DCMI power": "not reported (ATX PSU, no PMBus)"}

    def sel_info(self):
        return {"entries": 3, "used": "1%", "overflow": "false"}

    def sel(self):
        t = time.time()
        return [{"id": "a9", "t": t - 86400 * 10, "sensor": "Temperature P1_DIMMA~D Temp", "event": "Upper Critical going high", "dir": "Asserted", "detail": "Reading 85 > Threshold 85 degrees C"},
                {"id": "ab", "t": t - 86400 * 10 + 360, "sensor": "Temperature P1_DIMME~H Temp", "event": "Upper Non-recoverable going high", "dir": "Asserted", "detail": "Reading 90 > Threshold 90 degrees C"},
                {"id": "ac", "t": t - 86400 * 10 + 480, "sensor": "Temperature P1_DIMME~H Temp", "event": "Upper Non-recoverable going high", "dir": "Deasserted", "detail": "Reading 88 < Threshold 90 degrees C"}]

    def gpu(self):
        return [{"index": "0", "name": "NVIDIA RTX A4000", "pci": "0000:81:00.0", "temp": 38 + random.randint(0, 2), "fan_pct": 30,
                 "power_w": 11.5, "power_limit_w": 140, "util_pct": 0, "mem_used_mb": 1, "mem_total_mb": 16376, "sm_mhz": 210,
                 "pstate": "P8", "tlimit_c": 55}]

    def nvme_list(self):
        return [{"ctrl": "nvme%d" % i, "dev": "nvme%dn1" % i, "address": a, "serial": "NVMEMOCK%02d" % i,
                 "model": "SAMSUNG MZVL41T0HBLB-00BTW" if i in (2, 4) else "SAMSUNG MZVL4512HBLU-00BH1",
                 "firmware": "MOCKFW01", "state": "live"}
                for i, a in enumerate(["0000:01:00.0", "0000:02:00.0", "0000:03:00.0", "0000:04:00.0", "0000:05:00.0", "0000:06:00.0"])]

    def nvme_smart(self, ctrl):
        i = int(ctrl[4:])
        return {"passed": True, "critical_warning": 0, "temp": 36 + i, "spare": 100, "spare_thresh": 10, "used_pct": 2 + i,
                "tb_written": 40.0 + i * 7, "hours": 20000 + i * 100, "unsafe_shutdowns": 30 + i, "media_errors": 0, "err_log": i,
                "capacity_gb": 1024 if i in (2, 4) else 512, "temp_sensors": [36 + i, 40 + i]}

    def fault(self, slot, on):
        self.faults = getattr(self, "faults", set())
        (self.faults.add if on else self.faults.discard)(slot)
        return True

    def fault_state(self, slot):
        return slot in getattr(self, "faults", set())

    def zpool(self, path):
        vd = {"/dev/sd%s1" % chr(97 + i): {"name": "/dev/sd%s1" % chr(97 + i), "read_errors": "0", "write_errors": "0",
                                            "checksum_errors": "3" if i == 13 else "0"} for i in range(24) if i != 22}
        vd["/dev/sdw1"] = {"name": "/dev/sdw1", "state": "REMOVED", "read_errors": "0", "write_errors": "0", "checksum_errors": "0"}
        for k in vd:
            vd[k].setdefault("state", "ONLINE")
        return {"pools": {"TENTBPOOL": {"vdevs": {"TENTBPOOL": {"vdev_type": "root", "vdevs": {"raidz2-0": {"vdev_type": "raidz", "vdevs": vd}}}}}}}, time.time(), {}, {}

    def set_duty(self, zone, pct):
        self.duty[int(zone)] = pct

    def get_duty(self, zone):
        return self.duty[int(zone)]

    def ensure_full_mode(self):
        return False

    def locate(self, slot, on):
        (self.leds.add if on else self.leds.discard)(slot)
        return True

    def locate_state(self, slot):
        return slot in self.leds


# ---------------------------------------------------------------- controller
class Controller:
    def __init__(self, hw, data, cfg_path, layout_path, logs=None):
        self.hw, self.data = hw, data
        self.cfg_path, self.layout_path = cfg_path, layout_path
        raw = load_json(cfg_path, DEFAULT_CONFIG)
        self.cfg_mtime = os.path.getmtime(cfg_path) if os.path.exists(cfg_path) else 0
        self.safety = load_json(os.path.join(data, "safety.json"), DEFAULT_SAFETY)
        for k, v in DEFAULT_SAFETY.items():   # deep merge: a partial safety.json can't drop default limits
            if isinstance(v, dict) and isinstance(self.safety.get(k), dict):
                for kk, vv in v.items():
                    self.safety[k].setdefault(kk, vv)
            else:
                self.safety.setdefault(k, v)
        e, w, cfg = load_file_config(raw, self.safety)
        if e:
            log("WARNING: config.json violates safety, running the BUILT-IN curves instead: %s" % "; ".join(e))
            notify("cfg-invalid", "config.json rejected - running built-in curves", "; ".join(e))
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        self.cfg = cfg
        tp = os.path.join(data, "edit_token")
        if not os.path.exists(tp):
            import secrets
            with open(tp, "w") as f:
                f.write(secrets.token_urlsafe(18) + "\n")
            own(tp)
            os.chmod(tp, 0o640)
        self.token_path = tp
        self.emergency = None
        self.emerg_active = {}            # sensor -> message; released per sensor, never on a missing sample
        self.led_lock = threading.RLock()  # leds.json / SES fault LEDs: drive-scan thread vs HTTP threads
        self.alert_keys = []
        self.set_err = None
        self.last_scan_ok = time.time()
        self.backup_dir = os.path.join(data, "backups")
        os.makedirs(self.backup_dir, exist_ok=True)
        own(self.backup_dir)
        self.layout = load_json(layout_path, DEFAULT_LAYOUT)
        for k, v in DEFAULT_LAYOUT.items():   # new layout keys added after the file was first written
            self.layout.setdefault(k, v)
        for k, v in DEFAULT_LAYOUT["sensors"].items():
            self.layout["sensors"].setdefault(k, v)
        if not any(f.get("header") == "GPU" for f in self.layout.get("fans", [])):
            self.layout.setdefault("fans", []).append([f for f in DEFAULT_LAYOUT["fans"] if f["header"] == "GPU"][0])
        self.lock = threading.Lock()
        self.duty = {z: None for z in self.cfg["zones"]}
        self.reason = {z: "" for z in self.cfg["zones"]}
        self.state = {"ok": True, "failsafe": None, "alerts": []}
        self.bmc_data, self.nvme, self.tctl = {}, {}, None
        self.drives = []  # [{slot, dev, temp, status, smart, zfs, health, reasons, locate, fault}]
        self.smart_cache = {}
        self.nvme_drives = []
        self.hbas, self.ses = [], []
        self.hba_err = None
        self.gpus, self.gpu_err = [], None
        self.platform = {"chassis": {}, "bmc": {}, "sel_info": {}, "sel": []}
        self.last_platform, self.last_static, self.sel_count = 0, 0, None
        self.thresh_alerted = set()
        self.zfs_age = None
        self.slotmap = load_json(os.path.join(data, "slotmap.json"), {})
        self.leds = load_json(os.path.join(data, "leds.json"), {"manual": {}, "auto": {}})
        self.fail_count = 0
        self.last_hdd = 0
        self.last_reassert = 0
        self.dimm_hot_since = None
        self.hist_dir = os.path.join(logs or data, "history")
        os.makedirs(self.hist_dir, exist_ok=True)
        own(self.hist_dir)
        self.db = histdb.HistDB(os.path.join(logs or data, "history.db"), own)
        if self.db.new:
            n = self.db.import_jsonl(self.hist_dir)
            log("history.db created; imported %d samples from JSONL" % n)
        self.last_rollup = 0
        self.last_drive_db = 0

    # ---- history
    def write_history(self, row):
        vals = dict(row["s"])
        for z, d in row["d"].items():
            vals["duty%s" % z] = d
        if time.time() - self.last_drive_db >= self.cfg.get("hdd_interval_s", 60):
            for d in self.drives:            # per-drive temps by serial (survive sdX renames)
                if d.get("serial") and d.get("temp") is not None:
                    vals["hdd:" + d["serial"]] = d["temp"]
            for n in self.nvme_drives:
                if n.get("serial") and n.get("temp") is not None:
                    vals["nvme:" + n["serial"]] = n["temp"]
            for h in self.hbas:
                if h.get("ioc_temp_c") is not None:
                    vals["hba:" + h.get("pci", "")] = h["ioc_temp_c"]
            self.last_drive_db = time.time()
        try:
            self.db.add(row["t"], vals)
            if time.time() - self.last_rollup >= 60:
                self.db.rollup()
                self.last_rollup = time.time()
        except Exception as e:
            log("history.db write failed: %s" % e)
        try:   # a full /logs must never stop the fan loop
            fn = os.path.join(self.hist_dir, time.strftime("%Y-%m-%d") + ".jsonl")
            new = not os.path.exists(fn)
            with open(fn, "a") as f:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
            if new:
                own(fn)
            keep = self.cfg.get("history_days", 14)
            files = sorted(glob.glob(self.hist_dir + "/*.jsonl"))
            for old in files[:-keep]:
                os.remove(old)
        except OSError as e:
            log("history JSONL write failed: %s" % e)

    # ---- sensor values usable by curves
    def value(self, name):
        if name == "hdd_max":
            v = [d["temp"] for d in self.drives if d.get("temp") is not None]
            return max(v) if v else None
        if name == "nvme_max":
            v = [n["value"] for n in self.nvme.values()]
            return max(v) if v else None
        if name == "Tctl":
            return self.tctl
        if name == "gpu_temp":
            v = [g["temp"] for g in self.gpus if g.get("temp") is not None]
            return max(v) if v else None
        if name == "hba_max":
            v = [h["ioc_temp_c"] for h in self.hbas if h.get("ioc_temp_c") is not None]
            return max(v) if v else None
        if name == "ses_max":
            return max(self.ses) if self.ses else None
        s = self.bmc_data.get(name)
        return s["value"] if s else None

    def reload_cfg(self):
        try:
            m = os.path.getmtime(self.cfg_path)
            if m != self.cfg_mtime:
                self.cfg_mtime = m
                with open(self.cfg_path) as f:
                    new = json.load(f)
                e, w, cfg = load_file_config(new, self.safety)
                if e:
                    log("config.json edit REJECTED by safety, keeping running config: %s" % "; ".join(e))
                    notify("cfg-rejected", "config.json edit rejected", "; ".join(e))
                    return
                with self.lock:
                    self.cfg = cfg
                log("config reloaded from file" + (" (warnings: %s)" % "; ".join(w) if w else ""))
        except (OSError, ValueError) as e:
            log("config reload failed, keeping old: %s" % e)

    def check_token(self, t):
        try:
            with open(self.token_path) as f:
                want = f.read().strip()
        except OSError:
            return False
        if len(want) < 16 or not isinstance(t, str):   # an empty/truncated token file locks everything
            return False
        return hmac.compare_digest(t.strip().encode(), want.encode())

    def backups(self):
        return sorted((os.path.basename(p) for p in glob.glob(self.backup_dir + "/config-*.json")), reverse=True)

    def apply_config(self, new_cfg, who):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bp = os.path.join(self.backup_dir, "config-%s.json" % stamp)
        with open(bp, "w") as f:
            json.dump(self.cfg, f, indent=2)
        own(bp)
        for old in self.backups()[20:]:
            os.remove(os.path.join(self.backup_dir, old))
        changes = []
        for z, zc in new_cfg["zones"].items():
            oz = self.cfg["zones"][z]
            if oz["floor"] != zc["floor"]:
                changes.append("zone %s floor %s->%s" % (z, oz["floor"], zc["floor"]))
            if oz["curves"] != zc["curves"]:
                changes.append("zone %s curves changed" % z)
        for k in ("hysteresis_c", "ramp_down_pct_per_min"):
            if self.cfg.get(k) != new_cfg.get(k):
                changes.append("%s %s->%s" % (k, self.cfg.get(k), new_cfg.get(k)))
        tmp = self.cfg_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(new_cfg, f, indent=2)
        own(tmp)
        os.replace(tmp, self.cfg_path)
        self.cfg = new_cfg
        self.cfg_mtime = os.path.getmtime(self.cfg_path)
        log("config applied (%s): %s; backup %s" % (who, ", ".join(changes) or "no effective change", os.path.basename(bp)))

    def demand(self, cfg):
        """Instant per-zone demand for a config (no ramp/hysteresis) - used for previews."""
        out = {}
        for z, zc in cfg["zones"].items():
            best, why, per = zc["floor"], "floor", {}
            for c in zc["curves"]:
                v = self.value(c["sensor"])
                if v is None:
                    per[c["sensor"]] = None
                    if not c.get("optional"):
                        best, why = cfg.get("failsafe_duty", 100), "FAILSAFE %s missing" % c["sensor"]
                    continue
                d = interp(c["points"], v)
                per[c["sensor"]] = round(d, 1)
                if d > best:
                    best, why = d, "%s %.0f°C" % (c["sensor"], v)
            out[z] = {"duty": int(math.ceil(max(zc["floor"], min(100, best)))), "why": why, "per_curve": per}
        return out

    def check_emergency(self):
        S = self.safety
        for name, lim in S.get("emergency", {}).items():
            v = self.value(name)
            if v is None or not isinstance(lim, (int, float)):
                continue   # a missing sample never releases an active emergency
            if name in self.emerg_active:
                if v < lim - S.get("emergency_release_c", 5):
                    del self.emerg_active[name]
                else:
                    self.emerg_active[name] = "%s %.0f°C (trip %s)" % (name, v, lim)
            elif v >= lim:
                self.emerg_active[name] = "%s %.0f°C >= %s" % (name, v, lim)
        hits = list(self.emerg_active.values())
        if S.get("emergency_on_bmc_critical", True):
            for name, s in self.bmc_data.items():
                if s.get("unit") == "degrees C" and (s.get("status") or "").lower() in ("cr", "nr"):
                    hits.append("%s BMC %s" % (name, s["status"]))
        return hits

    def read_all(self):
        bmc = self.hw.bmc()
        nv, tctl = self.hw.hwmon()
        try:
            hb = self.hw.hba()
            if self.hba_err:
                log("HBA temperature reads recovered")
            self.hba_err = None
        except Exception as e:      # optional sensor: never failsafe on it
            hb = []
            if str(e) != self.hba_err:
                log("HBA temperature read failed: %s" % e)
            self.hba_err = str(e)
        try:
            gp = self.hw.gpu()
            if self.gpu_err:
                log("nvidia-smi recovered")
            self.gpu_err = None
        except Exception as e:      # optional: never failsafe on it
            gp = []
            if str(e) != self.gpu_err:
                log("nvidia-smi failed: %s" % e)
            self.gpu_err = str(e)
        with self.lock:
            self.bmc_data, self.nvme, self.tctl, self.hbas, self.gpus = bmc, nv, tctl, hb, gp

    def slow_loop(self, stop):
        """SMART/SES/ZFS drive scan + BMC platform info, in their own thread: a hung smartctl or sg_ses can never
        delay the 10 s fan loop."""
        while not stop.is_set():
            now = time.time()
            if now - self.last_hdd >= self.cfg["hdd_interval_s"]:
                self.last_hdd = now
                try:
                    self.scan_drives()
                    self.last_scan_ok = time.time()
                except Exception as e:
                    log("drive scan failed: %r" % e)
            if now - self.last_platform >= 60:
                self.last_platform = now
                try:
                    self.read_platform(now)
                except Exception as e:
                    log("platform read failed: %r" % e)
            stop.wait(2)

    def read_platform(self, now):
        try:
            ch = self.hw.chassis()
            if now - self.last_static >= 3600 or not self.platform["bmc"]:
                self.platform["bmc"] = self.hw.bmc_static()
                self.last_static = now
            si = self.hw.sel_info()
            if si.get("entries") != self.sel_count:
                sel = self.hw.sel()
                if self.sel_count is not None:
                    known = {e["id"] for e in self.platform["sel"]}
                    for e in sel:
                        if e["id"] not in known:
                            log("SEL: %s %s %s %s" % (e["sensor"], e["event"], e["dir"], e["detail"]))
                self.platform["sel"] = sel
                self.sel_count = si.get("entries")
            with self.lock:
                self.platform["chassis"], self.platform["sel_info"] = ch, si
        except Exception as e:
            log("platform read failed: %s" % e)

    # ---- drives: SES slot + SMART + ZFS -> health, fault LED
    def scan_drives(self):
        H = self.cfg.get("health", {})
        zmap, zmissing, zage = {}, [], None
        try:
            zj, mt, parts, pser = self.hw.zpool(self.cfg.get("zpool_json", "/logs/zpool.json"))
            pm = self.slotmap.setdefault("_parts", {})
            ps = self.slotmap.setdefault("_pserial", {})
            for pu, dv in parts.items():
                pm[pu] = dv
            for pu, sn in pser.items():
                if sn:
                    ps[pu] = sn
            zmap, zmissing = zfs_members(zj, dict(pm))
            zage = time.time() - mt
        except (OSError, ValueError) as e:
            zage = None
        drives = []
        for s in self.hw.slots():
            slot, dev = s["slot"], s["dev"]
            d = {"slot": slot, "dev": dev, "temp": None, "status": "empty", "smart": None, "zfs": None,
                 "health": "empty", "reasons": []}
            prev = self.smart_cache.get(dev) if dev else None
            if dev:
                st, sm = self.hw.hdd_smart(dev)
                if sm is None and prev:
                    sm = prev  # standby / error: keep the last good SMART read
                if sm:
                    self.smart_cache[dev] = sm
                d.update(status=st, smart=sm, temp=(sm or {}).get("temp") if st == "active" else None,
                         serial=(sm or {}).get("serial"), model=(sm or {}).get("model"))
                d["zfs"] = zmap.get(dev)
                self.slotmap[str(slot)] = {"dev": dev, "serial": d.get("serial"), "t": int(time.time())}
            d["health"], d["reasons"] = self.classify(d, H)
            d["locate"] = self.hw.locate_state(slot)
            d["fault"] = self.hw.fault_state(slot)
            drives.append(d)
        # pool members that vanished: find their last-known slot
        for m in zmissing:
            dv = self.slotmap.get("_parts", {}).get(m.get("partuuid")) or re.sub(r"^/dev/|p?\d+$", "", m["path"])
            sn = self.slotmap.get("_pserial", {}).get(m.get("partuuid"))
            for sl, info in self.slotmap.items():   # match by serial first: sdX letters shift across reboots
                if sl.isdigit() and ((sn and info.get("serial") == sn) or (not sn and info.get("dev") == dv)):
                    for d in drives:
                        if d["slot"] == int(sl) and not d["dev"]:
                            d.update(health="fail", zfs=m, serial=info.get("serial"),
                                     reasons=["missing from %s (%s, last seen as %s)" % (m["pool"], m["state"], dv)])
        self.save_json("slotmap.json", self.slotmap)
        prevh = {d["slot"]: d["health"] for d in self.drives}
        for d in drives:
            ph = prevh.get(d["slot"])
            if ph is not None and ph != d["health"] and "fail" in (ph, d["health"]):
                what = "FAILED" if d["health"] == "fail" else "recovered (%s)" % d["health"]
                notify("bay%d" % d["slot"], "Drive bay %d %s" % (d["slot"], what),
                       "Bay %d (%s, serial %s): %s\n%s" % (d["slot"], d.get("dev") or "empty", d.get("serial") or "?", what, "; ".join(d["reasons"])), 0)
        try:
            self.ses = self.hw.ses_temps()
        except Exception as e:
            self.ses = []
        with self.led_lock:
            self.apply_fault_leds(drives)
        nvs = []
        for n in self.hw.nvme_list():
            sm = self.hw.nvme_smart(n["ctrl"]) if n.get("state") == "live" else None
            d = dict(n, smart=sm, temp=(sm or {}).get("temp"), zfs=zmap.get(n["dev"]) if n.get("dev") else None)
            d["health"], d["reasons"] = self.classify_nvme(d, H)
            nvs.append(d)
        old = {n["address"]: n["health"] for n in self.nvme_drives}
        for n in nvs:
            if old.get(n["address"], "ok") != n["health"]:
                log("NVMe %s (%s %s) -> %s: %s" % (n["address"], n["ctrl"], n["serial"], n["health"], "; ".join(n["reasons"]) or "ok"))
                if self.nvme_drives and "fail" in (old.get(n["address"], "ok"), n["health"]):
                    notify("nvme" + n["address"], "NVMe %s %s" % (n["address"], n["health"].upper()),
                           "%s %s (%s): %s" % (n["ctrl"], n["serial"], n["address"], "; ".join(n["reasons"]) or "ok"), 0)
        with self.lock:
            self.drives = drives
            self.nvme_drives = nvs
            self.zfs_age = zage

    def classify_nvme(self, d, H):
        sm, z, r, lvl = d.get("smart") or {}, d.get("zfs"), [], 0
        def bad(level, msg):
            nonlocal lvl
            lvl = max(lvl, level)
            r.append(msg)
        if d.get("state") != "live":
            bad(2, "controller state %s" % d.get("state"))
        if sm.get("passed") is False:
            bad(2, "SMART overall health FAILED")
        cw = sm.get("critical_warning")
        if cw:
            bits = [n for b, n in ((1, "spare below threshold"), (2, "temperature"), (4, "reliability degraded"),
                                   (8, "read-only"), (16, "volatile backup failed"), (32, "PMR read-only")) if cw & b]
            bad(2, "NVMe critical warning 0x%02x: %s" % (cw, ", ".join(bits)))
        if z:
            if z["state"] in ("FAULTED", "UNAVAIL", "REMOVED", "OFFLINE"):
                bad(2, "ZFS %s in %s/%s" % (z["state"], z["pool"], z["vdev"]))
            elif z["state"] != "ONLINE":
                bad(1, "ZFS %s" % z["state"])
            if z["read"] + z["write"] + z["cksum"] >= H.get("zfs_errors_warn", 1):
                bad(1, "ZFS errors r/w/ck %d/%d/%d" % (z["read"], z["write"], z["cksum"]))
        t = d.get("temp")
        if t is not None:
            if t >= H.get("nvme_temp_fail", 80):
                bad(2, "temperature %d°C" % t)
            elif t >= H.get("nvme_temp_warn", 70):
                bad(1, "temperature %d°C" % t)
        return ("ok", "warn", "fail")[lvl], r

    def classify(self, d, H):
        if not d["dev"]:
            return "empty", []
        sm, z, r, lvl = d.get("smart") or {}, d.get("zfs"), [], 0
        def bad(level, msg):
            nonlocal lvl
            lvl = max(lvl, level)
            r.append(msg)
        if sm.get("passed") is False:
            bad(2, "SMART overall health FAILED")
        for a in sm.get("attr_failing_now") or []:
            bad(2, "SMART attribute below threshold now: %s" % a)
        for a in sm.get("attr_failed_past") or []:
            bad(1, "SMART attribute was below threshold in the past: %s" % a)
        if z:
            if z["state"] in ("FAULTED", "UNAVAIL", "REMOVED", "OFFLINE"):
                bad(2, "ZFS %s in %s/%s" % (z["state"], z["pool"], z["vdev"]))
            elif z["state"] != "ONLINE":
                bad(1, "ZFS %s" % z["state"])
            errs = z["read"] + z["write"] + z["cksum"]
            if errs >= H.get("zfs_errors_warn", 1):
                bad(1, "ZFS errors r/w/ck %d/%d/%d" % (z["read"], z["write"], z["cksum"]))
        t = d.get("temp")
        if t is not None:
            if t >= H.get("temp_fail", 55):
                bad(2, "temperature %d°C" % t)
            elif t >= H.get("temp_warn", 45):
                bad(1, "temperature %d°C" % t)
        for k, lim, label in (("pending", "pending_warn", "pending sectors"), ("offline_unc", "uncorrectable_warn", "offline uncorrectable"),
                              ("reported_unc", "uncorrectable_warn", "reported uncorrectable"), ("realloc", "realloc_warn", "reallocated sectors"),
                              ("crc", "crc_warn", "UDMA CRC errors (cable/backplane)"), ("grown_defects", "grown_defects_warn", "grown defects"),
                              ("read_unc", "uncorrectable_warn", "uncorrected read errors"), ("write_unc", "uncorrectable_warn", "uncorrected write errors"),
                              ("verify_unc", "uncorrectable_warn", "uncorrected verify errors")):
            v, th = sm.get(k), H.get(lim)
            if v is not None and th is not None and v >= th:
                bad(1, "%s: %d" % (label, v))
        if d["status"] == "error":
            bad(1, "SMART read failed")
        return ("ok", "warn", "fail")[lvl], r

    def apply_fault_leds(self, drives):
        auto = self.cfg.get("auto_fault_led", True)
        for d in drives:
            sl = str(d["slot"])
            want_auto = auto and d["health"] == "fail"
            manual = self.leds.get("manual", {}).get(sl)
            if manual is not None:
                want = manual
            elif want_auto:
                want = True
            elif sl in self.leds.get("auto", {}):
                want = False
            else:
                continue
            if want != d["fault"]:
                try:
                    self.hw.fault(d["slot"], want)
                    d["fault"] = want
                    log("fault LED bay %s %s (%s)" % (sl, "ON" if want else "off",
                        "manual" if manual is not None else ("; ".join(d["reasons"]) or "recovered")))
                except OSError as e:
                    log("fault LED bay %s failed: %s" % (sl, e))
            if want_auto and manual is None:
                self.leds.setdefault("auto", {})[sl] = True
            elif sl in self.leds.get("auto", {}) and not want_auto:
                self.leds["auto"].pop(sl, None)
        self.save_json("leds.json", self.leds)

    def save_json(self, name, obj):
        p = os.path.join(self.data, name)
        tmp = "%s.%d.tmp" % (p, threading.get_ident())
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=1)
        own(tmp)
        os.replace(tmp, p)

    def compute(self, dt):
        cfg = self.cfg
        hyst, ramp = cfg["hysteresis_c"], cfg["ramp_down_pct_per_min"]
        new, failsafe = {}, None
        for z, zc in cfg["zones"].items():
            up, down, why, dwhy = zc["floor"], zc["floor"], "floor", None
            for c in zc["curves"]:
                v = self.value(c["sensor"])
                if v is None:
                    if c.get("optional"):
                        continue
                    failsafe = "zone %s: sensor %s missing" % (z, c["sensor"])
                    up = down = cfg["failsafe_duty"]
                    why = "FAILSAFE " + c["sensor"] + " missing"
                    break
                u = interp(c["points"], v)
                # hysteresis: hold a higher duty until v is hyst °C below where it was reached - but below the
                # curve's first point the curve asks for nothing, so it must not hold the zone above its floor
                d = zc["floor"] if v <= c["points"][0][0] else interp(c["points"], v + hyst)
                if u > up:
                    up, why = u, "%s %.0f°C" % (c["sensor"], v)
                if d > down:
                    down, dwhy = d, "%s %.0f°C" % (c["sensor"], v)
            cur = self.duty[z] if self.duty[z] is not None else zc["floor"]
            if up > cur:
                tgt = up
            elif down < cur:
                tgt = max(down, cur - ramp * dt / 60.0)
                why = why if up > zc["floor"] else ("ramping down" if tgt > zc["floor"] else "floor")
            else:
                tgt = cur
                if why == "floor" and cur > zc["floor"]:
                    why = "hysteresis hold (%s)" % (dwhy or "?")
            new[z] = (max(zc["floor"], min(zc["max"], tgt)), why)
        return new, failsafe

    def apply(self, z, duty, force=False):
        d = int(math.ceil(duty))
        prev = self.duty[z]
        if force or prev is None or int(math.ceil(prev)) != d:
            if not self.cfg.get("dry_run"):
                try:
                    self.hw.set_duty(z, d)
                except Exception as e:   # keep the old duty so the next loop retries
                    if self.set_err != str(e):
                        log("set_duty FAILED, will retry: %s" % e)
                        notify("set_duty", "fanctl could not set a fan duty", str(e))
                    self.set_err = str(e)
                    return False
                if self.set_err:
                    log("set_duty recovered")
                    self.set_err = None
        self.duty[z] = duty
        return True

    def failsafe_all(self, why):
        for z in self.cfg["zones"]:
            self.duty[z] = self.cfg["failsafe_duty"]
            self.reason[z] = "FAILSAFE: " + why
            if not self.cfg.get("dry_run"):
                try:
                    self.hw.set_duty(z, self.cfg["failsafe_duty"])
                except Exception:
                    pass
        if self.state["failsafe"] != why:
            log("FAILSAFE -> %d%%: %s" % (self.cfg["failsafe_duty"], why))
            notify("failsafe", "FAILSAFE: fans at %d %%" % self.cfg["failsafe_duty"], why)
        self.state["failsafe"] = why

    def check_alerts(self):
        a = self.cfg.get("alert", {})
        dimms = [self.value("P1_DIMMA~D Temp"), self.value("P1_DIMME~H Temp")]
        hot = any(v is not None and v >= a.get("dimm_c", 80) for v in dimms)
        now = time.time()
        alerts = []
        keys = []   # log/notify only when the SET of alerts changes, not on every changing number
        if hot:
            self.dimm_hot_since = self.dimm_hot_since or now
            if now - self.dimm_hot_since >= a.get("hold_s", 120):
                alerts.append("DIMMs >= %s°C for %ds (A-D %s, E-H %s)" % (a.get("dimm_c"), now - self.dimm_hot_since, dimms[0], dimms[1]))
                keys.append("dimm")
        else:
            self.dimm_hot_since = None
        # any BMC threshold sensor outside its window (status != ok) -> alert
        bad, badn = [], []
        for name, s in self.bmc_data.items():
            st = (s.get("status") or "").lower()
            if s.get("unit") != "discrete" and s.get("value") is not None and st not in ("ok", "na", ""):
                bad.append("%s %s (%s)" % (name, s["value"], st))
                badn.append("%s:%s" % (name, st))
        if bad:
            alerts.append("BMC sensor out of range: " + ", ".join(bad))
            keys += sorted(badn)
        age = now - self.last_scan_ok
        if age > 600:
            alerts.append("drive scan stale: no SMART/SES update for %d min" % (age // 60))
            keys.append("scan")
        if keys != self.alert_keys:
            if alerts:
                log("ALERT " + "; ".join(alerts))
                notify("alert:" + "|".join(keys), "Thermal/BMC alert", "\n".join(alerts))
            elif self.alert_keys:
                log("alerts cleared")
        self.alert_keys = keys
        self.state["alerts"] = alerts

    def loop(self, stop):
        log("controller start (dry_run=%s)" % self.cfg.get("dry_run"))
        self.hw.ensure_full_mode()
        threading.Thread(target=self.slow_loop, args=(stop,), daemon=True, name="drives").start()
        last = time.time()
        while not stop.is_set():
            t0 = time.time()
            dt = min(t0 - last, 2 * self.cfg["interval_s"])   # never ramp down a backlog after failed reads
            last = t0
            self.reload_cfg()
            try:
                self.read_all()
                self.fail_count = 0
            except Exception as e:
                self.fail_count += 1
                log("read failed (%d): %s" % (self.fail_count, e))
                if self.fail_count >= self.cfg["read_fail_limit"]:
                    self.failsafe_all("sensor reads failing: %s" % e)
                stop.wait(self.cfg["interval_s"])
                continue
            new, fs = self.compute(dt)
            em = self.check_emergency()
            if em:
                if not self.emergency:
                    log("EMERGENCY -> all zones 100%%: %s" % "; ".join(em))
                    notify("emergency", "EMERGENCY: fans forced to 100 %", "fanctl emergency override: " + "; ".join(em), 600)
                self.emergency = "; ".join(em)
                new = {z: (100, "EMERGENCY " + self.emergency) for z in new}
            elif self.emergency:
                log("emergency cleared")
                notify("emergency-clear", "Emergency cleared", "fanctl emergency override released; curves back in control.", 600)
                self.emergency = None
            force = t0 - self.last_reassert >= self.cfg["reassert_s"]
            if force:
                if self.hw.ensure_full_mode():
                    force = True
                self.last_reassert = t0
            with self.lock:
                for z, (d, why) in new.items():
                    old = self.duty[z]
                    self.apply(z, d, force)
                    if old is None or abs(int(math.ceil(old)) - int(math.ceil(d))) >= 5:
                        log("zone %s -> %d%% (%s)" % (z, math.ceil(d), why))
                    self.reason[z] = why
                if force and not self.cfg.get("dry_run"):
                    for z in new:
                        got, want = self.hw.get_duty(z), int(math.ceil(self.duty[z])) if self.duty[z] is not None else None
                        if got is not None and want is not None and got != want:
                            log("zone %s readback %d%% != %d%%, re-setting" % (z, got, want))
                            self.apply(z, self.duty[z], True)
                if fs:
                    if self.state["failsafe"] != fs:
                        log("FAILSAFE " + fs)
                        notify("failsafe", "FAILSAFE: zone at 100 %", fs)
                    self.state["failsafe"] = fs
                elif self.state["failsafe"]:
                    log("failsafe cleared")
                    notify("failsafe-clear", "Failsafe cleared", "fanctl sensors readable again; curves back in control.")
                    self.state["failsafe"] = None
                self.check_alerts()
            row = {"t": round(t0), "d": {z: int(math.ceil(v)) for z, v in self.duty.items()},
                   "s": {k: v["value"] for k, v in self.bmc_data.items() if v.get("value") is not None}}
            row["s"]["Tctl"] = self.tctl
            row["s"]["hdd_max"] = self.value("hdd_max")
            row["s"]["nvme_max"] = self.value("nvme_max")
            row["s"]["hba_max"] = self.value("hba_max")
            row["s"]["gpu_temp"] = self.value("gpu_temp")
            row["s"]["gpu_fan"] = self.gpus[0]["fan_pct"] if self.gpus else None
            row["s"]["ses_max"] = self.value("ses_max")
            self.write_history(row)
            heartbeat()
            stop.wait(max(1, self.cfg["interval_s"] - (time.time() - t0)))

    def restore_floors(self):
        if self.cfg.get("dry_run"):
            return
        # never LOWER the fans on the way out: during an emergency/failsafe/hot spell they stay where they are
        left = {}
        for z, zc in self.cfg["zones"].items():
            cur = self.duty.get(z)
            d = max(zc["floor"], int(math.ceil(cur)) if cur is not None else 100)
            left[z] = d
            try:
                self.hw.set_duty(z, d)
            except Exception:
                pass
        log("stopped: zones left at %s (floors %s)" % (left, {z: zc["floor"] for z, zc in self.cfg["zones"].items()}))

    # ---- API views
    def snapshot(self):
        with self.lock:
            zones = {}
            for z, zc in self.cfg["zones"].items():
                zones[z] = {"name": zc["name"], "duty": None if self.duty[z] is None else int(math.ceil(self.duty[z])),
                            "floor": zc["floor"], "reason": self.reason[z],
                            "fans": {f: (self.bmc_data.get(f) or {}).get("value") for f in zc["fans"]},
                            "curves": zc["curves"],
                            "inputs": {c["sensor"]: self.value(c["sensor"]) for c in zc["curves"]}}
            sensors = []
            lay = self.layout["sensors"]
            for name, s in self.bmc_data.items():
                if name.startswith("FAN") or (s.get("unit") not in ("degrees C", "") ) or (s.get("unit") == "" and "Temp" not in name):
                    continue
                p = lay.get(name, {})
                sensors.append({"name": name, "value": s.get("value"), "status": s.get("status"),
                                "unc": s.get("unc"), "ucr": s.get("ucr"), "x": p.get("x"), "y": p.get("y"),
                                "loc": p.get("loc", ""), "src": "BMC"})
            if self.tctl is not None:
                p = lay.get("Tctl", {})
                sensors.append({"name": "Tctl", "value": round(self.tctl, 1), "status": "ok", "unc": None, "ucr": None,
                                "x": p.get("x"), "y": p.get("y"), "loc": p.get("loc", "CPU"), "src": "k10temp"})
            for k, n in sorted(self.nvme.items()):
                p = lay.get(k, {})
                sensors.append({"name": k, "label": "%s %s" % (n["dev"], n["serial"]), "value": round(n["value"], 1),
                                "status": "ok", "unc": 70, "ucr": 80, "x": p.get("x"), "y": p.get("y"),
                                "loc": p.get("loc", "NVMe"), "src": "NVMe"})
            for g in self.gpus:
                k = "gpu:" + g["pci"]
                p = lay.get(k, lay.get("gpu:0000:81:00.0", {}))
                sensors.append({"name": k, "label": "%s (nvidia-smi)" % g["name"].replace("NVIDIA ", ""), "value": g["temp"],
                                "status": "ok", "unc": 83, "ucr": 93, "x": p.get("x"), "y": p.get("y"),
                                "loc": "GPU · fan %s%% · %s W · util %s%%" % (fmt0(g["fan_pct"]), fmt0(g["power_w"]), fmt0(g["util_pct"])),
                                "src": "GPU"})
            for h in self.hbas:
                k = "hba:" + h.get("pci", "")
                p = lay.get(k, {})
                sensors.append({"name": k, "label": "HBA %s %s" % (h.get("board"), h.get("pci", "").replace("0000:", "")),
                                "value": h.get("ioc_temp_c"), "status": "ok" if h.get("ioc_temp_c") is not None else "na",
                                "unc": 85, "ucr": 95, "x": p.get("x"), "y": p.get("y"),
                                "loc": p.get("loc", "PCIe HBA") + ("" if h.get("ioc_temp_c") is not None else " · no sensor"),
                                "src": "HBA"})
            names = ["Backplane SES sensor 1 (ambient)", "Backplane SES sensor 2 (expander chip)"]
            for i, t in enumerate(self.ses):
                k = "ses:%d" % i
                p = lay.get(k, {})
                sensors.append({"name": k, "label": names[i] if i < len(names) else "Backplane SES sensor %d" % (i + 1),
                                "value": t, "status": "ok", "unc": 80 if i else 45, "ucr": 90 if i else 55,
                                "x": p.get("x"), "y": p.get("y"), "loc": p.get("loc", "Backplane"), "src": "SES"})
            bays = self.layout["bays"]
            by_slot = {d["slot"]: d for d in self.drives}
            for c in range(bays["cols"]):
                col = [by_slot.get(bays["order"][r][c], {}).get("temp") for r in range(bays["rows"])]
                col = [v for v in col if v is not None]
                key = "hdd_col%d" % c
                p = lay.get(key, {})
                if col:
                    sensors.append({"name": key, "label": "HDD column %d max" % (c + 1), "value": round(max(col), 1), "status": "ok",
                                    "unc": 45, "ucr": 50, "x": p.get("x"), "y": p.get("y"), "loc": p.get("loc", ""), "src": "SMART"})
            return {"t": time.time(), "zones": zones, "sensors": sensors, "drives": self.drives,
                    "failsafe": self.state["failsafe"], "alerts": self.state["alerts"],
                    "dry_run": bool(self.cfg.get("dry_run")), "events": list(EVENTS)[-40:],
                    "rails": [{"name": k, "value": v["value"], "status": v.get("status"), "lcr": v.get("lcr"), "lnc": v.get("lnc"),
                               "unc": v.get("unc"), "ucr": v.get("ucr")} for k, v in self.bmc_data.items() if v.get("unit") == "Volts"],
                    "discrete": {k: {"raw": v.get("raw") or v.get("status"), "status": v.get("status")}
                                 for k, v in self.bmc_data.items() if v.get("unit") == "discrete"},
                    "platform": self.platform,
                    "gpus": self.gpus, "nvme": self.nvme_drives, "zfs_age": self.zfs_age, "auto_fault_led": self.cfg.get("auto_fault_led", True),
                    "layout": self.layout, "enclosure": os.path.basename(self.hw.enc or "") if self.hw.enc else None}

    def hist(self, minutes, names=None):
        return self.db.query(minutes, names)


# ---------------------------------------------------------------- HTTP
def make_handler(ctl):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            path, _, q = self.path.partition("?")
            if path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self.send(200, ctl.snapshot())
            if path == "/api/history":
                m = re.search(r"minutes=(\d+)", q)
                nm = re.search(r"series=([^&]*)", q)
                from urllib.parse import unquote
                names = [x for x in unquote(nm.group(1)).split(",") if x] if nm else None   # decode first: %2C commas too
                return self.send(200, ctl.hist(min(int(m.group(1)) if m else 60, 730 * 1440), names))
            if path == "/api/history/series":
                return self.send(200, {"series": ctl.db.names(), "stats": ctl.db.stats()})
            if path == "/api/config":
                return self.send(200, {"config": ctl.cfg, "safety": ctl.safety, "inputs": CURVE_INPUTS,
                                       "values": {k: ctl.value(k) for k in CURVE_INPUTS},
                                       "demand": ctl.demand(ctl.cfg), "backups": ctl.backups()})
            if path.startswith("/api/config/backup/"):
                name = os.path.basename(path)
                p = os.path.join(ctl.backup_dir, name)
                if not re.match(r"^config-\d{8}-\d{6}\.json$", name) or not os.path.exists(p):
                    return self.send(404, {"error": "no such backup"})
                with open(p) as f:
                    return self.send(200, json.load(f))
            self.send(404, {"error": "not found"})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= 1000000:
                return self.send(413, {"error": "body too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self.send(400, {"error": "bad json"})
            if not isinstance(body, dict):
                return self.send(400, {"error": "body must be a JSON object"})
            if self.path in ("/api/locate", "/api/fault", "/api/layout") and not ctl.check_token(body.get("token")):
                log("%s REFUSED: bad edit token from %s" % (self.path, self.client_address[0]))
                return self.send(403, {"ok": False, "errors": ["edit token missing or wrong"]})
            if self.path == "/api/locate":
                slot = int(body.get("slot", -1))
                if not 0 <= slot < 64:
                    return self.send(400, {"error": "bad slot"})
                ok = ctl.hw.locate(slot, bool(body.get("on")))
                log("locate slot %d %s" % (slot, "on" if body.get("on") else "off"))
                for d in ctl.drives:
                    if d["slot"] == slot:
                        d["locate"] = bool(body.get("on"))
                return self.send(200, {"ok": ok})
            if self.path in ("/api/config/validate", "/api/config/apply"):
                with ctl.lock:
                    e, w, cfg = validate_config(body.get("config"), ctl.cfg, ctl.safety)
                    prev = ctl.demand(cfg) if cfg else None
                if self.path.endswith("validate") or e:
                    return self.send(200 if not e else 422, {"ok": not e, "errors": e, "warnings": w, "preview": prev})
                if not ctl.check_token(body.get("token")):
                    log("config apply REFUSED: bad edit token from %s" % self.client_address[0])
                    return self.send(403, {"ok": False, "errors": ["edit token missing or wrong"]})
                with ctl.lock:
                    ctl.apply_config(cfg, "web " + self.client_address[0])
                return self.send(200, {"ok": True, "warnings": w, "preview": prev})
            if self.path == "/api/config/revert":
                name = os.path.basename(str(body.get("backup", "")))
                p = os.path.join(ctl.backup_dir, name)
                if not ctl.check_token(body.get("token")):
                    return self.send(403, {"ok": False, "errors": ["edit token missing or wrong"]})
                if not re.match(r"^config-\d{8}-\d{6}\.json$", name) or not os.path.exists(p):
                    return self.send(404, {"ok": False, "errors": ["no such backup"]})
                with open(p) as f:
                    old = json.load(f)
                with ctl.lock:
                    e, w, cfg = validate_config(old, ctl.cfg, ctl.safety)
                    if e:
                        return self.send(422, {"ok": False, "errors": e})
                    ctl.apply_config(cfg, "revert to %s by %s" % (name, self.client_address[0]))
                return self.send(200, {"ok": True})
            if self.path == "/api/fault":
                slot = int(body.get("slot", -1))
                if not 0 <= slot < 64:
                    return self.send(400, {"error": "bad slot"})
                mode = body.get("mode")  # "on" | "off" | "auto"
                if mode not in ("on", "off", "auto"):
                    return self.send(400, {"error": "mode must be on|off|auto"})
                with ctl.led_lock:
                    man = ctl.leds.setdefault("manual", {})
                    if mode == "auto":
                        man.pop(str(slot), None)
                    else:
                        man[str(slot)] = (mode == "on")
                    log("fault LED bay %d manual -> %s" % (slot, mode))
                    ctl.apply_fault_leds(ctl.drives)
                return self.send(200, {"ok": True})
            if self.path == "/api/layout":
                lay = body.get("layout")
                if not isinstance(lay, dict) or "sensors" not in lay or "bays" not in lay:
                    return self.send(400, {"error": "bad layout"})
                with ctl.lock:
                    ctl.layout = lay
                    with open(ctl.layout_path, "w") as f:
                        json.dump(lay, f, indent=2)
                    own(ctl.layout_path)
                log("layout saved")
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/data", help="config.json + layout.json")
    ap.add_argument("--logs", default=None, help="fanctl.log + history/ (default: --data)")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--mock", action="store_true")
    a = ap.parse_args()
    global LOGFILE, OWNER
    os.umask(0o007)
    if os.environ.get("PUID") and os.environ.get("PGID"):
        OWNER = (int(os.environ["PUID"]), int(os.environ["PGID"]))
    os.makedirs(a.data, exist_ok=True)
    logs = a.logs or a.data
    os.makedirs(logs, exist_ok=True)
    LOGFILE = os.path.join(logs, "fanctl.log")
    global NOTIFY_FILE, HEARTBEAT
    NOTIFY_FILE = os.path.join(logs, "notify.jsonl")
    HEARTBEAT = os.path.join(logs, "heartbeat")
    cfg = load_json(os.path.join(a.data, "config.json"), DEFAULT_CONFIG)
    hw = MockHardware(cfg) if a.mock else Hardware(cfg)
    ctl = Controller(hw, a.data, os.path.join(a.data, "config.json"), os.path.join(a.data, "layout.json"), logs)
    stop = threading.Event()

    def on_term(*_):
        stop.set()
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(ctl))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("http on :%d" % a.port)
    try:
        ctl.loop(stop)
    except Exception as e:
        ctl.failsafe_all("controller crashed: %r" % e)
        raise
    ctl.restore_floors()
    srv.shutdown()


if __name__ == "__main__":
    main()
