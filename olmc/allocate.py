"""Pure current allocation: share the available current per phase among charging sessions.

No I/O, no clock. Used by the year simulation and by the live OCPP controller.

Each session draws `limit * weight[p]` amps on resource p. Phase-aware: three resources (building phases L1-L3),
weight 1 on every phase the car uses. kW-only ablation: one resource (total), weight = number of phases.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

EPS = 1e-9
POLICIES = ("equal", "fifo", "departure")


@dataclass
class Demand:
    key: str
    weights: tuple[float, ...]  # per resource, same length as `avail`
    max_a: float
    min_a: float = 6.0
    order: float = 0.0  # lower = served first (admission order when not everyone fits)
    need_a: float = 0.0  # departure policy: current that finishes the request exactly at departure


def _headroom(d: Demand, used: list[float], avail: Sequence[float]) -> float:
    """Largest extra current d can take before a resource it uses is full."""
    h = math.inf
    for p, w in enumerate(d.weights):
        if w > 0:
            h = min(h, (avail[p] - used[p]) / w)
    return max(h, 0.0)


def _water_fill(ds: list[Demand], alloc: dict[str, float], used: list[float], avail: Sequence[float]) -> None:
    """Max-min fair: raise every unfrozen session by the same amount until it hits its max or a resource fills up."""
    m = len(avail)
    active = [d for d in ds if alloc[d.key] < d.max_a - EPS]
    while active:
        active = [d for d in active if _headroom(d, used, avail) > EPS and alloc[d.key] < d.max_a - EPS]
        if not active:
            break
        delta = min(d.max_a - alloc[d.key] for d in active)
        for p in range(m):
            wsum = sum(d.weights[p] for d in active)
            if wsum > 0:
                delta = min(delta, (avail[p] - used[p]) / wsum)
        if delta <= EPS:
            break
        for d in active:
            alloc[d.key] += delta
            for p, w in enumerate(d.weights):
                used[p] += w * delta


def allocate(avail: Sequence[float], demands: Sequence[Demand], policy: str = "equal", step_a: float = 0.1) -> dict[str, float]:
    """Return a limit (A per phase) for every demand: 0 (paused) or between min_a and max_a.

    1. admission in `order`: a session gets its minimum current if every resource it uses still has room for it;
       the others are paused (OCPP limit 0 A), never given less than min_a.
    2. the rest is shared: equal = max-min fair water filling; fifo = in order, each up to its max;
       departure = in order (least slack first) up to the current it needs, then water filling.
    Limits are rounded down to `step_a`, so the sum per resource never exceeds `avail`.
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}, expected one of {POLICIES}")
    m = len(avail)
    for d in demands:
        if len(d.weights) != m:
            raise ValueError(f"demand {d.key}: {len(d.weights)} weights for {m} resources")
    used = [0.0] * m
    alloc = {d.key: 0.0 for d in demands}
    admitted: list[Demand] = []
    for d in sorted(demands, key=lambda x: x.order):
        if d.max_a < d.min_a - EPS:
            continue
        if all(w <= 0 or used[p] + w * d.min_a <= avail[p] + EPS for p, w in enumerate(d.weights)):
            admitted.append(d)
            alloc[d.key] = d.min_a
            for p, w in enumerate(d.weights):
                used[p] += w * d.min_a
    if policy == "fifo":
        for d in admitted:
            extra = min(d.max_a - alloc[d.key], _headroom(d, used, avail))
            alloc[d.key] += extra
            for p, w in enumerate(d.weights):
                used[p] += w * extra
    else:
        if policy == "departure":
            for d in admitted:
                target = min(max(d.need_a, d.min_a), d.max_a)
                extra = max(0.0, min(target - alloc[d.key], _headroom(d, used, avail)))
                alloc[d.key] += extra
                for p, w in enumerate(d.weights):
                    used[p] += w * extra
        _water_fill(admitted, alloc, used, avail)
    return {k: (round(math.floor(v / step_a + 1e-6) * step_a, 3) if v > 0 else 0.0) for k, v in alloc.items()}
