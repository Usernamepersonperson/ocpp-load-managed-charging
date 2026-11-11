"""OCPP 1.6J over real websockets on localhost (central system <-> simulated chargers)."""
import asyncio
import json

import numpy as np
import pytest
from ocpp.v16 import call
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidHandshake

from olmc.chargepoint import Car, SimChargePoint, open_connection
from olmc.config import MIN_PER_DAY, SiteConfig
from olmc.csms import MSG_DEPARTURE, VENDOR_ID, CentralSystem, build_profile
from olmc.data import BuildingData, Session
from olmc.live import Faults, run_live

SITE3 = SiteConfig(n_chargers=3)


def data3(base_a: float, sessions: list[Session]) -> BuildingData:
    return BuildingData(np.full((MIN_PER_DAY, 3), base_a), np.zeros(1), sessions, 1, 0)


def test_build_profile_fields():
    p = build_profile(7, 12.34, "tx", transaction_id=3)
    assert p.charging_profile_purpose == "TxProfile" and p.transaction_id == 3 and p.stack_level == 1
    assert p.charging_schedule.charging_rate_unit == "A"
    assert p.charging_schedule.charging_schedule_period[0].limit == 12.3
    d = build_profile(8, 6, "default")
    assert d.charging_profile_purpose == "TxDefaultProfile" and d.transaction_id is None and d.stack_level == 0
    with pytest.raises(ValueError):
        build_profile(1, 10, "tx")
    with pytest.raises(ValueError):
        build_profile(1, -1, "default")
    with pytest.raises(ValueError):
        build_profile(1, 10, "max")


async def _pair():
    cs = CentralSystem("127.0.0.1", 0, failsafe_a=6.0)
    cs.register("CP01", 0)
    await cs.start()
    conn = await open_connection(f"ws://127.0.0.1:{cs.port}", "CP01")
    cp = SimChargePoint("CP01", conn)
    task = asyncio.create_task(cp.start())
    await cp.boot()
    await cs.wait_for("CP01")
    return cs, cp, conn, task


async def _close(cs, conn, task):
    await conn.close()
    task.cancel()
    await cs.stop()


def test_boot_sets_failsafe_default_profile_and_session_flow():
    async def go():
        cs, cp, conn, task = await _pair()
        for _ in range(100):
            if cp.default_limit is not None:
                break
            await asyncio.sleep(0.01)
        assert cp.default_limit == 6.0  # TxDefaultProfile after boot
        await cp.plug_in(Car(10.0, phases=1), departure_min=600.0)
        rec = cs.records["CP01"]
        assert rec.transaction_id == cp.transaction_id and rec.departure_min == 600.0 and rec.need_kwh == 10.0
        assert cp.current_draw() == 6.0  # default profile until the controller speaks
        assert (await cs.apply({"CP01": 13.7}))["CP01"]
        assert cp.tx_limit == pytest.approx(13.7) and cp.current_draw() == pytest.approx(13.7)
        await cp.send_meter_values(cp.phase_currents())
        obs = rec.obs()
        assert obs.in_session and obs.measured_a == pytest.approx((13.7, 0.0, 0.0))
        await cp.unplug()
        assert rec.transaction_id is None and not rec.obs().in_session
        await _close(cs, conn, task)

    asyncio.run(go())


def test_charger_rejects_bad_profiles():
    async def go():
        cs, cp, conn, task = await _pair()
        await cp.plug_in(Car(5.0))
        pt = cs.points["CP01"]
        too_high = build_profile(90, 40.0, "tx", transaction_id=cp.transaction_id)
        r = await pt.call(call.SetChargingProfile(connector_id=1, cs_charging_profiles=too_high))
        assert r.status == "Rejected"
        wrong_tx = build_profile(91, 10.0, "tx", transaction_id=999)
        r = await pt.call(call.SetChargingProfile(connector_id=1, cs_charging_profiles=wrong_tx))
        assert r.status == "Rejected"
        assert cp.profiles_rejected == 2 and cp.tx_limit is None
        await _close(cs, conn, task)

    asyncio.run(go())


def test_data_transfer_validation():
    async def go():
        cs, cp, conn, task = await _pair()
        r = await cp.call(call.DataTransfer(vendor_id="someone-else", message_id=MSG_DEPARTURE, data="{}"))
        assert r.status == "UnknownVendorId"
        r = await cp.call(call.DataTransfer(vendor_id=VENDOR_ID, message_id=MSG_DEPARTURE, data="not json"))
        assert r.status == "Rejected"
        r = await cp.call(call.DataTransfer(vendor_id=VENDOR_ID, message_id=MSG_DEPARTURE,
                                            data=json.dumps({"departure_min": 5, "need_kwh": -1})))
        assert r.status == "Rejected"
        await _close(cs, conn, task)

    asyncio.run(go())


def test_connection_without_ocpp_subprotocol_is_closed():
    async def go():
        cs = CentralSystem("127.0.0.1", 0)
        await cs.start()
        try:  # rejected at the handshake (HTTP 400) or closed right after with 1002
            async with connect(f"ws://127.0.0.1:{cs.port}/CP09") as ws:
                await asyncio.wait_for(ws.wait_closed(), 5)
                assert ws.close_code == 1002
        except InvalidHandshake:
            pass
        assert "CP09" not in cs.points
        await cs.stop()

    asyncio.run(go())


def _three_cars():
    # three cars plug in at 18:00, base 40 A: 20 A headroom for 48 A of demand
    return [Session(0, 18 * 60, 23 * 60, 12.0, 3, 16.0), Session(1, 18 * 60 + 2, 23 * 60, 8.0, 1, 16.0),
            Session(2, 18 * 60 + 4, 22 * 60, 10.0, 3, 16.0)]


def run3(faults=None, base=40.0, minutes=330):
    return asyncio.run(run_live(SITE3, data3(base, _three_cars()), 17 * 60 + 50, minutes, "departure", faults))


def test_closed_loop_keeps_fuse_and_delivers():
    r = run3()
    s = r.summary()
    assert s["sessions"] == 3 and s["minutes_above_fuse"] == 0
    assert s["max_phase_a"] <= SITE3.fuse_a
    assert s["profiles_sent"] > 0 and s["profiles_accepted"] == s["profiles_sent"]
    assert s["energy_delivered_pct"] == pytest.approx(100.0)
    assert s["cycle_ms_p50"] < 1000


def test_disconnected_charger_is_reserved_and_recovers():
    r = run3(Faults(disconnect_cp="CP01", disconnect_at=20, reconnect_at=60))
    s = r.summary()
    assert s["minutes_above_fuse"] == 0
    assert any("CP01: offline" in e for e in s["events"]) and any("CP01: back online" in e for e in s["events"])
    assert s["energy_delivered_pct"] == pytest.approx(100.0)


def test_stale_meter_falls_back_and_stays_safe():
    r = run3(Faults(stale_from=20, stale_to=50))
    s = r.summary()
    assert s["stale_cycles"] >= 25 and s["minutes_above_fuse"] == 0
    assert any("meter: stale" in e for e in s["events"])


def test_charger_ignoring_profile_is_detected():
    r = run3(Faults(ignore_cp="CP03"))
    s = r.summary()
    assert any("CP03: ignores profile" in e for e in s["events"])
    assert s["minutes_above_fuse"] == 0
