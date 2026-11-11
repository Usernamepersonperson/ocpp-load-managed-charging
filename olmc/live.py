"""Closed loop over real OCPP 1.6J websockets on localhost: central system + controller, 12 simulated chargers,
a simulated building meter. One virtual minute per cycle; optional faults (disconnect, stale meter, a charger
that ignores its profile).

Per virtual minute t:
  1. arrivals and departures (StartTransaction + DataTransfer / StopTransaction);
  2. the chargers report MeterValues with the current they draw under their present limit;
  3. the meter publishes base[t] + those currents (unless the stale fault freezes it);
  4. controller cycle (timed): read meter + chargers, decide, SetChargingProfile to every session, wait for replies;
  5. cars charge `reaction_s` seconds under the old limits and the rest of the minute under the new ones;
     the one-minute mean per phase is compared with the fuse.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field

import numpy as np
from ocpp.v16.enums import ChargePointStatus

from .chargepoint import Car, SimChargePoint, open_connection
from .config import SiteConfig
from .controller import Controller, MeterReading
from .csms import CentralSystem
from .data import BuildingData, Session


@dataclass
class Faults:
    disconnect_cp: str | None = None  # charger that loses its websocket
    disconnect_at: int = 0  # minutes after the start
    reconnect_at: int | None = None
    stale_from: int | None = None  # meter frozen from this minute ...
    stale_to: int | None = None  # ... to this minute (exclusive)
    ignore_cp: str | None = None  # charger that accepts profiles but ignores them


@dataclass
class LiveResult:
    minutes: int = 0
    minutes_above_fuse: int = 0
    seconds_above_fuse_before_reaction: float = 0.0
    max_phase_a: float = 0.0
    sessions: int = 0
    requested_kwh: float = 0.0
    delivered_kwh: float = 0.0
    profiles_sent: int = 0
    profiles_accepted: int = 0
    meter_values: int = 0
    cycle_ms: list[float] = field(default_factory=list)
    cycle_profiles: list[int] = field(default_factory=list)
    events: list[tuple[float, str, str]] = field(default_factory=list)
    stale_cycles: int = 0
    max_phase_series: list[float] = field(default_factory=list)
    ev_series: list[float] = field(default_factory=list)

    def summary(self) -> dict:
        c = np.asarray(self.cycle_ms) if self.cycle_ms else np.zeros(1)
        return {
            "minutes": self.minutes,
            "minutes_above_fuse": self.minutes_above_fuse,
            "seconds_above_fuse_before_reaction": round(self.seconds_above_fuse_before_reaction),
            "max_phase_a": round(self.max_phase_a, 1),
            "sessions": self.sessions,
            "requested_kwh": round(self.requested_kwh, 1),
            "delivered_kwh": round(self.delivered_kwh, 1),
            "energy_delivered_pct": round(100 * self.delivered_kwh / self.requested_kwh, 1) if self.requested_kwh else 100.0,
            "profiles_sent": self.profiles_sent,
            "profiles_accepted": self.profiles_accepted,
            "meter_values": self.meter_values,
            "cycle_ms_p50": round(float(np.percentile(c, 50)), 1),
            "cycle_ms_p95": round(float(np.percentile(c, 95)), 1),
            "cycle_ms_max": round(float(c.max()), 1),
            "cycles_with_profiles": len(self.cycle_ms),
            "max_profiles_per_cycle": max(self.cycle_profiles, default=0),
            "stale_cycles": self.stale_cycles,
            "events": [f"t+{int(t)} min {cp}: {what}" for t, cp, what in self.events],
        }


async def _connect(url: str, cp: str, site: SiteConfig, ignore: bool, keep: SimChargePoint | None = None):
    conn = await open_connection(url, cp)
    sim = SimChargePoint(cp, conn, max_a=site.charger_max_a, min_a=site.min_a, ignore_profiles=ignore)
    if keep is not None:  # reconnect: same charger hardware, same car and transaction
        for attr in ("default_limit", "tx_limit", "car", "transaction_id", "energy_wh", "profiles_accepted", "profiles_rejected"):
            setattr(sim, attr, getattr(keep, attr))
    task = asyncio.create_task(sim.start())
    await sim.boot()
    return sim, conn, task


async def run_live(site: SiteConfig, data: BuildingData, start_min: int, minutes: int, policy: str = "departure",
                   faults: Faults | None = None, port: int = 0, reaction_s: float = 10.0, tick_s: float = 0.0) -> LiveResult:
    faults = faults or Faults()
    clock = {"t": float(start_min)}
    cs = CentralSystem("127.0.0.1", port, clock=lambda: clock["t"], failsafe_a=site.failsafe_a)
    await cs.start()
    url = f"ws://127.0.0.1:{cs.port}"
    ids = [f"CP{c + 1:02d}" for c in range(site.n_chargers)]
    for c, cp in enumerate(ids):
        cs.register(cp, site.rotation(c), site.charger_max_a)
    cps: dict[str, SimChargePoint] = {}
    conns: dict[str, object] = {}
    tasks: dict[str, asyncio.Task] = {}
    for cp in ids:
        cps[cp], conns[cp], tasks[cp] = await _connect(url, cp, site, cp == faults.ignore_cp)
        await cs.wait_for(cp)
    ctrl = Controller(site, "dynamic", policy)
    res = LiveResult()
    frac = min(max(reaction_s / 60.0, 0.0), 1.0)
    end = start_min + minutes
    arrivals: dict[int, list[Session]] = {}
    for s in data.sessions:
        if start_min <= s.arrival < end and s.departure <= end:
            arrivals.setdefault(s.arrival, []).append(s)
    departures: dict[int, list[str]] = {}
    online = set(ids)
    meter = MeterReading(float(start_min - 1), tuple(data.base_a[start_min - 1]))
    try:
        for t in range(start_min, end):
            clock["t"] = float(t)
            k = t - start_min
            # faults: connection loss and recovery
            if faults.disconnect_cp and k == faults.disconnect_at and faults.disconnect_cp in online:
                cp = faults.disconnect_cp
                await conns[cp].close()
                with contextlib.suppress(Exception):
                    await tasks[cp]
                online.discard(cp)
                for _ in range(100):
                    if not cs.records[cp].connected:
                        break
                    await asyncio.sleep(0.01)
            if faults.disconnect_cp and faults.reconnect_at is not None and k == faults.reconnect_at:
                cp = faults.disconnect_cp
                cps[cp], conns[cp], tasks[cp] = await _connect(url, cp, site, cp == faults.ignore_cp, keep=cps[cp])
                await cs.wait_for(cp)
                online.add(cp)
            # 1. sessions
            for cp in departures.pop(t, []):
                if cp in online:
                    await cps[cp].unplug()
                else:
                    cps[cp].car, cps[cp].transaction_id = None, None
            for s in arrivals.get(t, []):
                cp = ids[s.charger]
                if cps[cp].car is not None or cp not in online:
                    continue
                await cps[cp].plug_in(Car(s.request_kwh, s.phases, s.car_max_a), departure_min=float(s.departure))
                departures.setdefault(s.departure, []).append(cp)
                res.sessions += 1
                res.requested_kwh += s.request_kwh
            # 2. MeterValues under the present limits
            old = {cp: sim.current_draw() for cp, sim in cps.items()}
            await asyncio.gather(*(cps[cp].send_meter_values(cps[cp].phase_currents(old[cp])) for cp in ids
                                   if cp in online and cps[cp].transaction_id is not None))
            # 3. building meter
            ev_old = [0.0, 0.0, 0.0]
            for c, cp in enumerate(ids):
                bp = Controller.to_building(site.rotation(c), cps[cp].phase_currents(old[cp]))
                for p in range(3):
                    ev_old[p] += bp[p]
            b = data.base_a[t]
            stale = faults.stale_from is not None and faults.stale_from <= k < (faults.stale_to or minutes)
            if not stale:
                meter = MeterReading(float(t), (b[0] + ev_old[0], b[1] + ev_old[1], b[2] + ev_old[2]))
            if any(b[p] + ev_old[p] > site.fuse_a + 1e-6 for p in range(3)):
                res.seconds_above_fuse_before_reaction += reaction_s
            # 4. controller cycle over OCPP
            tic = time.perf_counter()
            limits = ctrl.step(float(t), meter, cs.observations())
            await cs.apply(limits)
            if limits:  # latency of cycles that sent profiles: meter read -> decision -> all replies received
                res.cycle_ms.append((time.perf_counter() - tic) * 1000.0)
                res.cycle_profiles.append(len(limits))
            if ctrl.last.meter_stale:
                res.stale_cycles += 1
            # 5. energy over the minute and the one-minute mean per phase
            ev_mean = [0.0, 0.0, 0.0]
            for c, cp in enumerate(ids):
                sim = cps[cp]
                m_old = sim.charge(old[cp], 60.0 * frac)
                m_new = sim.charge(sim.current_draw(), 60.0 * (1.0 - frac))
                mean_a = m_old * frac + m_new * (1.0 - frac)
                bp = Controller.to_building(site.rotation(c), sim.phase_currents(mean_a) if mean_a > 0 else (0.0, 0.0, 0.0))
                for p in range(3):
                    ev_mean[p] += bp[p]
                if sim.car is not None and sim.car.full and sim.transaction_id is not None and cp in online \
                        and cs.records[cp].status != "SuspendedEV":
                    await sim.status(ChargePointStatus.suspended_ev)
            tot = [b[p] + ev_mean[p] for p in range(3)]
            res.max_phase_series.append(max(tot))
            res.ev_series.append(sum(ev_mean))
            res.max_phase_a = max(res.max_phase_a, max(tot))
            if any(x > site.fuse_a + 1e-6 for x in tot):
                res.minutes_above_fuse += 1
            res.minutes += 1
            if tick_s > 0:
                await asyncio.sleep(tick_s)
        res.delivered_kwh = sum(sim.energy_wh for sim in cps.values()) / 1000.0  # every session ends inside the window
    finally:
        res.profiles_sent = sum(r.profiles_sent for r in cs.records.values())
        res.profiles_accepted = sum(r.profiles_accepted for r in cs.records.values())
        res.meter_values = sum(r.meter_values for r in cs.records.values())
        res.events = [(t - start_min, cp, what) for t, cp, what in ctrl.events]
        for cp in ids:
            if cp in online:
                with contextlib.suppress(Exception):
                    await conns[cp].close()
        for task in tasks.values():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        await cs.stop()
    return res

