"""Simulated OCPP 1.6J charger with one connector and a simulated car.

The protocol side is real (BootNotification, StatusNotification, Authorize, StartTransaction, DataTransfer,
MeterValues with Current.Import per phase, StopTransaction; it obeys TxDefaultProfile and TxProfile).
The car and the electrical side are modelled. `ignore_profiles=True` makes a faulty charger that answers
Accepted but keeps drawing the car's maximum.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from ocpp.routing import on
from ocpp.v16 import ChargePoint as _ChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.datatypes import MeterValue, SampledValue
from ocpp.v16.enums import (
    Action,
    ChargePointErrorCode,
    ChargePointStatus,
    ChargingProfileStatus,
    Measurand,
    Phase,
    UnitOfMeasure,
)
from websockets.asyncio.client import connect

from .config import VOLTAGE
from .csms import MSG_DEPARTURE, SUBPROTOCOL, VENDOR_ID


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Car:
    need_kwh: float
    phases: int = 3  # 1 or 3
    max_a: float = 16.0
    delivered_kwh: float = 0.0

    @property
    def full(self) -> bool:
        return self.delivered_kwh >= self.need_kwh - 1e-9


class SimChargePoint(_ChargePoint):
    def __init__(self, cp_id: str, connection, max_a: float = 16.0, min_a: float = 6.0, ignore_profiles: bool = False):
        super().__init__(cp_id, connection)
        self.max_a, self.min_a, self.ignore_profiles = max_a, min_a, ignore_profiles
        self.default_limit: float | None = None  # TxDefaultProfile
        self.tx_limit: float | None = None  # TxProfile of the running transaction
        self.car: Car | None = None
        self.transaction_id: int | None = None
        self.energy_wh = 0.0
        self.draw_a = 0.0  # present current per used phase
        self.profiles_accepted = 0
        self.profiles_rejected = 0

    # ---- central system -> charger
    @on(Action.set_charging_profile)
    async def on_set_charging_profile(self, connector_id, cs_charging_profiles, **kwargs):
        p = cs_charging_profiles
        try:
            purpose = str(p["charging_profile_purpose"])
            sched = p["charging_schedule"]
            unit = str(sched["charging_rate_unit"])
            limit = float(sched["charging_schedule_period"][0]["limit"])
        except (KeyError, IndexError, TypeError, ValueError):
            self.profiles_rejected += 1
            return call_result.SetChargingProfile(status=ChargingProfileStatus.rejected)
        if unit != "A" or limit < 0 or limit > self.max_a:
            self.profiles_rejected += 1
            return call_result.SetChargingProfile(status=ChargingProfileStatus.rejected)
        if purpose == "TxDefaultProfile":
            self.default_limit = limit
        elif purpose == "TxProfile":
            if self.transaction_id is None or p.get("transaction_id") != self.transaction_id:
                self.profiles_rejected += 1
                return call_result.SetChargingProfile(status=ChargingProfileStatus.rejected)
            self.tx_limit = limit
        else:
            self.profiles_rejected += 1
            return call_result.SetChargingProfile(status=ChargingProfileStatus.rejected)
        self.profiles_accepted += 1
        return call_result.SetChargingProfile(status=ChargingProfileStatus.accepted)

    # ---- electrical model (no I/O)
    @property
    def active_limit(self) -> float:
        if self.ignore_profiles:
            return self.max_a
        if self.tx_limit is not None:
            return self.tx_limit
        if self.default_limit is not None:
            return self.default_limit
        return self.max_a

    def current_draw(self) -> float:
        """Current per used phase the car draws now (0 below the minimum: the session is suspended)."""
        if self.car is None or self.transaction_id is None or self.car.full:
            return 0.0
        lim = self.active_limit
        return 0.0 if lim < self.min_a - 1e-9 else min(lim, self.car.max_a)

    def phase_currents(self, a: float | None = None) -> tuple[float, float, float]:
        """Charger L1, L2, L3."""
        a = self.current_draw() if a is None else a
        if self.car is None or a <= 0:
            return (0.0, 0.0, 0.0)
        return (a, a, a) if self.car.phases == 3 else (a, 0.0, 0.0)

    def charge(self, a: float, seconds: float) -> float:
        """Deliver energy at `a` A per phase for `seconds`; returns the mean current actually drawn."""
        if self.car is None or a <= 0 or seconds <= 0:
            return 0.0
        kw = a * VOLTAGE * self.car.phases / 1000.0
        kwh = min(kw * seconds / 3600.0, self.car.need_kwh - self.car.delivered_kwh)
        kwh = max(kwh, 0.0)
        self.car.delivered_kwh += kwh
        self.energy_wh += kwh * 1000.0
        return kwh * 3600.0 / seconds / (VOLTAGE * self.car.phases / 1000.0)

    # ---- charger -> central system
    async def boot(self) -> None:
        await self.call(call.BootNotification(charge_point_model="sim-ac-11kW", charge_point_vendor="olmc-sim"))
        st = ChargePointStatus.charging if self.transaction_id is not None else ChargePointStatus.available
        await self.status(st)

    async def status(self, st: ChargePointStatus) -> None:
        await self.call(call.StatusNotification(connector_id=1, error_code=ChargePointErrorCode.no_error, status=st))

    async def plug_in(self, car: Car, departure_min: float | None = None, id_tag: str = "resident") -> None:
        self.car = car
        self.tx_limit = None
        await self.status(ChargePointStatus.preparing)
        await self.call(call.Authorize(id_tag=id_tag))
        resp = await self.call(call.StartTransaction(connector_id=1, id_tag=id_tag, meter_start=int(self.energy_wh),
                                                     timestamp=_now()))
        self.transaction_id = resp.transaction_id
        if departure_min is not None:  # the resident's departure time and energy request (stands in for an app)
            await self.call(call.DataTransfer(vendor_id=VENDOR_ID, message_id=MSG_DEPARTURE,
                                              data=json.dumps({"departure_min": departure_min, "need_kwh": car.need_kwh})))
        await self.status(ChargePointStatus.charging)

    async def unplug(self) -> None:
        tx, self.transaction_id, self.tx_limit = self.transaction_id, None, None
        if tx is not None:
            await self.call(call.StopTransaction(meter_stop=int(self.energy_wh), timestamp=_now(), transaction_id=tx))
        self.car = None
        await self.status(ChargePointStatus.available)

    async def send_meter_values(self, phase_a: tuple[float, float, float]) -> None:
        if self.transaction_id is None:
            return
        sv = [SampledValue(value=f"{self.energy_wh:.0f}", measurand=Measurand.energy_active_import_register,
                           unit=UnitOfMeasure.wh)]
        for ph, a in zip((Phase.l1, Phase.l2, Phase.l3), phase_a, strict=True):
            sv.append(SampledValue(value=f"{a:.2f}", measurand=Measurand.current_import, phase=ph, unit=UnitOfMeasure.a))
        await self.call(call.MeterValues(connector_id=1, transaction_id=self.transaction_id,
                                         meter_value=[MeterValue(timestamp=_now(), sampled_value=sv)]))


async def open_connection(url: str, cp_id: str):
    return await connect(f"{url.rstrip('/')}/{cp_id}", subprotocols=[SUBPROTOCOL])
