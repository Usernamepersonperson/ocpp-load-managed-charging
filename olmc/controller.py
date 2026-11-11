"""Load-management controller: one decision per interval, no I/O.

Inputs per cycle: the building meter (total current per phase, with a timestamp) and what each charger reported
(connected, transaction, per-phase current from MeterValues, driver's departure time and energy request).
Output: a current limit (A per phase) for every controllable session; 0 means paused.

Safety rules:
- base load = meter total - current of the sessions we control (so offline or misbehaving chargers count as base);
- an offline charger keeps its last limit (or the failsafe TxDefaultProfile if idle): reserve the difference;
- a charger that draws more than its limit is flagged non-compliant and its charger rating is reserved;
- a stale meter reading switches to an assumed worst-case base load until fresh data arrives;
- single-phase cars are detected from the per-phase currents; until then every car counts on all three phases.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .allocate import POLICIES, Demand, allocate
from .config import VOLTAGE, SiteConfig

MODES = ("dynamic", "static")
_ZERO = (0.0, 0.0, 0.0)


@dataclass
class MeterReading:
    t_min: float
    phase_a: tuple[float, float, float]  # building total per phase


@dataclass
class ChargerObs:
    cp_id: str
    rotation: int  # building phase wired to the charger's L1
    connected: bool = True
    in_session: bool = False  # transaction running and the car still wants energy
    session_id: int = 0  # changes with every new transaction
    arrival_min: float = 0.0
    departure_min: float | None = None
    need_kwh: float | None = None
    delivered_kwh: float = 0.0
    measured_a: tuple[float, float, float] = (0.0, 0.0, 0.0)  # charger L1, L2, L3 from the latest MeterValues
    max_a: float = 16.0


@dataclass
class _CpState:
    session_id: int = -1
    last_limit: float | None = None
    phases: int | None = None  # detected: 1 or 3
    running: bool = False
    noncompliant: bool = False
    last_measured_bp: tuple[float, float, float] = (0.0, 0.0, 0.0)
    pauses: int = 0


@dataclass
class CycleInfo:
    avail_a: tuple[float, ...] = ()
    meter_stale: bool = False
    offline: list[str] = field(default_factory=list)
    noncompliant: list[str] = field(default_factory=list)


class Controller:
    def __init__(self, site: SiteConfig, mode: str = "dynamic", policy: str = "equal", phase_aware: bool = True):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if policy not in POLICIES:
            raise ValueError(f"policy must be one of {POLICIES}")
        self.site, self.mode, self.policy, self.phase_aware = site, mode, policy, phase_aware
        self.state: dict[str, _CpState] = {}
        self.last: CycleInfo = CycleInfo()
        self.events: list[tuple[float, str, str]] = []

    # ---- helpers
    @staticmethod
    def to_building(rotation: int, charger_a: tuple[float, float, float]) -> tuple[float, float, float]:
        """Charger L1..L3 currents -> building phases (L1 of the charger sits on building phase `rotation`)."""
        a, b, c = charger_a
        r = rotation % 3
        if r == 0:
            return (a, b, c)
        if r == 1:
            return (c, a, b)
        return (b, c, a)

    def _weights(self, ob: ChargerObs, st: _CpState) -> tuple[float, float, float]:
        if st.phases == 1:
            w = [0.0, 0.0, 0.0]
            w[ob.rotation % 3] = 1.0
            return (w[0], w[1], w[2])
        return (1.0, 1.0, 1.0)

    def _event(self, t: float, cp: str, what: str) -> None:
        self.events.append((t, cp, what))

    # ---- one control cycle
    def step(self, now_min: float, meter: MeterReading | None, chargers: list[ChargerObs]) -> dict[str, float]:
        s = self.site
        info = CycleInfo()
        controllable: list[tuple[ChargerObs, _CpState]] = []
        ctrl_draw = [0.0, 0.0, 0.0]  # measured current of the sessions we control (building phases)
        reserve = [0.0, 0.0, 0.0]  # possible increase of chargers we cannot control
        reserve_full = [0.0, 0.0, 0.0]  # full cap of uncontrollable chargers (used when the meter is stale)
        states = self.state
        for ob in chargers:
            st = states.get(ob.cp_id)
            if st is None:
                st = states[ob.cp_id] = _CpState()
            if ob.connected and not ob.in_session and ob.measured_a == _ZERO:  # idle charger: nothing to do
                st.running = False
                st.last_measured_bp = _ZERO
                continue
            if ob.in_session and ob.session_id != st.session_id:  # new transaction: forget the old car
                st.session_id, st.last_limit, st.phases, st.running, st.noncompliant = ob.session_id, None, None, False, False
            if not ob.connected:
                info.offline.append(ob.cp_id)
                if ob.cp_id not in self.last.offline:
                    self._event(now_min, ob.cp_id, "offline")
                if st.session_id == ob.session_id and ob.in_session:
                    cap = st.last_limit if st.last_limit is not None else s.failsafe_a
                    w = self._weights(ob, st)
                else:
                    cap, w = s.failsafe_a, (1.0, 1.0, 1.0)
                for p in range(3):
                    reserve[p] += max(0.0, cap * w[p] - st.last_measured_bp[p])
                    reserve_full[p] += cap * w[p]
                continue
            if ob.cp_id in self.last.offline:
                self._event(now_min, ob.cp_id, "back online")
            bp = self.to_building(ob.rotation, ob.measured_a)
            st.last_measured_bp = bp
            if not ob.in_session:
                st.running = False
                continue
            l1, l2, l3 = ob.measured_a
            if st.phases is None and l1 > 1.0:
                st.phases = 1 if (l2 < 0.5 and l3 < 0.5) else 3
            expected = st.last_limit if st.last_limit is not None else s.failsafe_a  # new session: TxDefaultProfile
            if not st.noncompliant and max(ob.measured_a) > expected + s.noncompliant_tol_a:
                st.noncompliant = True
                self._event(now_min, ob.cp_id, "ignores profile")
            if st.noncompliant:
                info.noncompliant.append(ob.cp_id)
                w = self._weights(ob, st)
                for p in range(3):
                    reserve[p] += max(0.0, ob.max_a * w[p] - bp[p])
                    reserve_full[p] += ob.max_a * w[p]
                continue
            controllable.append((ob, st))
            for p in range(3):
                ctrl_draw[p] += bp[p]

        if self.mode == "static":
            avail = [s.fuse_a - s.static_reserve_a - reserve_full[p] for p in range(3)]
        elif meter is None or now_min - meter.t_min > s.stale_after_min:
            info.meter_stale = True
            if not self.last.meter_stale:
                self._event(now_min, "meter", "stale")
            avail = [s.target_a - s.fallback_base_a - reserve_full[p] for p in range(3)]
        else:
            avail = [s.target_a - (meter.phase_a[p] - ctrl_draw[p]) - reserve[p] for p in range(3)]
        info.avail_a = tuple(avail)

        rotate = int(round(now_min)) % max(s.rotate_every_min, 1) == 0
        demands: list[Demand] = []
        for ob, st in controllable:
            w = self._weights(ob, st)
            nph = int(sum(w))
            remaining = max((ob.need_kwh or 0.0) - ob.delivered_kwh, 0.0)
            full_kw = ob.max_a * VOLTAGE * nph / 1000.0
            if ob.departure_min is None or ob.need_kwh is None:
                need_a, laxity = 0.0, 1e9
            else:
                left = ob.departure_min - now_min
                need_a = ob.max_a if left <= 0 else min(ob.max_a, remaining * 1000.0 / (VOLTAGE * nph * left / 60.0))
                laxity = left - remaining / full_kw * 60.0
            order = {"equal": ob.delivered_kwh, "fifo": ob.arrival_min, "departure": laxity}[self.policy]
            if st.running and not rotate:
                order -= 1e12  # running cars keep running between rotation boundaries (no flapping)
            if self.phase_aware:
                weights: tuple[float, ...] = w
            else:
                weights = (float(nph),)
            demands.append(Demand(ob.cp_id, weights, ob.max_a, s.min_a, order, need_a))
        res = avail if self.phase_aware else [sum(avail)]
        limits = allocate(res, demands, self.policy)
        for ob, st in controllable:
            lim = limits[ob.cp_id]
            if st.running and lim < s.min_a:
                st.pauses += 1
            st.running = lim >= s.min_a
            st.last_limit = lim
        self.last = info
        return limits

    def pauses(self) -> int:
        return sum(st.pauses for st in self.state.values())
