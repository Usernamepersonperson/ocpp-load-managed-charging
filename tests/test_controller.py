import pytest

from olmc.config import SiteConfig
from olmc.controller import ChargerObs, Controller, MeterReading

SITE = SiteConfig(fuse_a=63.0, margin_a=3.0, static_reserve_a=45.0, fallback_base_a=55.0, failsafe_a=6.0)


def car(cp: str, rot: int = 0, sid: int = 1, measured=(0.0, 0.0, 0.0), **kw) -> ChargerObs:
    return ChargerObs(cp_id=cp, rotation=rot, in_session=True, session_id=sid, measured_a=measured, **kw)


def meter(t: float, base: float, ev=(0.0, 0.0, 0.0)) -> MeterReading:
    return MeterReading(t, (base + ev[0], base + ev[1], base + ev[2]))


def test_to_building_rotates_charger_phases():
    assert Controller.to_building(0, (1.0, 2.0, 3.0)) == (1.0, 2.0, 3.0)
    assert Controller.to_building(1, (1.0, 2.0, 3.0)) == (3.0, 1.0, 2.0)
    assert Controller.to_building(2, (1.0, 2.0, 3.0)) == (2.0, 3.0, 1.0)


def test_dynamic_uses_measured_headroom():
    c = Controller(SITE)
    lim = c.step(0, meter(0, 30.0), [car("A"), car("B")])
    # target 60 A - base 30 A = 30 A for two three-phase cars
    assert lim == {"A": 15.0, "B": 15.0}
    assert c.last.avail_a == pytest.approx((30.0, 30.0, 30.0))


def test_base_estimate_subtracts_controlled_charging():
    c = Controller(SITE)
    assert c.step(0, meter(0, 50.0), [car("A")]) == {"A": 10.0}
    # next minute: meter shows 30 A base + 10 A from car A; headroom for A is target - base, not target - meter
    lim = c.step(1, meter(1, 30.0, (10, 10, 10)), [car("A", measured=(10.0, 10.0, 10.0))])
    assert lim == {"A": 16.0}


def test_static_split_ignores_meter():
    c = Controller(SITE, mode="static")
    lim = c.step(0, meter(0, 58.0), [car("A"), car("B"), car("C")])
    # static budget 63 - 45 = 18 A: three cars at 6 A even though the meter says the building is almost full
    assert lim == {"A": 6.0, "B": 6.0, "C": 6.0}


def test_stale_meter_falls_back_to_assumed_base_load():
    c = Controller(SITE)
    c.step(0, meter(0, 20.0), [car("A")])
    lim = c.step(5, meter(0, 20.0), [car("A")])  # reading is 5 min old > stale_after_min
    assert c.last.meter_stale
    assert lim == {"A": 0.0}  # 60 - 55 = 5 A < 6 A: paused
    assert any(what == "stale" for _, _, what in c.events)
    lim = c.step(6, meter(6, 20.0), [car("A")])
    assert not c.last.meter_stale and lim["A"] == 16.0


def test_missing_meter_counts_as_stale():
    c = Controller(SiteConfig(fallback_base_a=40.0))
    lim = c.step(0, None, [car("A"), car("B")])
    assert c.last.meter_stale and lim == {"A": 10.0, "B": 10.0}


def test_noncompliant_charger_is_detected_and_reserved():
    c = Controller(SITE)
    obs = [car("A"), car("B")]
    lim = c.step(0, meter(0, 30.0), obs)
    assert lim == {"A": 15.0, "B": 15.0}
    # B ignores 15 A and draws 16 A + more on next cycle: say 16 A while its limit is 15 -> within tolerance
    obs = [car("A", measured=(15, 15, 15)), car("B", measured=(16.0, 16.0, 16.0))]
    c.step(1, meter(1, 30.0, (31, 31, 31)), obs)
    assert c.last.noncompliant == []
    # now B is told 6 A but still draws 16 A -> flagged, A gets what is left after B's full rating
    c.state["B"].last_limit = 6.0
    obs = [car("A", measured=(15, 15, 15)), car("B", measured=(16.0, 16.0, 16.0))]
    lim = c.step(2, meter(2, 30.0, (31, 31, 31)), obs)
    assert c.last.noncompliant == ["B"]
    assert "B" not in lim
    assert lim["A"] == pytest.approx(14.0)  # 60 - (30 base + 16 B) = 14
    assert any(cp == "B" and what == "ignores profile" for _, cp, what in c.events)


def test_offline_charger_keeps_its_last_limit_reserved():
    c = Controller(SITE)
    c.step(0, meter(0, 30.0), [car("A"), car("B")])  # both 15 A
    obs = [car("A", measured=(15, 15, 15)), car("B", measured=(15, 15, 15))]
    c.step(1, meter(1, 30.0, (30, 30, 30)), obs)
    # B goes offline; its draw is still in the meter but not reported any more
    obs = [car("A", measured=(15, 15, 15)), ChargerObs("B", 0, connected=False, in_session=True, session_id=1)]
    lim = c.step(2, meter(2, 30.0, (30, 30, 30)), obs)
    assert c.last.offline == ["B"]
    assert lim == {"A": 15.0}  # A keeps 15, never more: B may still draw its 15 A


def test_idle_offline_charger_reserves_failsafe_current():
    c = Controller(SITE)
    off = ChargerObs("X", 0, connected=False, in_session=False)
    lim = c.step(0, meter(0, 30.0), [car("A"), off])
    assert lim == {"A": 16.0}
    assert c.last.avail_a == pytest.approx((24.0, 24.0, 24.0))  # 30 A headroom - 6 A failsafe


def test_single_phase_car_detected_from_meter_values():
    c = Controller(SITE)
    assert c.step(0, meter(0, 40.0), [car("A", rot=1), car("B", rot=0)]) == {"A": 10.0, "B": 10.0}
    assert c.state["A"].phases is None
    obs = [car("A", rot=1, measured=(5.0, 0.0, 0.0)), car("B", rot=0, measured=(5.0, 0.0, 0.0))]
    c.step(1, MeterReading(1, (55.0, 55.0, 50.0)), obs)
    assert c.state["A"].phases == 1 and c.state["B"].phases == 1
    # building phase 1 (A) is nearly full, phase 0 (B) is free: B gets its maximum, A only what phase 1 allows
    lim = c.step(2, MeterReading(2, (30.0, 58.0, 30.0)), obs)
    assert lim == {"A": 7.0, "B": 16.0}


def test_unknown_car_counts_on_all_three_phases():
    c = Controller(SITE)
    lim = c.step(0, MeterReading(0, (30.0, 58.0, 30.0)), [car("A", rot=0)])
    assert lim == {"A": 0.0}  # only 2 A free on phase 1 and the car might be three-phase


def test_running_cars_are_not_preempted_between_rotation_boundaries():
    site = SiteConfig(fuse_a=63.0, margin_a=3.0, rotate_every_min=15)
    c = Controller(site, policy="equal")
    # room for one car at >= 6 A only (target 60 - base 52 = 8 A)
    obs = [car("A", delivered_kwh=5.0), car("B", delivered_kwh=0.0)]
    lim = c.step(1, meter(1, 52.0), obs)
    assert lim == {"A": 0.0, "B": 8.0}  # B has less energy, goes first
    obs = [car("A", delivered_kwh=5.0), car("B", delivered_kwh=9.0, measured=(8, 8, 8))]
    lim = c.step(2, meter(2, 52.0, (8, 8, 8)), obs)
    assert lim["B"] == 8.0  # still B: no flapping before the boundary
    lim = c.step(15, meter(15, 52.0, (8, 8, 8)), obs)
    assert lim == {"A": 8.0, "B": 0.0}  # at the boundary the car with less energy takes over
    assert c.pauses() == 1


def test_departure_policy_prefers_the_car_leaving_soon():
    c = Controller(SITE, policy="departure")
    obs = [car("soon", departure_min=60.0, need_kwh=10.0), car("late", departure_min=900.0, need_kwh=10.0)]
    lim = c.step(0, meter(0, 46.0), obs)  # 14 A of room: only two cars at 6 A, extra to the urgent one
    assert lim["soon"] == 8.0 and lim["late"] == 6.0


def test_kw_only_controller_can_overload_a_phase():
    site = SiteConfig(fuse_a=63.0, margin_a=3.0)
    aware, blind = Controller(site), Controller(site, phase_aware=False)
    obs = [car(f"C{i}", rot=0, sid=1, measured=(7.5, 0.0, 0.0)) for i in range(4)]  # four 1-phase cars on L1
    for c in (aware, blind):
        c.step(0, meter(0, 30.0), [car(o.cp_id, rot=0) for o in obs])
        for o in obs:
            c.state[o.cp_id].phases = 1
    m = MeterReading(1, (70.0, 30.0, 30.0))
    la = aware.step(1, m, obs)
    lb = blind.step(1, m, obs)
    assert sum(la.values()) <= 30.0 + 1e-6  # L1 headroom 60 - 30
    assert sum(lb.values()) > 30.0  # kW budget lets L1 go over


def test_invalid_mode_and_policy():
    with pytest.raises(ValueError):
        Controller(SITE, mode="magic")
    with pytest.raises(ValueError):
        Controller(SITE, policy="lottery")
