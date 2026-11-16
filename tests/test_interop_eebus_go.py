"""SPINE and use case interop with eebus-go / spine-go.

Runs only when EEBUS_GO_HARNESS points to the built interop/eebus-go-harness.
"""

from __future__ import annotations

import asyncio
import os
import socket

import pytest

from pyeebus.service import EebusService
from pyeebus.ship import Identity
from pyeebus.simulator import SimulatedEVSE
from pyeebus.usecases import (
    EVCC,
    EVCEM,
    EVSECC,
    LPC,
    MPC,
    OPEV,
    CsLPC,
    DataNotAvailable,
    LoadLimit,
    LPCState,
    PhaseLimit,
)

HARNESS = os.environ.get("EEBUS_GO_HARNESS")
pytestmark = pytest.mark.skipif(not HARNESS, reason="EEBUS_GO_HARNESS not set")
NODE = {"announce": False, "discover": False, "host": "127.0.0.1"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Harness:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.lines: list[str] = []
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        while line := await self.proc.stdout.readline():
            self.lines.append(line.decode().strip())

    async def expect(self, prefix: str, timeout: float = 10.0) -> str:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            for line in self.lines:
                if line.startswith(prefix):
                    return line
            await asyncio.sleep(0.02)
        raise AssertionError(f"no {prefix!r} line; got {self.lines}")

    async def send(self, command: str) -> None:
        self.proc.stdin.write(command.encode() + b"\n")
        await self.proc.stdin.drain()

    async def stop(self) -> None:
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self._task.cancel()


async def start_harness(mode: str, trust: str, port: int) -> Harness:
    proc = await asyncio.create_subprocess_exec(
        HARNESS, "-mode", mode, "-port", str(port), "-trust", trust,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    h = Harness(proc)
    await h.expect("READY")
    return h


async def wait_for(condition, timeout: float = 10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            if result := condition():
                return result
        except DataNotAvailable:
            pass
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def test_eebus_go_cem_controls_pyeebus_evse():
    """eebus-go CEM (EVSECC, EVCC, EVCEM, OPEV) <- pyeebus simulated EVSE."""
    evse = SimulatedEVSE(Identity.create("evse"), port=0, **NODE)
    port = free_port()
    harness = await start_harness("cem", evse.service.ski, port)
    try:
        go_ski = (await harness.expect("SKI")).split()[1]
        evse.service.node.trust.trust(go_ski)
        await evse.start()
        await evse.service.node.connect("127.0.0.1", port, go_ski)
        assert await harness.expect("EVSE_MANUFACTURER") == "EVSE_MANUFACTURER SimEVSE"

        evse.plug_in()
        await harness.expect("OPEV_LIMITS 16,16,16")
        assert await harness.expect("WRITE_RESULT") == "WRITE_RESULT 0"
        await wait_for(lambda: evse.limits["obligation"] == [10, 10, 10])
        assert await harness.expect("EVCC_STANDARD") == "EVCC_STANDARD iec61851"

        evse.set_charging(10, 1234)
        await harness.expect("EVCEM_CURRENT 10,10,10")
        await harness.expect("EVCEM_ENERGY 1234")

        evse.unplug()
        await harness.expect("EVENT cem-evcc-EvDisconnected")
    finally:
        await evse.stop()
        await harness.stop()


async def test_pyeebus_cem_controls_spine_go_evse():
    """pyeebus CEM -> spine-go EVSE with an EV entity."""
    cem = EebusService(Identity.create("cem"), brand="pyeebus", model="TestCEM", serial="1", port=0, **NODE)
    ucs = {cls.__name__: cls(cem.entities[0]).setup() for cls in (EVSECC, EVCC, EVCEM, OPEV)}
    port = free_port()
    harness = await start_harness("evse", cem.ski, port)
    try:
        go_ski = (await harness.expect("SKI")).split()[1]
        cem.trust(go_ski)
        await cem.start()
        await cem.node.connect("127.0.0.1", port, go_ski)
        remote = await wait_for(lambda: next(iter(cem.remote_devices.values()), None))
        evse_entity = await wait_for(lambda: next((e for e in remote.entities if e.type == "EVSE"), None))
        manufacturer = await wait_for(lambda: ucs["EVSECC"].manufacturer_data(evse_entity))
        assert manufacturer["deviceName"] == "GoEVSE"
        await harness.expect("SUBSCRIBED NodeManagement")

        await harness.send("plug")
        ev = await wait_for(lambda: remote.entity((1, 1)))
        limits = await wait_for(lambda: ucs["OPEV"].load_control_limits(ev))
        assert [lim.value for lim in limits] == [16, 16, 16]
        assert await wait_for(lambda: ucs["EVCC"].communication_standard(ev) == "iso15118-2ed1")
        await harness.expect("BOUND LoadControl")

        await ucs["OPEV"].write_load_control_limits(ev, [PhaseLimit(p, 10) for p in "abc"])
        assert await harness.expect("LIMIT_WRITE 3") == "LIMIT_WRITE 3 10 true"
        await wait_for(lambda: [lim.value for lim in ucs["OPEV"].load_control_limits(ev)] == [10, 10, 10])

        await harness.send("measure 8 500")
        assert await wait_for(lambda: ucs["EVCEM"].current_per_phase(ev) == [8, 8, 8])
        assert ucs["EVCEM"].energy_charged(ev) == 500

        await harness.send("unplug")
        await wait_for(lambda: remote.entity((1, 1)) is None)
    finally:
        await cem.stop()
        await harness.stop()


async def test_pyeebus_energy_guard_controls_eebus_go_lpc_device():
    """pyeebus LPC (Energy Guard) and MPC (Monitoring Appliance) -> eebus-go cs/lpc + mu/mpc."""
    cem = EebusService(Identity.create("eg"), brand="pyeebus", model="TestEG", serial="1", port=0, **NODE)
    lpc = LPC(cem.entities[0]).setup()
    mpc = MPC(cem.entities[0]).setup()
    port = free_port()
    harness = await start_harness("lpc", cem.ski, port)
    try:
        go_ski = (await harness.expect("SKI")).split()[1]
        cem.trust(go_ski)
        await cem.start()
        await cem.node.connect("127.0.0.1", port, go_ski)
        remote = await wait_for(lambda: next(iter(cem.remote_devices.values()), None))
        evse = await wait_for(lambda: next((e for e in remote.entities if e.type == "EVSE"), None))

        assert await wait_for(lambda: lpc.is_scenario_available(evse, 1))
        limit = await wait_for(lambda: lpc.consumption_limit(evse))
        assert (limit.value, limit.is_active, limit.is_changeable) == (11000, False, True)
        assert await wait_for(lambda: lpc.failsafe_consumption_limit(evse)) == 4200
        assert lpc.failsafe_duration_minimum(evse) == 7200
        assert await wait_for(lambda: lpc.consumption_nominal_max(evse)) == 11000

        await lpc.write_consumption_limit(evse, LoadLimit(4200, True, duration=3600))
        assert (await harness.expect("LPC_LIMIT")) == "LPC_LIMIT 4200 true 1h0m0s"
        await wait_for(lambda: lpc.consumption_limit(evse).is_active)
        assert lpc.consumption_limit(evse).value == 4200

        await lpc.write_failsafe_consumption_limit(evse, 5000)
        await harness.expect("LPC_CONFIG failsafeConsumptionActivePowerLimit 5000")
        await wait_for(lambda: lpc.failsafe_consumption_limit(evse) == 5000)
        await lpc.write_failsafe_duration_minimum(evse, 3 * 3600)
        await harness.expect("LPC_CONFIG failsafeDurationMinimum 3h0m0s")

        # the device receives our heartbeat
        await harness.expect("LPC_HEARTBEAT", timeout=15)

        # MPC
        assert await wait_for(lambda: mpc.power(evse)) == 3000
        assert mpc.power_per_phase(evse) == [1000, 1000, 1000]
        assert mpc.energy_consumed(evse) == 12345
        assert mpc.voltage_per_phase(evse) == [230, 231, 232]
        assert mpc.frequency(evse) == 50
        assert [round(c, 2) for c in mpc.current_per_phase(evse)] == [4.35, 4.35, 4.35]
        await harness.send("power 6000")
        assert await wait_for(lambda: mpc.power(evse) == 6000)
    finally:
        await cem.stop()
        await harness.stop()


async def test_eebus_go_energy_guard_controls_pyeebus_cs():
    """eebus-go eg/lpc (Energy Guard, like a control box) -> pyeebus CS-LPC."""
    cs = EebusService(Identity.create("cs"), brand="pyeebus", model="TestCS", serial="1", port=0, **NODE)
    events: list[str] = []
    approvals: list[LoadLimit] = []

    def approve_limit(limit: LoadLimit) -> str | None:
        approvals.append(limit)
        return "below the minimum" if limit.is_active and limit.value < 1000 else None

    # short heartbeat timeout, so the test does not wait 2 minutes for failsafe
    lpc = CsLPC(cs.entities[0], lambda _ski, _entity, name: events.append(name),
                approve_limit=approve_limit, heartbeat_timeout=8).setup()
    lpc.set_consumption_nominal_max(11000)
    lpc.set_consumption_limit(LoadLimit(11000, is_active=False))
    lpc.set_failsafe_consumption_limit(4200)
    lpc.set_failsafe_duration_minimum(2 * 3600)
    port = free_port()
    harness = await start_harness("eg", cs.ski, port)
    try:
        go_ski = (await harness.expect("SKI")).split()[1]
        cs.trust(go_ski)
        await cs.start()
        await cs.node.connect("127.0.0.1", port, go_ski)

        # the guard sees the start values and our heartbeat; we see the guard's heartbeat
        assert await harness.expect("EG_ENTITY") == "EG_ENTITY CEM"
        assert await harness.expect("EG_LIMIT") == "EG_LIMIT 11000 false true"
        assert await harness.expect("EG_FAILSAFE ") == "EG_FAILSAFE 4200"
        assert await harness.expect("EG_FAILSAFE_DURATION") == "EG_FAILSAFE_DURATION 2h0m0s"
        assert await harness.expect("EG_NOMINAL_MAX") == "EG_NOMINAL_MAX 11000"
        await harness.expect("EG_HEARTBEAT", timeout=15)
        await wait_for(lpc.is_heartbeat_within_duration)
        assert (lpc.state, lpc.effective_limit()) == (LPCState.INIT, 4200)

        # limit with duration
        await harness.send("limit 4200 3600")
        assert await harness.expect("WRITE_RESULT") == "WRITE_RESULT 0"
        assert approvals[-1] == LoadLimit(4200, True, duration=3600)
        assert await wait_for(lambda: lpc.state == LPCState.LIMITED)
        assert lpc.effective_limit() == 4200
        await harness.expect("EG_LIMIT 4200 true")

        # denied by the application
        harness.lines.clear()
        await harness.send("limit 500")
        assert await harness.expect("WRITE_RESULT") == "WRITE_RESULT 7"
        assert lpc.consumption_limit().value == 4200

        harness.lines.clear()
        await harness.send("release")
        assert await harness.expect("WRITE_RESULT") == "WRITE_RESULT 0"
        assert await wait_for(lambda: lpc.state == LPCState.UNLIMITED_CONTROLLED)
        assert lpc.effective_limit() is None
        await harness.expect("EG_LIMIT 500 false")

        # failsafe values
        await harness.send("failsafe 5000")
        assert await harness.expect("EG_FAILSAFE 5000")
        assert lpc.failsafe_consumption_limit() == (5000, True)
        await harness.send("failsafeduration 10800")
        assert await harness.expect("EG_FAILSAFE_DURATION 3h0m0s")
        assert lpc.failsafe_duration_minimum() == (10800, True)
        assert CsLPC.DATA_UPDATE_FAILSAFE_POWER in events
        assert CsLPC.DATA_UPDATE_FAILSAFE_DURATION in events

        # the guard's heartbeat stops: failsafe; back with heartbeat and a new limit
        await harness.send("heartbeat stop")
        assert await wait_for(lambda: lpc.state == LPCState.FAILSAFE, timeout=20)
        assert lpc.effective_limit() == 5000
        assert CsLPC.HEARTBEAT_TIMEOUT in events
        await harness.send("heartbeat start")
        await wait_for(lpc.is_heartbeat_within_duration, timeout=15)
        assert lpc.state == LPCState.FAILSAFE
        harness.lines.clear()
        await harness.send("limit 3000")
        assert await harness.expect("WRITE_RESULT") == "WRITE_RESULT 0"
        assert await wait_for(lambda: lpc.state == LPCState.LIMITED)
        assert lpc.effective_limit() == 3000
    finally:
        lpc.close()
        await cs.stop()
        await harness.stop()
