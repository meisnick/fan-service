#!/usr/bin/env python3
# Summarise a fanmap log: python fanmap_analyze.py data/fanmap_2026-11-24.csv
import csv,sys,statistics as S
sys.stdout.reconfigure(encoding="utf-8")
rows=[r for r in csv.DictReader(open(sys.argv[1])) if r.get('FAN1') not in (None,'','guard')]
ph={}
for r in rows: ph.setdefault(r['phase'],[]).append(r)
def f(x):
    try: return float(x)
    except: return None
def eq(rs,k,n):
    v=[f(r[k]) for r in rs[-n:] if f(r[k]) is not None]; return round(S.mean(v),1) if v else None
print("RPM MAP (idle, last sample of each 25 s step)")
for p,rs in ph.items():
    if p.startswith('P0'): print(f"  {p:12} z0={rs[-1]['z0']:>3} z1={rs[-1]['z1']:>3}  FAN1 {rs[-1]['FAN1']:>6} FAN2 {rs[-1]['FAN2']:>6} FAN5 {rs[-1]['FAN5']:>6} FANA {rs[-1]['FANA']:>6}")
keys=['CPU','Tctl','CPU_VRM','SOC_VRM','VRMABCD','VRMEFGH','DIMM_AD','DIMM_EH','System','Peripheral','gpu','nvme0','nvme1','nvme2','nvme3','nvme4','nvme5','hdd_min','hdd_avg','hdd_max']
print("\nLOADED EQUILIBRIUM (mean of last ~2 min = 8 samples); n=samples in phase")
print("phase        z0  z1  n  RPM(F1/F2/F5/FA)        "+" ".join(f"{k[:7]:>7}" for k in keys))
for p,rs in ph.items():
    if p.startswith(('P1','P2','P3','P4')):
        print(f"{p:12} {rs[-1]['z0']:>3} {rs[-1]['z1']:>3} {len(rs):>2}  {eq(rs,'FAN1',8):>5}/{eq(rs,'FAN2',8):>5}/{eq(rs,'FAN5',8):>5}/{eq(rs,'FANA',8):>5}  "+" ".join(f"{str(eq(rs,k,8)):>7}" for k in keys))
print("\nslopes in P1 soak last 2 min (C/min) to judge equilibrium:")
rs=ph['P1_soak']; 
for k in ['CPU','DIMM_AD','DIMM_EH','hdd_avg','nvme3']:
    a=[f(r[k]) for r in rs[-9:]]; t=[f(r['ts']) for r in rs[-9:]]
    print(" ",k, round((a[-1]-a[0])/((t[-1]-t[0])/60),2))
for p in ['P2_z1_40','P3_z0_60']:
    rs=ph[p]; print(p,'trend DIMM_EH', [r['DIMM_EH'] for r in rs][::3], 'CPU', [r['CPU'] for r in rs][::3])
# per-disk at z1 100 vs 40
a=ph['P2_z1_100']; b=ph['P2_z1_40']
disks=[k for k in rows[0] if k.startswith('sd')]
print('\nper-disk HDD at z1=100 vs z1=40 (end of step):', ' '.join(f"{d}:{a[-1][d]}/{b[-1][d]}" for d in disks))
