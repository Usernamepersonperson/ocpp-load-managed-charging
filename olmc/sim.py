"""Minute-by-minute simulation of the building: every strategy runs on the same simulated base load and sessions.

Timing inside minute t: the base load steps to its new value; for `reaction_s` seconds the cars keep their old
limits (control cycle + charger and car response); then the controller, having read the meter and the chargers'
MeterValues, has new limits in place for the rest of the minute. Fuse check = one-minute mean current per phase.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .config import VOLTAGE, SiteConfig
from .controller import ChargerObs, Controller, MeterReading
from .data import BuildingData, Session

# name -> (mode, policy, phase_aware); None = no load management (every car at its maximum)
STRATEGIES: dict[str, tuple[str, str, bool] | None] = {
    "none": None,
    "static": ("static", "equal", True),
    "equal": ("dynamic", "equal", True),
    "fifo": ("dynamic", "fifo", True),
    "departure": ("dynamic", "departure", True),
    "equal_kw": ("dynamic", "equal", False),
}
LABELS = {
    "none": "No load management",
    "static": "Static equal split",
    "equal": "Dynamic, equal share",
    "fifo": "Dynamic, first come",
    "departure": "Dynamic, departure aware",
    "equal_kw": "Dynamic, kW budget only (not phase aware)",
}


@dataclass
class SessionOutcome:
    charger: int
    request_kwh: float
    delivered_kwh: float

    @property
    def served(self) -> float:
        return min(self.delivered_kwh / self.request_kwh, 1.0) if self.request_kwh > 1e-9 else 1.0


@dataclass
class SimResult:
    strategy: str
    t0: int
    total_a: np.ndarray  # (minutes, 3) building total per phase
    ev_a: np.ndarray  # (minutes, 3) EV part
    outcomes: list[SessionOutcome] = field(default_factory=list)
    latency_us: np.ndarray = field(default_factory=lambda: np.zeros(0))
    pauses: int = 0
    transient_s: float = 0.0  # seconds above the fuse before the cars followed new limits


@dataclass
class _Live:
    sess: Session
    delivered: float = 0.0
    limit: float = 0.0


def _draw(limit: float, s: Session, delivered: float, min_a: float) -> float:
    """Current the car draws under a limit: 0 below the minimum (paused) or when full, else up to its own maximum."""
    if limit < min_a - 1e-9 or delivered >= s.request_kwh - 1e-9:
        return 0.0
    return min(limit, s.car_max_a)


def _add_phase(acc: list[float], a: float, phases: int, rotation: int) -> None:
    if phases == 3:
        acc[0] += a
        acc[1] += a
        acc[2] += a
    else:
        acc[rotation % 3] += a


def simulate(site: SiteConfig, data: BuildingData, strategy: str, start_day: int = 0, n_days: int | None = None,
             reaction_s: float = 10.0) -> SimResult:
    """reaction_s: time from a base-load change until the cars follow new limits (control cycle + charger and car
    response). During it the cars keep their previous limits; those seconds above the fuse are counted separately."""
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    spec = STRATEGIES[strategy]
    ctrl = Controller(site, *spec) if spec else None
    n_days = data.n_days - start_day if n_days is None else n_days
    t0, t1 = start_day * 1440, (start_day + n_days) * 1440
    base = data.base_a[t0:t1].tolist()
    arrivals: dict[int, list[Session]] = {}
    for s in data.sessions:
        if t0 <= s.arrival < t1 and s.departure <= t1:
            arrivals.setdefault(s.arrival, []).append(s)
    obs = [ChargerObs(cp_id=f"CP{c + 1:02d}", rotation=site.rotation(c), max_a=site.charger_max_a) for c in range(site.n_chargers)]
    active: dict[int, _Live] = {}
    ev: list[list[float]] = []
    outcomes: list[SessionOutcome] = []
    lat: list[float] = []
    sid = 0
    transient_s = 0.0
    frac = min(max(reaction_s / 60.0, 0.0), 1.0)
    for t in range(t0, t1):
        for c in [c for c, lv in active.items() if lv.sess.departure <= t]:
            lv = active.pop(c)
            outcomes.append(SessionOutcome(c, lv.sess.request_kwh, lv.delivered))
            o = obs[c]
            o.in_session, o.measured_a = False, (0.0, 0.0, 0.0)
        for s in arrivals.get(t, ()):
            sid += 1
            active[s.charger] = _Live(s, limit=site.failsafe_a)
            o = obs[s.charger]
            o.in_session, o.session_id, o.arrival_min = True, sid, float(s.arrival)
            o.departure_min, o.need_kwh, o.delivered_kwh, o.measured_a = float(s.departure), s.request_kwh, 0.0, (0.0, 0.0, 0.0)
        # 1) cars draw under the limits they already have (a new car: the charger's TxDefaultProfile)
        old: dict[int, float] = {}
        ev_old = [0.0, 0.0, 0.0]
        for c, lv in active.items():
            s, o = lv.sess, obs[c]
            lim = s.car_max_a if ctrl is None else lv.limit
            a = _draw(lim, s, lv.delivered, site.min_a)
            old[c] = a
            o.measured_a = (a, a, a) if s.phases == 3 else (a, 0.0, 0.0)
            _add_phase(ev_old, a, s.phases, o.rotation)
        b = base[t - t0]
        lim_f = site.fuse_a + 1e-6
        if b[0] + ev_old[0] > lim_f or b[1] + ev_old[1] > lim_f or b[2] + ev_old[2] > lim_f:
            transient_s += reaction_s
        # 2) the controller reads the meter after the base-load change and sends new limits
        if ctrl is not None and active:
            tic = time.perf_counter()
            limits = ctrl.step(float(t), MeterReading(float(t), (b[0] + ev_old[0], b[1] + ev_old[1], b[2] + ev_old[2])), obs)
            lat.append((time.perf_counter() - tic) * 1e6)
            for c, lv in active.items():
                if obs[c].cp_id in limits:
                    lv.limit = limits[obs[c].cp_id]
        # 3) energy over the minute: old limits for the reaction time, new limits for the rest
        ev_mean = [0.0, 0.0, 0.0]
        for c, lv in active.items():
            s, o = lv.sess, obs[c]
            lim = s.car_max_a if ctrl is None else lv.limit
            kw_per_a = VOLTAGE * s.phases / 1000.0
            e_old = min(old[c] * kw_per_a * frac / 60.0, s.request_kwh - lv.delivered)
            lv.delivered += max(e_old, 0.0)
            a_new = _draw(lim, s, lv.delivered, site.min_a)
            e_new = min(a_new * kw_per_a * (1.0 - frac) / 60.0, s.request_kwh - lv.delivered)
            lv.delivered += max(e_new, 0.0)
            a_mean = (max(e_old, 0.0) + max(e_new, 0.0)) * 60.0 / kw_per_a
            o.delivered_kwh = lv.delivered
            if lv.delivered >= s.request_kwh - 1e-9:
                o.in_session = False  # car full (SuspendedEV), stays plugged in until departure
            _add_phase(ev_mean, a_mean, s.phases, o.rotation)
        ev.append(ev_mean)
    ev_arr = np.asarray(ev).reshape(-1, 3)
    pauses = ctrl.pauses() if ctrl else 0
    return SimResult(strategy, t0, data.base_a[t0:t1] + ev_arr, ev_arr, outcomes, np.asarray(lat), pauses, transient_s)


def jain(x) -> float:
    """Jain fairness index: 1 = everyone gets the same share, 1/n = one gets everything."""
    x = np.asarray(x, dtype=float)
    if x.size == 0 or not np.any(x):
        return 1.0 if x.size else float("nan")
    return float(x.sum() ** 2 / (x.size * (x ** 2).sum()))


def metrics(site: SiteConfig, r: SimResult, base: np.ndarray | None = None) -> dict:
    over = (r.total_a > site.fuse_a + 1e-6).any(axis=1)
    req = sum(o.request_kwh for o in r.outcomes)
    dlv = sum(min(o.delivered_kwh, o.request_kwh) for o in r.outcomes)
    served = [o.served for o in r.outcomes]
    per_car: dict[int, list[float]] = {}
    for o in r.outcomes:
        acc = per_car.setdefault(o.charger, [0.0, 0.0])
        acc[0] += min(o.delivered_kwh, o.request_kwh)
        acc[1] += o.request_kwh
    car_share = {c: v[0] / v[1] for c, v in per_car.items() if v[1] > 0}
    lat = r.latency_us
    out = {
        "minutes": int(r.total_a.shape[0]),
        "minutes_above_fuse": int(over.sum()),
        "max_phase_a": round(float(r.total_a.max()), 1),
        "peak_kw": round(float(r.total_a.sum(axis=1).max() * VOLTAGE / 1000.0), 1),
        "sessions": len(r.outcomes),
        "requested_mwh": round(req / 1000.0, 2),
        "energy_delivered_pct": round(100.0 * dlv / req, 2) if req else 100.0,
        "sessions_fully_served_pct": round(100.0 * float(np.mean([s >= 0.99 for s in served])), 1) if served else 100.0,
        "worst_session_pct": round(100.0 * min(served), 1) if served else 100.0,
        "worst_car_pct": round(100.0 * min(car_share.values()), 1) if car_share else 100.0,
        "jain_sessions": round(jain(served), 3),
        "seconds_above_fuse_before_reaction": round(r.transient_s),
        "pauses_per_session": round(r.pauses / len(r.outcomes), 2) if r.outcomes else 0.0,
        "ctrl_us_p50": round(float(np.percentile(lat, 50)), 1) if lat.size else None,
        "ctrl_us_p99": round(float(np.percentile(lat, 99)), 1) if lat.size else None,
        "ctrl_us_max": round(float(lat.max()), 1) if lat.size else None,
    }
    if base is not None:
        b = base[r.t0:r.t0 + r.total_a.shape[0]]
        out["minutes_base_alone_above_fuse"] = int((b > site.fuse_a + 1e-6).any(axis=1).sum())
    return out
