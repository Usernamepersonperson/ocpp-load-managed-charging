"""OCPP 1.6J central system for load management (websocket server, subprotocol ocpp1.6).

It keeps what the controller needs per charger: connection, transaction, per-phase current from MeterValues
(Current.Import L1-L3), energy, the driver's departure time and energy request (DataTransfer), and it sends
SetChargingProfile: a TxDefaultProfile with the failsafe current after every boot, and a TxProfile per session.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import websockets
from ocpp.routing import after, on
from ocpp.v16 import ChargePoint as _ChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.datatypes import ChargingProfile, ChargingSchedule, ChargingSchedulePeriod, IdTagInfo
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    ChargingProfileKindType,
    ChargingProfilePurposeType,
    ChargingProfileStatus,
    ChargingRateUnitType,
    DataTransferStatus,
    RegistrationStatus,
)
from websockets.asyncio.server import serve

from .controller import ChargerObs

log = logging.getLogger("olmc.csms")
SUBPROTOCOL = "ocpp1.6"
VENDOR_ID = "ocpp-load-managed-charging"
MSG_DEPARTURE = "DepartureInfo"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_profile(profile_id: int, amps: float, purpose: str = "tx", transaction_id: int | None = None) -> ChargingProfile:
    """Absolute profile, one open-ended period, limit in A per phase. purpose: 'tx' (TxProfile) or 'default'."""
    if amps < 0:
        raise ValueError("amps must be >= 0")
    if purpose == "tx":
        if transaction_id is None:
            raise ValueError("a TxProfile needs the transaction id")
        p = ChargingProfilePurposeType.tx_profile
        stack = 1
    elif purpose == "default":
        p, stack, transaction_id = ChargingProfilePurposeType.tx_default_profile, 0, None
    else:
        raise ValueError("purpose must be 'tx' or 'default'")
    sched = ChargingSchedule(
        charging_rate_unit=ChargingRateUnitType.amps,
        charging_schedule_period=[ChargingSchedulePeriod(start_period=0, limit=round(float(amps), 1))],
    )
    return ChargingProfile(
        charging_profile_id=profile_id,
        stack_level=stack,
        charging_profile_purpose=p,
        charging_profile_kind=ChargingProfileKindType.absolute,
        charging_schedule=sched,
        transaction_id=transaction_id,
    )


@dataclass
class CpRecord:
    """Everything the central system knows about one charger (kept across reconnects)."""

    cp_id: str
    rotation: int = 0
    max_a: float = 16.0
    connected: bool = False
    status: str = "Unknown"
    transaction_id: int | None = None
    session_id: int = 0
    arrival_min: float = 0.0
    departure_min: float | None = None
    need_kwh: float | None = None
    meter_start_wh: float = 0.0
    energy_wh: float = 0.0
    current_a: dict[str, float] = field(default_factory=dict)
    boots: int = 0
    profiles_sent: int = 0
    profiles_accepted: int = 0
    meter_values: int = 0
    last_limit: float | None = None

    def obs(self) -> ChargerObs:
        in_session = self.transaction_id is not None and self.status not in ("SuspendedEV", "Finishing", "Available")
        return ChargerObs(
            cp_id=self.cp_id,
            rotation=self.rotation,
            connected=self.connected,
            in_session=in_session,
            session_id=self.session_id,
            arrival_min=self.arrival_min,
            departure_min=self.departure_min,
            need_kwh=self.need_kwh,
            delivered_kwh=max(self.energy_wh - self.meter_start_wh, 0.0) / 1000.0,
            measured_a=(self.current_a.get("L1", 0.0), self.current_a.get("L2", 0.0), self.current_a.get("L3", 0.0)),
            max_a=self.max_a,
        )


class CentralChargePoint(_ChargePoint):
    def __init__(self, cp_id: str, connection, rec: CpRecord, system: CentralSystem):
        super().__init__(cp_id, connection)
        self.rec, self.system = rec, system

    @on(Action.boot_notification)
    async def on_boot(self, charge_point_vendor, charge_point_model, **kwargs):
        self.rec.boots += 1
        return call_result.BootNotification(current_time=now_iso(), interval=60, status=RegistrationStatus.accepted)

    @after(Action.boot_notification)
    async def after_boot(self, **kwargs):
        await self.send_default_profile()

    @on(Action.heartbeat)
    async def on_heartbeat(self, **kwargs):
        return call_result.Heartbeat(current_time=now_iso())

    @on(Action.authorize)
    async def on_authorize(self, id_tag, **kwargs):
        return call_result.Authorize(id_tag_info=IdTagInfo(status=AuthorizationStatus.accepted))

    @on(Action.status_notification)
    async def on_status(self, connector_id, error_code, status, **kwargs):
        if connector_id >= 1:
            self.rec.status = str(getattr(status, "value", status))
        return call_result.StatusNotification()

    @on(Action.start_transaction)
    async def on_start(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        self.system.tx_counter += 1
        r = self.rec
        r.transaction_id = self.system.tx_counter
        r.session_id += 1
        r.arrival_min = self.system.clock()
        r.departure_min = r.need_kwh = None
        r.meter_start_wh = r.energy_wh = float(meter_start)
        r.current_a = {}
        r.last_limit = None
        return call_result.StartTransaction(transaction_id=r.transaction_id,
                                            id_tag_info=IdTagInfo(status=AuthorizationStatus.accepted))

    @on(Action.stop_transaction)
    async def on_stop(self, meter_stop, timestamp, transaction_id, **kwargs):
        r = self.rec
        r.energy_wh = float(meter_stop)
        r.transaction_id = None
        r.current_a = {}
        r.last_limit = None
        return call_result.StopTransaction()

    @on(Action.data_transfer)
    async def on_data_transfer(self, vendor_id, message_id=None, data=None, **kwargs):
        if vendor_id != VENDOR_ID or message_id != MSG_DEPARTURE:
            return call_result.DataTransfer(status=DataTransferStatus.unknown_vendor_id)
        try:
            d = json.loads(data or "{}")
            dep, need = float(d["departure_min"]), float(d["need_kwh"])
        except (ValueError, KeyError, TypeError):
            return call_result.DataTransfer(status=DataTransferStatus.rejected)
        if need < 0:
            return call_result.DataTransfer(status=DataTransferStatus.rejected)
        self.rec.departure_min, self.rec.need_kwh = dep, need
        return call_result.DataTransfer(status=DataTransferStatus.accepted)

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        cur: dict[str, float] = {}
        for mv in meter_value:
            for sv in mv["sampled_value"]:
                meas = sv.get("measurand", "Energy.Active.Import.Register")
                try:
                    val = float(sv["value"])
                except (KeyError, ValueError):
                    continue
                if meas == "Current.Import":
                    cur[sv.get("phase", "L1")] = val
                elif meas == "Energy.Active.Import.Register":
                    self.rec.energy_wh = val * (1000.0 if sv.get("unit") == "kWh" else 1.0)
        if cur:
            self.rec.current_a = cur
        self.rec.meter_values += 1
        return call_result.MeterValues()

    async def send_default_profile(self) -> bool:
        self.system.profile_counter += 1
        prof = build_profile(self.system.profile_counter, self.system.failsafe_a, purpose="default")
        resp = await self.call(call.SetChargingProfile(connector_id=0, cs_charging_profiles=prof))
        return resp.status == ChargingProfileStatus.accepted

    async def set_limit(self, amps: float) -> bool:
        tx = self.rec.transaction_id
        if tx is None:
            return False
        self.system.profile_counter += 1
        prof = build_profile(self.system.profile_counter, amps, purpose="tx", transaction_id=tx)
        self.rec.profiles_sent += 1
        resp = await self.call(call.SetChargingProfile(connector_id=1, cs_charging_profiles=prof))
        ok = resp.status == ChargingProfileStatus.accepted
        if ok:
            self.rec.profiles_accepted += 1
            self.rec.last_limit = float(amps)
        return ok


class CentralSystem:
    def __init__(self, host: str = "127.0.0.1", port: int = 9000, clock: Callable[[], float] | None = None,
                 failsafe_a: float = 6.0):
        self.host, self.port = host, port
        self.clock = clock or (lambda: 0.0)
        self.failsafe_a = failsafe_a
        self.records: dict[str, CpRecord] = {}
        self.points: dict[str, CentralChargePoint] = {}
        self.tx_counter = 0
        self.profile_counter = 0
        self._server = None

    def register(self, cp_id: str, rotation: int, max_a: float = 16.0) -> CpRecord:
        rec = self.records.setdefault(cp_id, CpRecord(cp_id))
        rec.rotation, rec.max_a = rotation, max_a
        return rec

    async def _handler(self, conn):
        cp_id = conn.request.path.strip("/").split("/")[-1] or "unknown"
        if conn.subprotocol != SUBPROTOCOL:
            await conn.close(code=1002, reason="ocpp1.6 subprotocol required")
            return
        rec = self.records.setdefault(cp_id, CpRecord(cp_id))
        cp = CentralChargePoint(cp_id, conn, rec, self)
        self.points[cp_id] = cp
        rec.connected = True
        try:
            await cp.start()
        except websockets.exceptions.ConnectionClosed:
            log.info("charger %s disconnected", cp_id)
        finally:
            if self.points.get(cp_id) is cp:
                self.points.pop(cp_id, None)
                rec.connected = False

    async def start(self) -> None:
        self._server = await serve(self._handler, self.host, self.port, subprotocols=[SUBPROTOCOL])
        if self.port == 0:
            self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def observations(self) -> list[ChargerObs]:
        return [r.obs() for r in self.records.values()]

    async def apply(self, limits: dict[str, float], timeout: float = 5.0) -> dict[str, bool]:
        """Send a TxProfile to every charger in `limits` concurrently; returns accepted per charger."""
        async def one(cp_id: str, amps: float) -> tuple[str, bool]:
            cp = self.points.get(cp_id)
            if cp is None:
                return cp_id, False
            try:
                return cp_id, await asyncio.wait_for(cp.set_limit(amps), timeout)
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                return cp_id, False

        res = await asyncio.gather(*(one(k, v) for k, v in limits.items()))
        return dict(res)

    async def wait_for(self, cp_id: str, timeout: float = 10.0) -> CentralChargePoint:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if cp_id in self.points:
                return self.points[cp_id]
            await asyncio.sleep(0.01)
        raise TimeoutError(f"charger {cp_id} did not connect")
