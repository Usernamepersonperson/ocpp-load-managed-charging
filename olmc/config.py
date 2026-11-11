"""Site and controller configuration. All electrical values are per phase, 230 V phase to neutral."""
from __future__ import annotations

from dataclasses import dataclass

VOLTAGE = 230.0
MIN_PER_DAY = 1440


@dataclass(frozen=True)
class SiteConfig:
    """One apartment building with a shared main fuse and a group of AC chargers.

    fuse_a            main-fuse rating per phase (e.g. 63 or 80 for 3x63 A / 3x80 A)
    margin_a          safety margin the dynamic controller keeps below the fuse
    charger_max_a     charger rating per phase (16 A = 11 kW three-phase)
    min_a             lowest current a car accepts (IEC 61851: 6 A); below it the session is paused (limit 0 A)
    static_reserve_a  base load a planner reserves for the flats when there is no meter (static split)
    fallback_base_a   base load the dynamic controller assumes when the meter reading is stale
    failsafe_a        current a charger applies on its own while it is offline (sent as TxDefaultProfile)
    stale_after_min   meter reading older than this is stale
    rotate_every_min  paused cars may take over from running ones only on these boundaries (no flapping)
    noncompliant_tol_a  a charger drawing more than its limit + this is treated as ignoring the profile
    """

    fuse_a: float = 63.0
    margin_a: float = 3.0
    n_flats: int = 24
    n_chargers: int = 12
    charger_max_a: float = 16.0
    min_a: float = 6.0
    static_reserve_a: float = 45.0
    fallback_base_a: float = 55.0
    failsafe_a: float = 6.0
    stale_after_min: float = 2.0
    rotate_every_min: int = 15
    noncompliant_tol_a: float = 1.5

    @property
    def target_a(self) -> float:
        return self.fuse_a - self.margin_a

    def rotation(self, charger: int) -> int:
        """Building phase wired to the charger's L1. Installers rotate phases so single-phase cars spread out."""
        return charger % 3
