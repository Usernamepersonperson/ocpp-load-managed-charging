import numpy as np
import pytest

from olmc.config import MIN_PER_DAY, SiteConfig
from olmc.data import BuildingData, Session, build
from olmc.report import merge
from olmc.sim import jain, metrics, simulate

SITE = SiteConfig()


@pytest.fixture(scope="module")
def fortnight():
    return build(SITE, n_days=21, seed=7)


def flat_building(base_a: float, sessions: list[Session], days: int = 1) -> BuildingData:
    return BuildingData(np.full((days * MIN_PER_DAY, 3), base_a), np.zeros(days), sessions, days, 0)


def test_build_is_seeded_and_reproducible():
    a, b = build(SITE, n_days=3, seed=11), build(SITE, n_days=3, seed=11)
    assert np.array_equal(a.base_a, b.base_a) and a.sessions == b.sessions
    assert not np.array_equal(a.base_a, build(SITE, n_days=3, seed=12).base_a)


def test_base_load_is_plausible(fortnight):
    b = fortnight.base_a
    assert b.shape == (21 * MIN_PER_DAY, 3) and (b > 0).all()
    kwh_per_day = b.sum() * 230 / 1000 / 60 / 21
    assert 150 < kwh_per_day < 450  # 24 flats + heat pump in winter
    evening = b.reshape(21, MIN_PER_DAY, 3)[:, 18 * 60:20 * 60].mean()
    night = b.reshape(21, MIN_PER_DAY, 3)[:, 2 * 60:3 * 60].mean()
    assert evening > night


def test_sessions_are_consistent(fortnight):
    last_dep: dict[int, int] = {}
    for s in sorted(fortnight.sessions, key=lambda s: (s.charger, s.arrival)):
        assert s.departure > s.arrival and s.request_kwh > 0 and s.phases in (1, 3)
        full_kwh = s.car_max_a * 230 * s.phases / 1000 * (s.departure - s.arrival) / 60
        assert s.request_kwh <= full_kwh + 1e-6  # always reachable without a fuse limit
        assert s.arrival >= last_dep.get(s.charger, -1)  # one car per charger at a time
        last_dep[s.charger] = s.departure
    assert {s.phases for s in fortnight.sessions} == {1, 3}


def test_jain_index_known_values():
    assert jain([1, 1, 1, 1]) == pytest.approx(1.0)
    assert jain([1, 0, 0, 0]) == pytest.approx(0.25)
    assert jain([1, 0.5]) == pytest.approx(0.9)


def test_no_load_management_exceeds_fuse_and_dynamic_does_not():
    # base 40 A, four three-phase cars arrive together: 40 + 64 A without control
    sess = [Session(c, 600, 1300, 20.0, 3, 16.0) for c in range(4)]
    data = flat_building(40.0, sess)
    none = metrics(SITE, simulate(SITE, data, "none"))
    dyn = metrics(SITE, simulate(SITE, data, "equal"))
    assert none["minutes_above_fuse"] > 60 and none["max_phase_a"] == pytest.approx(104.0)
    assert dyn["minutes_above_fuse"] == 0 and dyn["max_phase_a"] <= SITE.fuse_a
    assert dyn["energy_delivered_pct"] == pytest.approx(100.0)


def test_energy_never_exceeds_request_and_finishes_mid_minute():
    sess = [Session(0, 10, 200, 1.0, 3, 16.0)]  # 1 kWh at 11 kW: done in ~5.4 min
    r = simulate(SITE, flat_building(10.0, sess), "none")
    assert r.outcomes[0].delivered_kwh == pytest.approx(1.0)
    ev_kwh = r.ev_a.sum() * 230 / 1000 / 60
    assert ev_kwh == pytest.approx(1.0, rel=1e-6)


def test_static_split_starves_when_budget_is_small():
    site = SiteConfig(static_reserve_a=50.0)  # 63 - 50 = 13 A for EVs: only two of three cars can run
    sess = [Session(c, 0, 240, 40.0, 3, 16.0) for c in range(3)]
    st = metrics(site, simulate(site, flat_building(10.0, sess), "static"))
    dyn = metrics(site, simulate(site, flat_building(10.0, sess), "equal"))
    assert st["energy_delivered_pct"] < dyn["energy_delivered_pct"]
    assert st["minutes_above_fuse"] == 0 and dyn["minutes_above_fuse"] == 0


def test_departure_policy_beats_fifo_for_the_early_leaver():
    # room for one car at a time (60 - 50 = 10 A); car 1 arrives later but leaves much earlier
    sess = [Session(0, 0, 900, 30.0, 3, 16.0), Session(1, 5, 200, 6.0, 3, 16.0)]
    data = flat_building(50.0, sess)
    fifo = simulate(SITE, data, "fifo")
    dep = simulate(SITE, data, "departure")
    early = {o.charger: o.served for o in fifo.outcomes}[1], {o.charger: o.served for o in dep.outcomes}[1]
    assert early[1] > early[0]
    assert early[1] == pytest.approx(1.0)


def test_phase_aware_beats_kw_budget_with_single_phase_cars():
    # three single-phase cars, all on chargers wired with L1 on building phase 0 (chargers 0, 3, 6)
    sess = [Session(c, 0, 600, 30.0, 1, 16.0) for c in (0, 3, 6)]
    data = flat_building(35.0, sess)
    aware = metrics(SITE, simulate(SITE, data, "equal"))
    blind = metrics(SITE, simulate(SITE, data, "equal_kw"))
    assert aware["minutes_above_fuse"] == 0
    assert blind["minutes_above_fuse"] > 0


def test_unknown_strategy_rejected():
    with pytest.raises(ValueError):
        simulate(SITE, flat_building(10.0, []), "chaos")


def test_merge_concatenates_blocks(fortnight):
    a = simulate(SITE, fortnight, "equal", start_day=0, n_days=2)
    b = simulate(SITE, fortnight, "equal", start_day=7, n_days=2)
    m = merge([a, b])
    assert m.total_a.shape[0] == 4 * MIN_PER_DAY
    assert len(m.outcomes) == len(a.outcomes) + len(b.outcomes)
    assert metrics(SITE, m)["minutes"] == 4 * MIN_PER_DAY


def test_fortnight_dynamic_controller_keeps_fuse(fortnight):
    r = metrics(SITE, simulate(SITE, fortnight, "departure", start_day=7, n_days=7))
    none = metrics(SITE, simulate(SITE, fortnight, "none", start_day=7, n_days=7))
    assert none["minutes_above_fuse"] > 0
    assert r["minutes_above_fuse"] == 0
    assert r["energy_delivered_pct"] > 99.0
    assert r["ctrl_us_p50"] is not None and r["ctrl_us_p50"] < 5000
