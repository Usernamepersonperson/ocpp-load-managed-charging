"""Run every strategy on the same simulated weeks, run the OCPP closed loop with faults, write results/ and docs/."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from .config import MIN_PER_DAY, SiteConfig  # noqa: E402
from .data import BuildingData, build  # noqa: E402
from .live import Faults, run_live  # noqa: E402
from .sim import LABELS, STRATEGIES, SimResult, metrics, simulate  # noqa: E402

REPO = "https://github.com/VB-Builds/ocpp-load-managed-charging"
SEED = 7
# four representative fortnights (Mondays): mid-January, April, July, October of the simulated year
BLOCKS = (7, 98, 189, 280)
BLOCK_DAYS = 14
SCENARIOS = {
    "main": {"fuse_a": 63.0, "n_chargers": 12},
    "fuse80": {"fuse_a": 80.0, "n_chargers": 12},
    "all_flats": {"fuse_a": 63.0, "n_chargers": 24},
}
SENS_STRATEGIES = ("none", "static", "equal", "departure")
PAL = {"bg": "#16213A", "grid": "#2A3750", "text": "#F8FAFC", "muted": "#A9B6CB", "accent": "#38BDF8",
       "green": "#34D399", "amber": "#FBBF24", "red": "#F87171", "grey": "#64748B", "violet": "#A78BFA"}
SUBTITLE = "Simulated building: 24 flats, heat pump, seeded base load and car sessions. Not measured data."

_CACHE: dict[int, BuildingData] = {}


def _data(n_chargers: int, n_days: int) -> BuildingData:
    if n_chargers not in _CACHE:
        _CACHE[n_chargers] = build(SiteConfig(n_chargers=n_chargers), n_days=n_days, seed=SEED)
    return _CACHE[n_chargers]


def merge(parts: list[SimResult]) -> SimResult:
    return SimResult(
        parts[0].strategy, 0,
        np.concatenate([p.total_a for p in parts]), np.concatenate([p.ev_a for p in parts]),
        [o for p in parts for o in p.outcomes], np.concatenate([p.latency_us for p in parts]),
        sum(p.pauses for p in parts), sum(p.transient_s for p in parts),
    )


def _job(args: tuple) -> tuple[str, str, dict, np.ndarray | None]:
    scen, strategy, blocks, block_days, n_days = args
    site = SiteConfig(**SCENARIOS[scen])
    data = _data(site.n_chargers, n_days)
    parts = [simulate(site, data, strategy, start_day=b, n_days=block_days) for b in blocks]
    r = merge(parts)
    m = metrics(site, r)
    base_parts = [data.base_a[b * MIN_PER_DAY:(b + block_days) * MIN_PER_DAY] for b in blocks]
    m["minutes_base_alone_above_fuse"] = int((np.concatenate(base_parts) > site.fuse_a + 1e-6).any(axis=1).sum())
    keep = r.total_a.max(axis=1).astype(np.float32) if scen == "main" else None
    return scen, strategy, m, keep


def run_sims(blocks=BLOCKS, block_days=BLOCK_DAYS, workers: int = 2) -> tuple[dict, dict]:
    n_days = max(blocks) + block_days + 1
    jobs = [("main", s, blocks, block_days, n_days) for s in STRATEGIES]
    jobs += [(sc, s, blocks, block_days, n_days) for sc in ("fuse80", "all_flats") for s in SENS_STRATEGIES]
    out: dict = {}
    series: dict = {}
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for scen, strategy, m, keep in ex.map(_job, jobs):
            out.setdefault(scen, {})[strategy] = m
            if keep is not None:
                series[strategy] = keep
    return out, series


LIVE_START_H = 12
FAULT_WINDOW = (420, 480)  # minutes after the start: 19:00-20:00


def pick_live_day(data: BuildingData, first_day: int, days: int) -> int:
    """Weekday with the most sessions that arrive after 12:00 and leave before 12:00 next day."""
    best, best_n = first_day, -1
    for d in range(first_day, first_day + days):
        if d % 7 >= 5:
            continue
        s0, s1 = d * MIN_PER_DAY + LIVE_START_H * 60, (d + 1) * MIN_PER_DAY + LIVE_START_H * 60
        n = sum(1 for s in data.sessions if s0 <= s.arrival and s.departure <= s1)
        if n > best_n:
            best, best_n = d, n
    return best


def run_live_suite(minutes: int = 1440) -> tuple[dict, dict]:
    site = SiteConfig()
    data = build(site, n_days=BLOCKS[0] + BLOCK_DAYS + 1, seed=SEED)
    day = pick_live_day(data, BLOCKS[0], BLOCK_DAYS)
    start = day * MIN_PER_DAY + LIVE_START_H * 60
    f0, f1 = FAULT_WINDOW
    # chargers with a car plugged in during the fault window (first two by charger number)
    busy = sorted({s.charger for s in data.sessions if s.arrival <= start + f0 and s.departure >= start + f1
                   and s.arrival >= start})
    off_cp, bad_cp = f"CP{busy[0] + 1:02d}", f"CP{busy[1] + 1:02d}"
    runs = {
        "normal": Faults(),
        "disconnect": Faults(disconnect_cp=off_cp, disconnect_at=f0, reconnect_at=f1),
        "stale_meter": Faults(stale_from=f0, stale_to=f1),
        "ignores_profile": Faults(ignore_cp=bad_cp),
    }
    out = {"day": day, "start": f"{LIVE_START_H:02d}:00", "minutes": minutes, "runs": {}}
    series = {}
    for name, f in runs.items():
        r = asyncio.run(run_live(site, data, start, minutes, policy="departure", faults=f))
        out["runs"][name] = {"faults": asdict(f), **r.summary()}
        series[name] = np.asarray(r.max_phase_series)
    return out, series


# ---------------------------------------------------------------- figures
def _style() -> None:
    plt.rcParams.update({
        "font.family": ["Segoe UI", "DejaVu Sans"], "axes.facecolor": PAL["bg"], "figure.facecolor": PAL["bg"],
        "savefig.facecolor": PAL["bg"], "text.color": PAL["text"], "axes.labelcolor": PAL["muted"],
        "xtick.color": PAL["muted"], "ytick.color": PAL["muted"], "axes.edgecolor": PAL["grid"],
        "grid.color": PAL["grid"], "axes.grid": True, "grid.linewidth": 0.8,
    })


def worst_evening(series: dict) -> int:
    """Index (minute in the merged blocks) of 16:00 on the day where no load management peaks highest (first block)."""
    s = series["none"][: BLOCK_DAYS * MIN_PER_DAY].reshape(BLOCK_DAYS, MIN_PER_DAY)
    day = int(np.argmax(s[:, 16 * 60:24 * 60].max(axis=1)))
    return day * MIN_PER_DAY + 16 * 60


def _evening(ax, series: dict, i0: int, fuse: float, fs: int, strategies=("none", "static", "departure"), lw=2.0) -> None:
    hours = 16 + np.arange(16 * 60) / 60.0
    colors = {"none": PAL["red"], "static": PAL["amber"], "departure": PAL["green"], "equal": PAL["green"]}
    names = {"none": "no load management", "static": "static equal split", "departure": "dynamic controller",
             "equal": "dynamic controller"}
    for s in strategies:
        ax.plot(hours, series[s][i0:i0 + 16 * 60], color=colors[s], lw=lw, label=names[s])
    ax.axhline(fuse, color=PAL["text"], ls="--", lw=1.4)
    ax.text(22.6, fuse + 3, f"main fuse {fuse:.0f} A", color=PAL["text"], fontsize=fs)
    ax.set_xlim(16, 32)
    ax.set_xticks(range(16, 33, 2))
    ax.set_xticklabels([f"{h % 24:02d}" for h in range(16, 33, 2)], fontsize=fs)
    ax.tick_params(labelsize=fs)
    ax.set_ylabel("highest phase current, A", fontsize=fs)
    ax.set_xlabel("hour (winter weekday evening to next morning)", fontsize=fs)


def make_figure(sims: dict, series: dict, live: dict, live_series: dict, path: Path) -> None:
    _style()
    site = SiteConfig()
    main = sims["main"]
    fig = plt.figure(figsize=(16, 9), dpi=100)
    gs = fig.add_gridspec(2, 2, left=0.055, right=0.985, top=0.85, bottom=0.075, hspace=0.42, wspace=0.36,
                          width_ratios=[1.35, 1])
    ax1 = fig.add_subplot(gs[0, 0])
    i0 = worst_evening(series)
    _evening(ax1, series, i0, site.fuse_a, 11)
    ax1.set_ylim(0, max(140, float(series["none"][i0:i0 + 960].max()) + 10))
    ax1.legend(loc="upper right", fontsize=11, frameon=False, labelcolor=PAL["text"])
    ax1.set_title("The worst simulated winter evening, 12 chargers on a 3x63 A fuse", loc="left", fontsize=13,
                  fontweight="bold")

    ax2 = fig.add_subplot(gs[0, 1])
    order = ["none", "equal_kw", "static", "fifo", "equal", "departure"]
    short = {"none": "no load management", "equal_kw": "dynamic, kW budget only", "static": "static equal split",
             "fifo": "dynamic, first come", "equal": "dynamic, equal share", "departure": "dynamic, departure aware"}
    vals = [main[s]["minutes_above_fuse"] for s in order]
    cols = [PAL["red"], PAL["violet"], PAL["amber"], PAL["green"], PAL["green"], PAL["green"]]
    y = np.arange(len(order))[::-1]
    ax2.set_axisbelow(True)
    ax2.barh(y, vals, color=cols)
    for yy, v, s in zip(y, vals, order, strict=True):
        ax2.text(v + max(vals) * 0.015, yy, f"{v}   ({main[s]['energy_delivered_pct']:.1f} % energy)", va="center",
                 fontsize=10.5, color=PAL["text"])
    ax2.set_yticks(y)
    ax2.set_yticklabels([short[s] for s in order], fontsize=10.5)
    ax2.set_xlim(0, max(vals) * 1.55)
    ax2.set_xlabel("minutes above the fuse rating in 8 simulated weeks (one-minute mean)", fontsize=10.5)
    ax2.set_title("Minutes above 63 A and energy delivered by departure", loc="left", fontsize=13, fontweight="bold")

    ax3 = fig.add_subplot(gs[1, 0])
    scen = [("main", "12 chargers, 3x63 A"), ("fuse80", "12 chargers, 3x80 A"), ("all_flats", "24 chargers, 3x63 A")]
    strat = [("none", PAL["red"]), ("static", PAL["amber"]), ("departure", PAL["green"])]
    x = np.arange(len(scen))
    w = 0.26
    ax3.set_axisbelow(True)
    for k, (s, c) in enumerate(strat):
        v = [sims[sc][s]["energy_delivered_pct"] for sc, _ in scen]
        bars = ax3.bar(x + (k - 1) * w, v, width=w, color=c, label={"none": "no load management", "static": "static split",
                                                                    "departure": "dynamic"}[s])
        for b, vv, sc in zip(bars, v, [sc for sc, _ in scen], strict=True):
            over = sims[sc][s]["minutes_above_fuse"]
            ax3.text(b.get_x() + b.get_width() / 2, vv + 1.5, f"{vv:.0f}", ha="center", fontsize=10, color=PAL["text"])
            if over:
                ax3.text(b.get_x() + b.get_width() / 2, 8, f"{over}\nmin\nover", ha="center", fontsize=8.5, color=PAL["bg"])
    ax3.set_xticks(x)
    ax3.set_xticklabels([n for _, n in scen], fontsize=11)
    ax3.set_ylim(0, 118)
    ax3.set_yticks(range(0, 101, 20))
    ax3.set_ylabel("% of requested energy delivered by departure", fontsize=10.5)
    ax3.legend(loc="upper center", fontsize=10.5, frameon=False, labelcolor=PAL["text"], ncol=3, bbox_to_anchor=(0.5, 1.02))
    ax3.set_title("Energy delivered by departure (in the bars: minutes above the fuse)", loc="left", fontsize=13,
                  fontweight="bold")

    ax4 = fig.add_subplot(gs[1, 1])
    h0 = LIVE_START_H
    t = np.arange(len(live_series["normal"])) / 60.0 + h0
    w0, w1 = h0 + FAULT_WINDOW[0] / 60.0, h0 + FAULT_WINDOW[1] / 60.0
    span = f"{int(w0) % 24:02d}:00-{int(w1) % 24:02d}:00"
    bad = live["runs"]["ignores_profile"]["faults"]["ignore_cp"]
    for name, col, lab in (("ignores_profile", PAL["violet"], f"{bad} ignores its profile (whole run)"),
                           ("stale_meter", PAL["accent"], f"meter stale {span}"),
                           ("disconnect", PAL["amber"], f"one charger offline {span}")):
        ax4.plot(t, live_series[name], color=col, lw=1.6, label=lab)
    ax4.axhline(site.fuse_a, color=PAL["text"], ls="--", lw=1.2)
    ax4.axvspan(w0, w1, color=PAL["grid"], alpha=0.6, lw=0)
    ax4.set_xlim(h0, h0 + len(t) / 60.0)
    ticks = list(range(h0, int(h0 + len(t) / 60.0) + 1, 4))
    ax4.set_xticks(ticks)
    ax4.set_xticklabels([f"{h % 24:02d}" for h in ticks])
    ax4.set_ylim(0, 95)
    ax4.text(h0 + 0.2, site.fuse_a + 2, f"main fuse {site.fuse_a:.0f} A", color=PAL["text"], fontsize=10)
    ax4.set_ylabel("highest phase current, A", fontsize=10.5)
    ax4.set_xlabel("hour, closed loop over OCPP 1.6J websockets", fontsize=10.5)
    over = sum(r["minutes_above_fuse"] for r in live["runs"].values())
    ax4.legend(loc="upper right", fontsize=10, frameon=False, labelcolor=PAL["text"])
    ax4.set_title(f"Fault runs over real OCPP: {over} minutes above the fuse", loc="left", fontsize=13, fontweight="bold")

    fig.text(0.055, 0.945, "ocpp-load-managed-charging: EV charging under the building's main fuse",
             fontsize=19, fontweight="bold", color=PAL["text"])
    fig.text(0.055, 0.905, SUBTITLE + " 8 representative weeks, one-minute steps.", fontsize=11.5, color=PAL["muted"])
    fig.savefig(path, dpi=100)
    plt.close(fig)


def make_post_figure(sims: dict, series: dict, path: Path) -> None:
    _style()
    site = SiteConfig()
    main = sims["main"]
    fig = plt.figure(figsize=(9.6, 7.0), dpi=100, facecolor=PAL["bg"])
    gs = fig.add_gridspec(2, 1, left=0.105, right=0.965, top=0.80, bottom=0.07, hspace=0.5, height_ratios=[1.3, 1])
    ax = fig.add_subplot(gs[0])
    i0 = worst_evening(series)
    _evening(ax, series, i0, site.fuse_a, 13, strategies=("none", "departure"), lw=2.6)
    ax.set_xlabel("hour, a winter evening to the next morning", fontsize=13)
    ax.set_ylabel("A, highest phase", fontsize=13)
    ax.set_ylim(0, max(140, float(series["none"][i0:i0 + 960].max()) + 10))
    ax.legend(loc="upper right", fontsize=13, frameon=False, labelcolor=PAL["text"])
    ax.set_title("12 chargers on a 3x63 A main fuse", loc="left", fontsize=15, color=PAL["text"])
    ax2 = fig.add_subplot(gs[1])
    order = [("none", "no load management", PAL["red"]), ("static", "static equal split", PAL["amber"]),
             ("departure", "dynamic controller", PAL["green"])]
    y = np.arange(3)[::-1] * 1.0
    vals = [main[s]["minutes_above_fuse"] for s, _, _ in order]
    ax2.set_axisbelow(True)
    ax2.barh(y, vals, color=[c for _, _, c in order], height=0.42)
    for yy, v, (s, name, _) in zip(y, vals, order, strict=True):
        ax2.text(0, yy + 0.3, f"{name}: {v} min above, {main[s]['energy_delivered_pct']:.0f} % of energy delivered",
                 va="bottom", fontsize=13.5, color=PAL["text"])
    ax2.set_yticks([])
    ax2.set_ylim(-0.4, 2.85)
    ax2.set_xlim(0, max(vals) * 1.05)
    ax2.tick_params(axis="x", labelsize=12)
    ax2.set_title("8 simulated weeks, one-minute mean current", loc="left", fontsize=15, color=PAL["text"])
    fig.text(0.035, 0.93, "EV charging without a bigger grid connection", fontsize=19, fontweight="bold", color=PAL["text"])
    fig.text(0.035, 0.89, "Simulated building and cars, 8 weeks, OCPP 1.6J controller.", fontsize=15, color=PAL["muted"])
    fig.savefig(path, dpi=100, facecolor=PAL["bg"])
    plt.close(fig)


# ---------------------------------------------------------------- text outputs
def summary_md(sims: dict, live: dict) -> str:
    cols = [("minutes_above_fuse", "min above fuse"), ("seconds_above_fuse_before_reaction", "s above fuse before reaction"),
            ("max_phase_a", "max phase A"), ("peak_kw", "peak kW"), ("energy_delivered_pct", "energy by departure %"),
            ("sessions_fully_served_pct", "sessions fully served %"), ("worst_session_pct", "worst session %"),
            ("worst_car_pct", "worst car %"), ("jain_sessions", "Jain index"), ("pauses_per_session", "pauses / session"),
            ("ctrl_us_p50", "ctrl p50 us"), ("ctrl_us_p99", "ctrl p99 us")]
    out = ["# Results (simulated)", ""]
    titles = {"main": "12 chargers, 3x63 A (main)", "fuse80": "12 chargers, 3x80 A", "all_flats": "24 chargers, 3x63 A"}
    for scen, rows in sims.items():
        any_m = next(iter(rows.values()))
        out += [f"## {titles[scen]}: {any_m['sessions']} sessions, {any_m['requested_mwh']} MWh requested, "
                f"{any_m['minutes']} minutes; base load alone above fuse: {any_m['minutes_base_alone_above_fuse']} min", ""]
        out.append("| strategy | " + " | ".join(c[1] for c in cols) + " |")
        out.append("|---" * (len(cols) + 1) + "|")
        for s, m in rows.items():
            out.append(f"| {LABELS[s]} | " + " | ".join("-" if m[c] is None else str(m[c]) for c, _ in cols) + " |")
        out.append("")
    out += [f"## Closed loop over OCPP 1.6J (day {live['day']}, from {live['start']}, {live['minutes']} virtual minutes)", ""]
    lc = ["minutes_above_fuse", "seconds_above_fuse_before_reaction", "max_phase_a", "sessions", "energy_delivered_pct",
          "profiles_sent", "profiles_accepted", "meter_values", "cycle_ms_p50", "cycle_ms_p95", "cycle_ms_max", "stale_cycles"]
    out.append("| run | " + " | ".join(lc) + " | events |")
    out.append("|---" * (len(lc) + 2) + "|")
    for name, r in live["runs"].items():
        out.append(f"| {name} | " + " | ".join(str(r[c]) for c in lc) + " | " + "; ".join(r["events"]) + " |")
    return "\n".join(out) + "\n"


def linkedin(sims: dict, live: dict) -> dict:
    m = sims["main"]
    none, static, dyn = m["none"], m["static"], m["departure"]
    lat = live["runs"]["normal"]
    post = (
        "Twelve parking spaces want chargers, the building has a 3x63 A main fuse, and a bigger grid connection is "
        "expensive and slow.\n\n"
        "I built an open-source OCPP 1.6J central system that measures the building and sends every charger a "
        "SetChargingProfile each minute, so that the flats plus all cars stay under the fuse. It is phase aware "
        "(single-phase cars), never gives a car less than 6 A (it pauses instead) and shares fairly by equal share, "
        "first come or departure time.\n\n"
        f"On 8 simulated weeks of a 24-flat building with 12 chargers, without load management the one-minute current "
        f"was above 63 A for {none['minutes_above_fuse']} minutes. A static equal split (fixed reserve for the flats) was "
        f"above it for {static['minutes_above_fuse']} minutes and its worst-served session got {static['worst_session_pct']:.0f} % "
        f"of its request. The dynamic controller: {dyn['minutes_above_fuse']} minutes above, "
        f"{dyn['energy_delivered_pct']:.0f} % of the requested energy delivered by departure, building peak "
        f"{dyn['peak_kw']:.0f} kW instead of {none['peak_kw']:.0f} kW. Over real websockets one control cycle for up to "
        f"{lat['max_profiles_per_cycle']} active chargers took {lat['cycle_ms_p50']:.0f} ms (median). An offline charger, a "
        "stale meter and a charger ignoring its profile are tested.\n\n"
        "Not verified: no real charger, car or building meter yet; the building and the cars are simulated.\n\n"
        "Which charger behaviour would you test first on real hardware?\n\n"
        f"Code: {REPO}"
    )
    return {
        "claim": "Open-source OCPP controller: EV charging in a block of flats without a bigger fuse.",
        "data_label": "simulated data",
        "stats": [
            [f"{none['minutes_above_fuse']}", "min above fuse, no load management", "red"],
            [f"{dyn['minutes_above_fuse']}", "min above fuse, dynamic controller", "green"],
            [f"{dyn['peak_kw']:.0f} kW", f"building peak (none: {none['peak_kw']:.0f} kW)", "accent"],
        ],
        "limit": "Simulated building and cars; never run on real chargers or a real building meter.",
        "alt": ("Dark chart. Top: highest phase current on a winter evening; without load management it rises far above "
                "the dashed 63 A fuse line, the dynamic controller stays just under it. Bottom: minutes above the fuse "
                "in 8 weeks for no load management, static split and the dynamic controller, with energy delivered."),
        "hashtags": ["#EVCharging", "#OCPP", "#LoadManagement", "#SmartCharging", "#IoT", "#OpenSource"],
        "post": post,
    }


def run_all(out_dir: Path, docs_dir: Path, blocks=BLOCKS, block_days=BLOCK_DAYS, live_minutes: int = 1440,
            workers: int = 2) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    sims, series = run_sims(blocks, block_days, workers)
    live, live_series = run_live_suite(live_minutes)
    meta = {"seed": SEED, "blocks_start_day": list(blocks), "block_days": block_days, "site": asdict(SiteConfig()),
            "scenarios": SCENARIOS, "reaction_s": 10.0, "data": "simulated"}
    (out_dir / "metrics.json").write_text(json.dumps({"meta": meta, "sims": sims, "live": live}, indent=1))
    np.savez_compressed(out_dir / "timeseries_cache.npz", **{f"sim_{k}": v for k, v in series.items()},
                        **{f"live_{k}": v for k, v in live_series.items()})
    (out_dir / "summary.md").write_text(summary_md(sims, live))
    if blocks == BLOCKS and block_days == BLOCK_DAYS:
        make_figure(sims, series, live, live_series, docs_dir / "figure.png")
        make_post_figure(sims, series, docs_dir / "post-figure.png")
        (docs_dir / "linkedin.json").write_text(json.dumps(linkedin(sims, live), indent=2))
    return {"sims": sims, "live": live}



def redraw(out_dir: Path, docs_dir: Path) -> None:
    """Redraw figures and linkedin.json from results/metrics.json + the time-series cache of the last demo run."""
    m = json.loads((out_dir / "metrics.json").read_text())
    z = np.load(out_dir / "timeseries_cache.npz")
    series = {k[4:]: z[k] for k in z.files if k.startswith("sim_")}
    live_series = {k[5:]: z[k] for k in z.files if k.startswith("live_")}
    make_figure(m["sims"], series, m["live"], live_series, docs_dir / "figure.png")
    make_post_figure(m["sims"], series, docs_dir / "post-figure.png")
    (docs_dir / "linkedin.json").write_text(json.dumps(linkedin(m["sims"], m["live"]), indent=2))
