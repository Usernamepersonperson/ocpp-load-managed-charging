"""Simulated building: per-phase base load of the flats and common services, and the EV sessions of the residents.

Everything here is SIMULATED and seeded (no measured data). One-minute resolution, day 0 is a Monday.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .config import MIN_PER_DAY, VOLTAGE, SiteConfig

KWH_PER_KM = 0.19
# calibration: about 2.7 MWh per flat and year including the heat-pump share (simulated building, ~64 MWh/a)
BASE_SCALE = 0.68


@dataclass(frozen=True)
class Session:
    charger: int
    arrival: int  # minute index (inclusive)
    departure: int  # minute index (exclusive)
    request_kwh: float  # energy the driver wants, capped at what the car could take at full power while plugged in
    phases: int  # 1 or 3
    car_max_a: float


@dataclass
class BuildingData:
    base_a: np.ndarray  # (minutes, 3) base load current per building phase, A
    temp_c: np.ndarray  # (days,) daily mean outdoor temperature, simulated
    sessions: list[Session]
    n_days: int
    seed: int

    @property
    def minutes(self) -> int:
        return self.base_a.shape[0]


def _add(arr: np.ndarray, start: int, dur: int, phase_kw: tuple[float, float, float]) -> None:
    end = min(start + dur, arr.shape[0])
    if start >= arr.shape[0] or end <= start:
        return
    arr[start:end] += np.asarray(phase_kw) * 1000.0 / VOLTAGE


def _clock(rng: np.random.Generator, mean_h: float, sd_min: float, lo_h: float, hi_h: float) -> int:
    """Minute of day from a normal distribution truncated to [lo_h, hi_h] (resampled, so no pile-up at the bounds)."""
    for _ in range(20):
        m = rng.normal(mean_h * 60.0, sd_min)
        if lo_h * 60.0 <= m <= hi_h * 60.0:
            return int(m)
    return int(rng.uniform(lo_h * 60.0, hi_h * 60.0))


def daily_temperature(n_days: int, rng: np.random.Generator) -> np.ndarray:
    """Seasonal daily mean temperature for a Swiss Plateau town (simulated): ~0 C in January, ~19 C in July."""
    d = np.arange(n_days)
    seasonal = 9.5 - 9.5 * np.cos(2 * np.pi * (d - 20) / 365.0)
    noise = np.zeros(n_days)
    for i in range(1, n_days):
        noise[i] = 0.8 * noise[i - 1] + rng.normal(0, 1.8)
    return seasonal + noise


def base_load(cfg: SiteConfig, n_days: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Per-phase base load (A) of n_flats flats, a shared laundry room, a heat pump and common services."""
    n = n_days * MIN_PER_DAY
    a = np.zeros((n, 3))
    temp = daily_temperature(n_days, rng)
    hours = np.arange(MIN_PER_DAY) / 60.0
    for day in range(n_days):
        t0 = day * MIN_PER_DAY
        weekend = day % 7 >= 5
        winter = 0.5 + 0.5 * math.cos(2 * math.pi * (day - 355) / 365.0)  # 1 in late December, 0 in June
        # heat pump (space heating, modulating, 3-phase) + hot-water charging twice a day
        t_hour = temp[day] + 4.0 * np.sin(2 * np.pi * (hours - 9.0) / 24.0)
        q_th = 0.8 * np.clip(16.0 - t_hour, 0.0, None)
        cop = np.clip(2.3 + 0.075 * t_hour, 2.0, 4.5)
        a[t0:t0 + MIN_PER_DAY] += (q_th / cop / 3.0 * 1000.0 / VOLTAGE)[:, None]
        for start_h, dur in ((3.0, 90), (13.0, 60)):
            _add(a, t0 + int(start_h * 60) + int(rng.integers(0, 20)), dur, (2.0, 2.0, 2.0))
        # common services: ventilation, stairwell light, elevator trips
        a[t0:t0 + MIN_PER_DAY] += 0.25 * 1000.0 / VOLTAGE
        for _ in range(rng.poisson(70)):
            m = _clock(rng, 13.0, 300, 6.0, 23.0)
            _add(a, t0 + m, 1, (1.5, 1.5, 1.5))
        # shared laundry room: 2 washers + 2 dryers, booked 07:00-21:00
        for machine in range(4):
            for _ in range(rng.poisson(3.0 if not weekend else 3.6)):
                m = int(rng.integers(7 * 60, 20 * 60))
                if machine < 2:  # washer: heating then motor
                    ph = machine
                    kw = [0.0, 0.0, 0.0]
                    kw[ph] = 2.3
                    _add(a, t0 + m, 20, tuple(kw))
                    kw[ph] = 0.3
                    _add(a, t0 + m + 20, 70, tuple(kw))
                else:  # dryer, three-phase
                    _add(a, t0 + m + 95, 90, (1.0, 1.0, 1.0))
        # flats
        for f in range(cfg.n_flats):
            p0, p1, p2 = f % 3, (f + 1) % 3, (f + 2) % 3

            def on(ph: int, kw: float) -> tuple[float, float, float]:
                v = [0.0, 0.0, 0.0]
                v[ph] = kw
                return (v[0], v[1], v[2])

            def hob_kw(kw: float, pa: int = p0, pb: int = p1) -> tuple[float, float, float]:  # hob on a flat-specific phase pair
                v = [0.0, 0.0, 0.0]
                v[pa] = v[pb] = kw / 2
                return (v[0], v[1], v[2])

            a[t0:t0 + MIN_PER_DAY, p0] += (0.05 + 0.012 * (f % 5)) * 1000.0 / VOLTAGE  # fridge, standby
            # evening lights and plug loads, longer in winter
            start = _clock(rng, 17.5 - 1.5 * winter, 40, 15.0, 20.0)
            end = _clock(rng, 22.8, 45, 21.0, 24.0)
            _add(a, t0 + start, max(end - start, 0), on(p1, 0.12 + 0.18 * winter + rng.uniform(0, 0.1)))
            # breakfast: kettle / coffee machine
            if rng.random() < 0.8:
                m = _clock(rng, 8.8 if weekend else 7.0, 40, 5.5, 11.0)
                _add(a, t0 + m, int(rng.integers(3, 6)), on(p2, 2.0))
            # lunch
            if rng.random() < (0.55 if weekend else 0.2):
                m = _clock(rng, 12.0, 25, 11.0, 13.5)
                _add(a, t0 + m, int(rng.integers(15, 35)), hob_kw(rng.uniform(0.6, 1.6)))
            # dinner: hob (two phases), sometimes oven
            if rng.random() < 0.7:
                m = _clock(rng, 18.7, 65, 16.5, 21.5)
                _add(a, t0 + m, int(rng.integers(20, 50)), hob_kw(rng.uniform(0.8, 2.0)))
                if rng.random() < 0.25 + 0.15 * winter:  # oven, thermostat duty cycle averaged over the minute
                    _add(a, t0 + m - 10, int(rng.integers(35, 65)), on(p2, 1.2))
                if rng.random() < 0.55:  # dishwasher after dinner
                    d0 = m + int(rng.integers(40, 120))
                    _add(a, t0 + d0, 25, on(p0, 2.0))
                    _add(a, t0 + d0 + 25, 60, on(p0, 0.15))
            # TV / computer during the day on home-office days
            if rng.random() < 0.35:
                _add(a, t0 + 8 * 60 + int(rng.integers(0, 90)), int(rng.integers(240, 480)), on(p1, 0.15))
    return a * BASE_SCALE, temp


PROFILES = ("commuter", "homeoffice", "retired")


def sessions(cfg: SiteConfig, n_days: int, temp: np.ndarray, rng: np.random.Generator) -> list[Session]:
    """One car per charger. Trips per day by driver profile; the car is plugged in on most evenings.

    request_kwh = energy driven since the last plug-in (winter +20 %), capped at the battery size and at what the
    car could take at full power while it is plugged in (so 100 % is always reachable without a fuse)."""
    out: list[Session] = []
    for c in range(cfg.n_chargers):
        k = c % 12  # the same mix repeats for every 12 chargers
        prof = PROFILES[0] if k < 7 else (PROFILES[1] if k < 10 else PROFILES[2])
        phases = 1 if k in (1, 4, 8, 11) else 3  # a third of the cars charge on one phase only
        car_max = 16.0
        battery = float(rng.choice([50.0, 64.0, 77.0]))
        pending_kwh = 0.0
        home_from = 0  # minute the car came home and is available
        for day in range(n_days):
            weekend = day % 7 >= 5
            winter = 1.2 if temp[day] < 5 else 1.0
            if prof == "commuter" and not weekend:
                dep = _clock(rng, 7.1, 25, 5.8, 8.8)
                arr = _clock(rng, 18.0, 55, 15.5, 22.5)
                km = rng.lognormal(math.log(48), 0.35)
            elif prof == "homeoffice" and not weekend and rng.random() < 0.5:
                dep = _clock(rng, 8.0, 50, 6.5, 11.0)
                arr = _clock(rng, 15.5, 120, 11.5, 21.0)
                km = rng.lognormal(math.log(35), 0.5)
            elif rng.random() < (0.55 if weekend else 0.4):
                dep = _clock(rng, 10.0, 90, 7.5, 15.0)
                arr = dep + int(rng.integers(90, 480))
                km = rng.lognormal(math.log(30), 0.6)
            else:
                continue
            dep_abs = day * MIN_PER_DAY + dep
            # close the previous home period: was the car plugged in?
            plugged_start = home_from
            if dep_abs - plugged_start >= 30 and pending_kwh > 0.5:
                plug_p = 0.85 if pending_kwh > 15 else 0.6
                if rng.random() < plug_p:
                    dur_h = (dep_abs - plugged_start) / 60.0
                    full_kw = car_max * VOLTAGE * phases / 1000.0
                    req = min(pending_kwh, battery * 0.8, full_kw * dur_h)
                    out.append(Session(c, plugged_start, dep_abs, round(req, 3), phases, car_max))
                    pending_kwh = max(0.0, pending_kwh - req)
            pending_kwh = min(pending_kwh + km * KWH_PER_KM * winter, battery * 0.8)
            home_from = day * MIN_PER_DAY + min(max(arr, dep + 30), MIN_PER_DAY - 1)
    out.sort(key=lambda s: (s.arrival, s.charger))
    return out


def build(cfg: SiteConfig | None = None, n_days: int = 364, seed: int = 7) -> BuildingData:
    """Seeded simulated building: base load + EV sessions for n_days (default 52 weeks)."""
    cfg = cfg or SiteConfig()
    rng = np.random.default_rng(seed)
    base, temp = base_load(cfg, n_days, rng)
    sess = sessions(cfg, n_days, temp, rng)
    return BuildingData(base_a=base, temp_c=temp, sessions=sess, n_days=n_days, seed=seed)
