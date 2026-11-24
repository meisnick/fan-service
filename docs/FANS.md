# Fans

## Zones and commands
| Zone | Fans | Role |
|---|---|---|
| 0 | FAN1, FAN2, FAN5 | **CPU / rear**: FAN1/FAN2 = the NH-U9 TR4-SP3 NF-A9s (likely), FAN5 = 2x rear NF-A8 (splitter, likely) |
| 1 | FANA | **Fan wall**: 3x Noctua NF-F12 iPPC-3000 in a custom 3D-printed shroud, one splitter |

Mapping confirmed 2026-11-24 by revving each zone to 100% in turn.

FAN3, FAN4 and FANB are not populated. BMC lower critical threshold is 280 RPM.

```bash
ipmitool raw 0x30 0x45 0x00                 # read mode: 00 Standard, 01 Full, 02 Optimal, 04 HeavyIO
ipmitool raw 0x30 0x45 0x01 0x01            # set mode Full (required for manual duty to stick)
ipmitool raw 0x30 0x70 0x66 0x01 <zone> <hex%>   # set duty
ipmitool raw 0x30 0x70 0x66 0x00 <zone>          # read duty (hex %)
ipmitool sensor | grep -E '^FAN'
```

From the LAN add `-I lanplus -H <bmc-ip> -U ADMIN -P <pw>`.

**Any BMC reboot or flash resets both zones to 100%.** Re-apply the duties afterwards.

## Baseline ("comfortable") setting
```bash
ipmitool raw 0x30 0x70 0x66 0x01 0x01 0x36   # zone 1 = 54%
ipmitool raw 0x30 0x70 0x66 0x01 0x00 0x54   # zone 0 = 84%
```

Measured 2026-11-24 (stable within 15 s, held for 60 s):

| Zone | Duty | Fan | RPM at comfortable | RPM at 100% |
|---|---|---|---|---|
| 0 | 0x54 = 84% | FAN1 | 1680 | 1960 |
| | | FAN2 | 1680 | 2100 |
| | | FAN5 | 1960 | 2380 |
| 1 | 0x36 = 54% | FANA | 1540 | 2660 |

Conditions during the measurement: CPU 56 °C, DIMMs 50 / 53 °C, load ~1.7.

- Zone 0 keeps 80–86% of full speed at 84% duty, so those fans flatten near the top.
- Zone 1 at 54% runs at ~58% of full speed: roughly linear, the most headroom.

## Why fixed duty isn't enough: RAM overheating
BMC events (true times from Redfish), DIMM temps 85–90 °C, logged as non-recoverable:

| Date | Window |
|---|---|
| 2026-11-09 | 03:10–05:16 |
| 2026-11-10 | 05:49–06:06 |
| 2026-11-14 | 05:42–05:57 |

- Netdata at the same times: load ~17–21, CPU ~35%, CPU temp 88–91 °C, from ~02:10 to ~05:10.
- **Cause: Plex Butler maintenance (default window 02:00–05:00):** intro/credits detection, loudness analysis, chapter thumbnails. These are mostly CPU work even with GPU transcode on.
- **Not a scrub:** scrubs are weekly Sunday 00:00; TENTBPOOL's finished 11/08 16:34. No cron, cloud sync or replication jobs. Tautulli shows 1 play across those windows.
- Plex app CPU limit is 24 threads. Lowering it to ~8 was suggested (not done).
- ECC error counters are 0; no MCE.
- Caveat: the SEL fan readings around those events were ~2000–2600 RPM (close to 100%), which suggests airflow over the DIMMs is marginal even at high duty. Confirm which zone actually cools the DIMMs during the stress test.

## Zone mapping test (2026-11-24, 03:03–03:52)
Method: `scripts/fanmap_run.sh` ran as a systemd unit on the box. It logged every sensor every ~15 s: 4 fans, all BMC temps, k10temp Tctl, GPU, 6 NVMe, 24 HDDs. Steps:
- P0: idle RPM map, 25 s per step.
- P1: 8-minute heat soak at comfortable duty, with stress-ng in an Alpine container capped at 24 threads (`--cpu 16 --vm 8 --vm-bytes 3G`).
- P2: fan-wall sweep, 5 minutes per step.
- P3: CPU/rear sweep, 5 minutes per step.

Guard: DIMM ≥ 80 °C or CPU ≥ 88 °C → both zones 100% and load off. It **tripped at the end of P3 z0=60% (CPU 89 °C)**, after 4.8 of 5 minutes. Raw log: [`data/fanmap_2026-11-24.csv`](../data/fanmap_2026-11-24.csv); analysis: `scripts/fanmap_analyze.py`.

### Duty → RPM (idle)
| Duty % | FANA (zone 1, fan wall) | | Duty % | FAN1 / FAN2 / FAN5 (zone 0, CPU/rear) |
|---|---|---|---|---|
| 25 | 700 | | 40 | 840 / 840 / 980 |
| 35 | 1120 | | 50 | 1120 / 1120 / 1260 |
| 45 | 1400 | | 60 | 1260 / 1260 / 1400 |
| **54** | **1540** | | 70 | 1400 / 1540 / 1680 |
| 65 | 1820 | | **84** | **1680 / 1680 / 1960** |
| 75 | 2100 | | 100 | 1960 / 1960 / 2380 |
| 85 | 2380 | | | |
| 100 | 2660 | | | |

No stall seen down to 25% on zone 1 (700 RPM; the BMC lower-critical limit is 280). The BMC reads RPM in 140 RPM steps.

### Loaded equilibrium (mean of the last 2 min of each step, °C)
| z0 | z1 | CPU | DIMM A–D | DIMM E–H | VRM ABCD | VRM EFGH | CPU_VRM | SOC_VRM | System | Periph | HDD avg / max | NVMe max |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 84 | 54 (soak) | 81.8 | 65.8 | 65.9 | 56.0 | 63.6 | 55.1 | 58.8 | 38.0 | 44.0 | 31.6 / 34 | 41 |
| 84 | **100** | 80.1 | **59.2** | 67.9 | 53.5 | 61.0 | 53.0 | 56.0 | 35.1 | 39.6 | 30.8 / 33 | 40 |
| 84 | 75 | 81.2 | 60.5 | 68.4 | 54.0 | 62.5 | 54.0 | 57.8 | 36.0 | 40.8 | 30.4 / 33 | 40 |
| 84 | 54 | 78.9 | 65.2 | 66.0 | 55.5 | 63.0 | 55.0 | 59.0 | 37.0 | 43.0 | 30.6 / 33 | 40 |
| 84 | **40** | 80.2 | **67.0** | 65.1 | 57.4 | 65.2 | 57.0 | 59.0 | 39.0 | 45.0 | 31.4 / 34 | 41 |
| **100** | 54 | **78.8** | 65.0 | **64.0** | 57.0 | 64.0 | 56.0 | 58.5 | 38.0 | 44.0 | 31.8 / 34 | 40 |
| **60** | 54 | **86.2**↑ | 65.9 | **74.6**↑ | 57.9 | 68.0 | 59.5 | 61.5 | 39.6 | 44.2 | 32.1 / 35 | 42 |

↑ still rising when the step ended (DIMM E–H 64→75, CPU 79→86 over 5 min).

### What each zone cools
| Sensor | Zone 0 (CPU/rear) | Zone 1 (fan wall) |
|---|---|---|
| CPU | **Strong**: 100%→78.8, 84%→~80, 60%→86+ | Weak (~1 °C) |
| DIMM bank **E–H** | **Strong**: 100%→64, 84%→66, 60%→75+ | None (slightly warmer at higher z1) |
| DIMM bank **A–D** | None | **Strong**: 100%→59, 75%→60.5, 54%→65, 40%→67 |
| VRM EFGH | Medium (+4 at 60%) | Medium (−4 from 40→100%) |
| VRM ABCD, CPU_VRM, SOC_VRM | Small | Small–medium (−2…4) |
| System / Peripheral | None | **Yes** (−4…5 from 40→100%) |
| HDDs | None | Weak (~1 °C in 5 min; disks were idle, so little heat to move) |
| NVMe, GPU (idle) | ~None | ~None |

**Why (from photos):** the NH-U9 blows **sideways**, from the PCIe/GPU side toward the PSU. Bank A–D sits on the cooler intake side and gets fan-wall air; bank E–H sits in the CPU heatsink exhaust, which the rear NF-A8s pull out.

**Key finding:** the two RAM banks are cooled by different zones. Bank A–D sits in the fan-wall airflow. Bank E–H sits downstream of the CPU cooler and follows the rear zone. A RAM-only controller on one zone would miss half the DIMMs.

**Headroom:** above comfortable, zone 0 only buys ~2 °C (84→100% is +280–420 RPM), and it's the zone that cools the CPU and bank E–H. Under a heavier or longer load than this test, as on the Plex nights with CPU 88–91 °C, fans alone may not hold E–H under ~75 °C. The Plex CPU cap matters too.

### Caveats
- 5-minute steps: DIMMs were still rising ~0.5 °C/min at the end of the soak, so long loads end a few degrees hotter.
- The test load (CPU ~80 °C) was lighter than the Plex nights (CPU 88–91 °C).
- No disk I/O load, so HDD sensitivity is understated. HDDs sat at 27–35 °C throughout, far from limits.
- Step order (high→low duty) adds some carry-over from the previous step.

### Speed-to-heat table (v2, live since 2026-11-24 05:21)
v2 came out of the external review (see "v1 → v2" below). The v1 table is kept underneath for history.

**Zone 0: CPU / rear** (floor 84%)
| Input | ≤ floor temp → 84% | | → 100% |
|---|---|---|---|
| CPU Temp | ≤ 76 °C | 80 °C → 92% | ≥ 84 °C |
| DIMM E–H | ≤ 65 °C | 68 °C → 92% | ≥ 72 °C |
| VRM EFGH | ≤ 70 °C | | ≥ 80 °C |

**Zone 1: fan wall** (floor 54%)
| Input | ≤ floor temp → 54% | | | → 100% |
|---|---|---|---|---|
| DIMM A–D | ≤ 62 °C | 65 °C → 65% | 68 °C → 80% | ≥ 72 °C |
| Peripheral Temp | ≤ 48 °C | | 52 °C → 75% | ≥ 56 °C |
| HDD max | ≤ 40 °C | | 45 °C → 75% | ≥ 50 °C |
| (no DIMM E–H assist) | | | | |

**v1 → v2, and why:**
- **Zone 0 reaches 100% earlier** (CPU 87 → 84, E–H 75 → 72). At 84% zone 0 already turns ~85% of its maximum rpm and buys only ~2 °C more at 100%, so going full early costs little noise and avoids heat-soaking the NH-U9 and bank E–H.
- **The zone 1 "DIMM E–H assist" curve is removed.** In the P2 sweep (zone 0 held at 84%), raising the fan wall 54 → 75 → 100% *warmed* DIMM E–H from 66 to 67–69 °C while A–D dropped 6.6 °C. The wall's front-to-back flow seems to disturb the cooler's sideways exhaust over bank E–H, so the assist curve was loud and counter-productive.
- **Peripheral Temp starts at 48, not 46.** It soaks at 44–45 °C. With 3 °C hysteresis, a 46 °C start could pin zone 1 near 60% after any brief spike. The controller also no longer applies hysteresis below a curve's first point.
- Still to validate on a real Plex 02:00–05:00 night: E–H plateau ≤ 72–74 °C with zone 0 at 100%; E–H behaviour when zone 1 ramps for A–D; both zones back at 84/54 within ~20 min after 05:00.

### v2 soak test (2026-11-24 05:28–06:08, fanctl in control)
Same load as the mapping test (stress-ng, 24 CPUs: 16 cpu + 8×3 GB vm workers) for 40 min, fans left entirely to fanctl v2.
Load window statistics (10 s samples, 05:33–06:08):

| Sensor | avg | p95 | max | v1 test, fixed 84/54 % |
|---|---|---|---|---|
| CPU Temp | 77.0 | 79 | 83 | ~80–81 |
| DIMM A–D | 63.6 | 65 | 66 | ~66 |
| DIMM E–H | 65.4 | 66 | 67 | ~66 |
| VRM EFGH | 62.2 | 63 | 64 | 63–64 |
| Peripheral | 44.1 | 45 | 45 | 44 |
| HDD max / NVMe max / HBA max | 35.5 / 42.2 / 68 | | 37 / 43 / 70 | |

- Duty: zone 0 averaged 88.6 % (71 % of the time at 85–89 %, max 98 % during two real ~40 s CPU bursts to 82–83 °C); zone 1 averaged 64.7 % (85 % of the time at 60–65 %, max 70 %).
- Every sensor stayed far from its limits: DIMM E–H 67 °C max vs alert 80 and emergency 82; CPU 83 vs 90. No emergency, failsafe or alert.
- **Return to floor:** load stopped 06:08:10, both zones back at 84 / 54 % by 06:10:33. Nothing stuck in a hysteresis hold.
- As the mapping predicted, extra zone 0 duty barely moves DIMM E–H (~−0.5 °C at 88 % vs 84 %). The fan wall at ~65 % buys DIMM A–D about −2.5 °C.

### Speed-to-heat table (v1, superseded)
Each zone runs at the **max** of its inputs. It never goes below your comfortable floor, ramps up immediately, and ramps down at most 5% per minute with 3 °C hysteresis. If a sensor read fails or the controller exits, both zones go to 100%.

**Zone 0: CPU / rear** (floor 84%)
| Input | ≤ floor temp → 84% | | | → 100% |
|---|---|---|---|---|
| CPU Temp | ≤ 78 °C | 82 °C → 90% | 85 °C → 95% | ≥ 87 °C |
| DIMM E–H | ≤ 66 °C | 70 °C → 90% | 73 °C → 95% | ≥ 75 °C |
| VRM EFGH | ≤ 70 °C | | | ≥ 80 °C |

**Zone 1: fan wall** (floor 54%)
| Input | ≤ floor temp → 54% | | | → 100% |
|---|---|---|---|---|
| DIMM A–D | ≤ 62 °C | 65 °C → 65% | 68 °C → 80% | ≥ 72 °C |
| Peripheral Temp | ≤ 46 °C | | 50 °C → 75% | ≥ 55 °C |
| HDD max | ≤ 40 °C | | 45 °C → 75% | ≥ 50 °C |
| DIMM E–H (assist) | ≤ 74 °C | | | ≥ 78 °C |

Escalation: if either DIMM bank holds ≥ 80 °C for 2 minutes with both zones at 100%, log it and alert. A later option is to throttle Plex's cpuset.

## Fan-controller plan (in progress)
1. ~~Map duty → RPM~~ done (above).
2. ~~Which zone cools what~~ done (above).
3. ~~Curves~~ v2 table above (v1 kept for history); tune after real Plex nights.
4. ~~Daemon~~ **built and live 2026-11-24: TrueNAS app `fanctl`**, dashboard http://<nas>:31090. See [../fanctl/README.md](../fanctl/README.md).
5. Validate overnight during the Plex 02:00–05:00 window; target RAM < ~75 °C.


First guess to test: the fan wall (zone 1) is what cools the DIMMs and HDDs, so RAM heat should drive zone 1, and CPU heat should drive zone 0. Zone 1 has the most headroom (1540 → 2660 RPM).

Survey/measure scripts: [`scripts/fan_survey.sh`](../scripts/fan_survey.sh), [`scripts/fan_comfortable_measure.sh`](../scripts/fan_comfortable_measure.sh).
