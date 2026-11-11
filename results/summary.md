# Results (simulated)

## 12 chargers, 3x63 A (main): 316 sessions, 4.28 MWh requested, 80640 minutes; base load alone above fuse: 0 min

| strategy | min above fuse | s above fuse before reaction | max phase A | peak kW | energy by departure % | sessions fully served % | worst session % | worst car % | Jain index | pauses / session | ctrl p50 us | ctrl p99 us |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| No load management | 3038 | 30640 | 124.5 | 74.9 | 100.0 | 100.0 | 100.0 | 100.0 | 1.0 | 0.0 | - | - |
| Static equal split | 27 | 280 | 69.1 | 43.8 | 98.46 | 97.2 | 60.3 | 93.4 | 0.998 | 2.3 | 29.2 | 224.5 |
| Dynamic, equal share | 0 | 3800 | 61.8 | 41.9 | 99.96 | 99.7 | 96.5 | 99.7 | 1.0 | 0.21 | 28.4 | 256.0 |
| Dynamic, first come | 0 | 3870 | 62.0 | 41.9 | 99.93 | 99.7 | 93.5 | 99.4 | 1.0 | 0.16 | 27.7 | 198.7 |
| Dynamic, departure aware | 0 | 3820 | 61.8 | 41.9 | 100.0 | 100.0 | 100.0 | 100.0 | 1.0 | 0.2 | 28.6 | 299.7 |
| Dynamic, kW budget only (not phase aware) | 3356 | 33010 | 86.1 | 42.5 | 100.0 | 100.0 | 100.0 | 100.0 | 1.0 | 0.04 | 29.1 | 198.5 |

## 12 chargers, 3x80 A: 316 sessions, 4.28 MWh requested, 80640 minutes; base load alone above fuse: 0 min

| strategy | min above fuse | s above fuse before reaction | max phase A | peak kW | energy by departure % | sessions fully served % | worst session % | worst car % | Jain index | pauses / session | ctrl p50 us | ctrl p99 us |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| No load management | 1143 | 11570 | 124.5 | 74.9 | 100.0 | 100.0 | 100.0 | 100.0 | 1.0 | 0.0 | - | - |
| Static equal split | 1 | 10 | 80.1 | 53.7 | 99.94 | 99.7 | 94.3 | 99.5 | 1.0 | 0.12 | 27.2 | 257.1 |
| Dynamic, equal share | 0 | 1370 | 78.9 | 53.7 | 100.0 | 100.0 | 100.0 | 100.0 | 1.0 | 0.0 | 28.4 | 255.3 |
| Dynamic, departure aware | 0 | 1380 | 78.6 | 53.7 | 100.0 | 100.0 | 100.0 | 100.0 | 1.0 | 0.0 | 28.7 | 289.7 |

## 24 chargers, 3x63 A: 629 sessions, 8.68 MWh requested, 80640 minutes; base load alone above fuse: 0 min

| strategy | min above fuse | s above fuse before reaction | max phase A | peak kW | energy by departure % | sessions fully served % | worst session % | worst car % | Jain index | pauses / session | ctrl p50 us | ctrl p99 us |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| No load management | 8786 | 88240 | 243.3 | 145.4 | 100.0 | 100.0 | 100.0 | 100.0 | 1.0 | 0.0 | - | - |
| Static equal split | 27 | 280 | 69.1 | 42.5 | 83.93 | 73.9 | 21.7 | 66.5 | 0.959 | 5.99 | 123.0 | 433.6 |
| Dynamic, equal share | 0 | 10130 | 61.9 | 42.4 | 99.59 | 99.0 | 66.4 | 97.2 | 1.0 | 1.49 | 50.9 | 457.7 |
| Dynamic, departure aware | 0 | 10160 | 61.9 | 42.4 | 99.84 | 99.2 | 84.7 | 98.7 | 1.0 | 1.33 | 68.3 | 670.6 |

## Closed loop over OCPP 1.6J (day 14, from 12:00, 1440 virtual minutes)

| run | minutes_above_fuse | seconds_above_fuse_before_reaction | max_phase_a | sessions | energy_delivered_pct | profiles_sent | profiles_accepted | meter_values | cycle_ms_p50 | cycle_ms_p95 | cycle_ms_max | stale_cycles | events |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| normal | 0 | 230 | 60.9 | 9 | 100.0 | 2243 | 2243 | 7592 | 15.1 | 34.5 | 62.7 | 0 |  |
| disconnect | 0 | 240 | 60.9 | 9 | 100.0 | 2184 | 2184 | 7532 | 15.9 | 33.8 | 78.8 | 0 | t+420 min CP01: offline; t+480 min CP01: back online |
| stale_meter | 0 | 260 | 61.2 | 9 | 100.0 | 2636 | 2636 | 7592 | 17.2 | 34.8 | 92.1 | 58 | t+422 min meter: stale |
| ignores_profile | 0 | 230 | 61.1 | 9 | 100.0 | 2074 | 2074 | 7592 | 21.9 | 33.6 | 62.2 | 0 | t+250 min CP02: ignores profile |
