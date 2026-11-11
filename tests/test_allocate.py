import random

import pytest

from olmc.allocate import Demand, allocate

ALL = (1.0, 1.0, 1.0)


def one(ph: int) -> tuple[float, float, float]:
    w = [0.0, 0.0, 0.0]
    w[ph] = 1.0
    return tuple(w)


def test_equal_split_three_phase_cars():
    lim = allocate([30, 30, 30], [Demand(k, ALL, 16) for k in "abc"])
    assert lim == {"a": 10.0, "b": 10.0, "c": 10.0}


def test_everyone_gets_max_when_there_is_room():
    lim = allocate([100, 100, 100], [Demand(k, ALL, 16) for k in "ab"])
    assert lim == {"a": 16.0, "b": 16.0}


def test_below_minimum_pauses_instead_of_giving_less_than_6a():
    # 15 A per phase, three cars: 5 A each is not allowed -> two cars at 7.5 A, one paused
    lim = allocate([15, 15, 15], [Demand(k, ALL, 16, order=i) for i, k in enumerate("abc")])
    assert sorted(lim.values()) == [0.0, 7.5, 7.5]
    assert lim["c"] == 0.0  # lowest priority is paused


def test_no_room_pauses_everyone():
    assert set(allocate([5, 30, 30], [Demand("a", ALL, 16)]).values()) == {0.0}
    assert set(allocate([-4, -4, -4], [Demand("a", ALL, 16), Demand("b", one(1), 16)]).values()) == {0.0}


def test_single_phase_cars_use_free_phases():
    # phase L1 is nearly full; single-phase cars on L2 and L3 still charge at full current
    lim = allocate([7, 40, 40], [Demand("l1", one(0), 16), Demand("l2", one(1), 16), Demand("l3", one(2), 16)])
    assert lim == {"l1": 7.0, "l2": 16.0, "l3": 16.0}


def test_water_filling_is_max_min_fair_across_coupled_phases():
    # three-phase car shares L1 with a single-phase car; L2/L3 have room but the 3-phase car is bound by L1
    lim = allocate([20, 50, 50], [Demand("tri", ALL, 16), Demand("mono", one(0), 16)])
    assert lim == {"tri": 10.0, "mono": 10.0}


def test_fifo_fills_first_arrival_first():
    lim = allocate([30, 30, 30], [Demand("late", ALL, 16, order=2), Demand("early", ALL, 16, order=1)], policy="fifo")
    assert lim == {"early": 16.0, "late": 14.0}


def test_departure_policy_serves_need_first():
    # urgent car needs 14 A to finish by departure, relaxed car 6 A; 4 A left are shared equally -> 16 / 8
    ds = [Demand("urgent", ALL, 16, order=-30, need_a=14), Demand("relaxed", ALL, 16, order=400, need_a=6)]
    assert allocate([24, 24, 24], ds, policy="departure") == {"urgent": 16.0, "relaxed": 8.0}
    assert allocate([20, 20, 20], ds, policy="departure") == {"urgent": 14.0, "relaxed": 6.0}
    assert allocate([24, 24, 24], ds, policy="equal") == {"urgent": 12.0, "relaxed": 12.0}


def test_limits_rounded_down_to_tenth_of_amp():
    lim = allocate([20, 20, 20], [Demand(k, ALL, 16) for k in "abc"])
    assert all(v == 6.6 for v in lim.values())


def test_kw_only_budget_can_overload_one_phase():
    # one resource = total; four single-phase cars all wired to L1 fit in the kW budget but not on L1
    ds = [Demand(k, (1.0,), 16) for k in "abcd"]
    lim = allocate([60.0], ds)  # 20 A on each of 3 phases = 60 A-phase budget
    assert sum(lim.values()) == pytest.approx(60.0)
    phase_aware = allocate([20, 20, 20], [Demand(k, one(0), 16) for k in "abcd"])
    assert sum(phase_aware.values()) <= 20.0 + 1e-9


def test_unknown_policy_and_bad_weights_rejected():
    with pytest.raises(ValueError):
        allocate([10, 10, 10], [Demand("a", ALL, 16)], policy="random")
    with pytest.raises(ValueError):
        allocate([10, 10, 10], [Demand("a", (1.0, 1.0), 16)])


def test_property_never_exceeds_available_per_phase():
    rng = random.Random(3)
    for _ in range(400):
        avail = [rng.uniform(-5, 80) for _ in range(3)]
        ds = []
        for i in range(rng.randint(0, 12)):
            w = ALL if rng.random() < 0.6 else one(rng.randrange(3))
            ds.append(Demand(f"c{i}", w, rng.choice([10.0, 16.0, 32.0]), 6.0, rng.random(), rng.uniform(0, 16)))
        for pol in ("equal", "fifo", "departure"):
            lim = allocate(avail, ds, pol)
            for p in range(3):
                used = sum(lim[d.key] * d.weights[p] for d in ds)
                assert used <= max(avail[p], 0.0) + 1e-6
            for d in ds:
                assert lim[d.key] == 0.0 or 6.0 - 1e-9 <= lim[d.key] <= d.max_a + 1e-9
