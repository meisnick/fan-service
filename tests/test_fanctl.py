"""Unit tests for the safety-critical parts of fanctl: config validation, curve/hysteresis/ramp math,
duty-write failure handling, per-sensor emergency latching and the edit-token check. No hardware needed."""
import sys, os, json, math, tempfile, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fanctl", "app"))
import fanctl as F
S = json.loads(json.dumps(F.DEFAULT_SAFETY))
ok = 0
def check(c, msg):
    global ok
    assert c, msg
    ok += 1
# validate: defaults valid
e, w, c = F.load_file_config(F.DEFAULT_CONFIG, S); check(not e, ("defaults", e))
# optional on required -> error (hand edit)
bad = json.loads(json.dumps(F.DEFAULT_CONFIG)); bad["zones"]["0"]["curves"][0]["optional"] = True
e, w, c = F.load_file_config(bad, S); check(any("optional" in x for x in e), e)
# missing zone
bad = json.loads(json.dumps(F.DEFAULT_CONFIG)); del bad["zones"]["0"]
check(F.load_file_config(bad, S)[0], "zone missing")
# failsafe_duty / interval bounds / max
for k, v in (("failsafe_duty", 50), ("interval_s", 0), ("read_fail_limit", 99)):
    bad = json.loads(json.dumps(F.DEFAULT_CONFIG)); bad[k] = v
    check(F.load_file_config(bad, S)[0], k)
bad = json.loads(json.dumps(F.DEFAULT_CONFIG)); bad["zones"]["1"]["max"] = 60
check(F.load_file_config(bad, S)[0], "max")
# rounding collision
bad = json.loads(json.dumps(F.DEFAULT_CONFIG)); bad["zones"]["1"]["curves"][1]["points"] = [[50.01, 54], [50.04, 100]]
check(any("strictly" in x for x in F.validate_config(bad, F.DEFAULT_CONFIG, S)[0]), "round")
# bool duty
bad = json.loads(json.dumps(F.DEFAULT_CONFIG)); bad["zones"]["1"]["curves"][1]["points"] = [[50, True], [55, 100]]
check(F.validate_config(bad, F.DEFAULT_CONFIG, S)[0], "bool")
# XSS sensor name not echoed
bad = json.loads(json.dumps(F.DEFAULT_CONFIG)); bad["zones"]["1"]["curves"][1]["sensor"] = "<img src=x>"
check(not any("<" in x for x in F.validate_config(bad, F.DEFAULT_CONFIG, S)[0]), "xss")
# NaN
bad = json.loads(json.dumps(F.DEFAULT_CONFIG)); bad["zones"]["1"]["floor"] = float("nan")
check(F.validate_config(bad, F.DEFAULT_CONFIG, S)[0], "nan")

# controller-level tests with a fake hw
class HW:
    def __init__(s): s.d = {}; s.fail = False
    def set_duty(s, z, p):
        if s.fail: raise RuntimeError("busy")
        s.d[z] = p
    def get_duty(s, z): return s.d.get(z)
C = F.Controller.__new__(F.Controller)
C.hw = HW(); C.cfg = json.loads(json.dumps(F.DEFAULT_CONFIG)); C.safety = S
C.duty = {"0": None, "1": None}; C.set_err = None; C.emerg_active = {}; C.emergency = None
C.drives, C.nvme, C.gpus, C.hbas, C.ses, C.tctl = [], {}, [], [], [], None
vals = {"CPU Temp": 70, "P1_DIMME~H Temp": 60, "VRMEFGH Temp": 60, "P1_DIMMA~D Temp": 55, "Peripheral Temp": 44}
def setv(**kw):
    for k, v in kw.items(): vals[k.replace("_", " ").replace("DIMMEH", "P1_DIMME~H").replace("DIMMAD", "P1_DIMMA~D")] = v
    C.bmc_data = {k: {"value": v, "unit": "degrees C", "status": "ok"} for k, v in vals.items() if v is not None}
setv()
n, fs = C.compute(10); check(n["1"][0] == 54 and n["0"][0] == 84, n)
for z, (d, _) in n.items(): C.apply(z, d)
# hysteresis release below the first point: Peripheral 47 -> 44 must return to floor (with ramp)
vals["Peripheral Temp"] = 52; setv(); n, _ = C.compute(10); [C.apply(z, d) for z, (d, _) in n.items()]
check(C.duty["1"] == 75, C.duty)
vals["Peripheral Temp"] = 44; setv()
for i in range(40):
    n, _ = C.compute(10); [C.apply(z, d) for z, (d, _) in n.items()]
check(C.duty["1"] == 54, ("stuck", C.duty, n))
# dt cap is in loop(); compute ramp: one call with dt=20 drops <= 5*20/60
vals["Peripheral Temp"] = 56; setv(); n, _ = C.compute(10); [C.apply(z, d) for z, (d, _) in n.items()]
vals["Peripheral Temp"] = 40; setv(); n, _ = C.compute(20); check(n["1"][0] >= 100 - 5 * 20 / 60 - 1e-9, n)
# set_duty failure: duty not recorded, retried
C.duty["0"] = 84; C.hw.d["0"] = 84; C.hw.fail = True
check(C.apply("0", 100) is False and C.duty["0"] == 84, C.duty)
C.hw.fail = False; check(C.apply("0", 100) and C.hw.d["0"] == 100, C.hw.d)
# emergency per sensor, missing sample does not release
vals["P1_DIMME~H Temp"] = 83; setv(); check(C.check_emergency(), "trip")
vals["P1_DIMME~H Temp"] = None; setv(); check(C.check_emergency(), "None must not release")
vals["P1_DIMME~H Temp"] = 79; setv(); check(C.check_emergency(), "79 > 77 hold")
vals["CPU Temp"] = 86; setv(); h = C.check_emergency(); check(not any("CPU" in x for x in h), ("cpu cross-latch", h))
vals["P1_DIMME~H Temp"] = 76; setv(); check(not C.check_emergency(), "release")
# token
C.token_path = os.path.join(tempfile.mkdtemp(), "t")
open(C.token_path, "w").write("")
check(not C.check_token(" "), "empty token file")
open(C.token_path, "w").write("abcdefghijklmnopqrstuvwx\n")
check(C.check_token("abcdefghijklmnopqrstuvwx ") and not C.check_token("x") and not C.check_token(None), "token")
print("ALL OK", ok)
