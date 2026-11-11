"""Command line: `python -m olmc demo` reproduces every number in the README."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from .config import MIN_PER_DAY, SiteConfig
from .data import build
from .live import Faults, run_live
from .report import BLOCK_DAYS, BLOCKS, SEED, redraw, run_all


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="olmc", description="OCPP 1.6J load-managed EV charging (simulated building)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="8 simulated weeks x all strategies + OCPP fault runs; writes results/ and docs/")
    d.add_argument("--quick", action="store_true", help="one week + the OCPP fault runs (CI); writes results/quick/")
    d.add_argument("--workers", type=int, default=2)
    sub.add_parser("figures", help="redraw docs/ figures and linkedin.json from the last demo run")
    lv = sub.add_parser("live", help="one closed-loop run over OCPP websockets on localhost")
    lv.add_argument("--day", type=int, default=BLOCKS[0] + 1)
    lv.add_argument("--hours", type=float, default=24.0)
    lv.add_argument("--fuse", type=float, default=63.0)
    lv.add_argument("--policy", default="departure", choices=["equal", "fifo", "departure"])
    lv.add_argument("--tick", type=float, default=0.0, help="real seconds per virtual minute")
    lv.add_argument("--ignore", default=None, help="charger id that ignores its profile, e.g. CP05")
    a = ap.parse_args(argv)
    root = Path.cwd()
    t0 = time.time()
    if a.cmd == "demo":
        if a.quick:
            res = run_all(root / "results" / "quick", root / "results" / "quick", blocks=(BLOCKS[0],), block_days=7,
                          live_minutes=1440, workers=a.workers)
        else:
            res = run_all(root / "results", root / "docs", BLOCKS, BLOCK_DAYS, workers=a.workers)
        for scen, rows in res["sims"].items():
            for s, m in rows.items():
                print(f"{scen:9s} {s:10s} over={m['minutes_above_fuse']:5d} min  energy={m['energy_delivered_pct']:6.2f} %  "
                      f"worst={m['worst_session_pct']:5.1f} %  jain={m['jain_sessions']}")
        for name, r in res["live"]["runs"].items():
            print(f"live {name:16s} over={r['minutes_above_fuse']} min  energy={r['energy_delivered_pct']} %  "
                  f"cycle p50={r['cycle_ms_p50']} ms  events={r['events']}")
        assert all(r["profiles_sent"] > 0 for r in res["live"]["runs"].values()), "no OCPP profiles were exchanged"
    elif a.cmd == "figures":
        redraw(root / "results", root / "docs")
    else:
        site = SiteConfig(fuse_a=a.fuse)
        data = build(site, n_days=a.day + 2, seed=SEED)
        start = a.day * MIN_PER_DAY + 12 * 60
        r = asyncio.run(run_live(site, data, start, int(a.hours * 60), a.policy, Faults(ignore_cp=a.ignore), tick_s=a.tick))
        print(json.dumps(r.summary(), indent=1))
    print(f"done in {time.time() - t0:.0f} s")
    return 0
