"""CS-LPC: the LPC state machine and a controllable system controlled by the pyeebus Energy Guard."""

from __future__ import annotations

import asyncio

import pytest

from pyeebus.service import EebusService
from pyeebus.ship import Identity
from pyeebus.spine.model import SpineError, scaled_value
from pyeebus.usecases import LPC, CsLPC, DataNotAvailable, LoadLimit, LPCState, LPCStateMachine
from pyeebus.usecases.grid import KEY_FAILSAFE_DURATION

FAILSAFE = 2 * 3600

# --- state machine ----------------------------------------------------------------------------


def test_init_stays_without_heartbeat():
    # LPC-906: no transition from init to failsafe; init keeps the failsafe limit
    m = LPCStateMachine(now=0)
    assert not m.update(120, FAILSAFE)
    assert not m.update(120 + FAILSAFE, FAILSAFE)
    assert (m.state, m.failsafe_until) == (LPCState.INIT, None)


def test_init_may_go_autonomous():
    m = LPCStateMachine(now=0, autonomous_after_init=True)
    assert not m.update(119)
    assert m.update(120)
    assert m.state == LPCState.UNLIMITED_AUTONOMOUS


def test_init_needs_heartbeat_then_limit():
    m = LPCStateMachine(now=0)
    assert not m.heartbeat(5, FAILSAFE)
    assert m.state == LPCState.INIT
    assert m.limit(6, True, 4200, None, FAILSAFE)
    assert m.state == LPCState.LIMITED
    m = LPCStateMachine(now=0)
    m.heartbeat(5)
    assert m.limit(6, False, 4200, None)
    assert m.state == LPCState.UNLIMITED_CONTROLLED


def test_write_is_evaluated_only_after_a_heartbeat():
    m = LPCStateMachine(now=0)
    assert not m.limit(5, True, 4200, None)  # no heartbeat yet
    assert not m.heartbeat(6)  # a heartbeat after the write does not count
    assert m.state == LPCState.INIT
    assert not m.limit(67, True, 4200, None)  # more than 60 s after the heartbeat
    assert m.state == LPCState.INIT
    m.heartbeat(100)
    assert m.limit(160, True, 4200, None)  # within 60 s
    assert m.state == LPCState.LIMITED


def test_denied_write_leaves_init_unlimited():
    # LPC-902: heartbeat and write leave init, also if the limit is denied
    m = LPCStateMachine(now=0)
    m.heartbeat(5)
    assert m.limit(6, True, 500, None, accepted=False)
    assert (m.state, m.limit_active) == (LPCState.UNLIMITED_CONTROLLED, False)
    # in unlimited/controlled and limited a denied write changes nothing
    m.limit(7, True, 4200, None)
    assert not m.limit(8, True, 500, None, accepted=False)
    assert (m.state, m.limit_value) == (LPCState.LIMITED, 4200)


def test_limit_duration_and_release():
    m = LPCStateMachine(now=0)
    m.heartbeat(1)
    m.limit(1, True, 4200, 60)
    assert (m.state, m.limit_until) == (LPCState.LIMITED, 61)
    m.heartbeat(50)
    assert not m.update(60)
    assert m.update(61)
    assert (m.state, m.limit_active) == (LPCState.UNLIMITED_CONTROLLED, False)
    m.limit(62, True, 4200, None)
    assert m.state == LPCState.LIMITED
    assert m.limit(63, False, 4200, None)
    assert m.state == LPCState.UNLIMITED_CONTROLLED
    m.limit(64, True, 4200, 0)  # duration 0 deactivates the limit
    assert m.state == LPCState.UNLIMITED_CONTROLLED


def test_failsafe_needs_heartbeat_and_new_limit():
    m = LPCStateMachine(now=0)
    m.heartbeat(1)
    m.limit(1, True, 4200, None)
    assert not m.update(121)
    assert m.update(121.5, FAILSAFE)
    assert m.state == LPCState.FAILSAFE
    assert not m.heartbeat(200, FAILSAFE)  # the heartbeat alone does not end failsafe
    assert m.state == LPCState.FAILSAFE
    assert m.limit(201, False, 0, None, FAILSAFE)
    assert m.state == LPCState.UNLIMITED_CONTROLLED
    # a limit without heartbeat is not evaluated
    m.update(400, FAILSAFE)
    assert m.state == LPCState.FAILSAFE
    assert not m.limit(401, True, 3000, None, FAILSAFE)
    assert not m.heartbeat(402, FAILSAFE)
    assert m.state == LPCState.FAILSAFE
    # a denied write after the heartbeat leaves failsafe as well [LPC-916]
    assert m.limit(403, True, 500, None, FAILSAFE, accepted=False)
    assert (m.state, m.limit_active) == (LPCState.UNLIMITED_CONTROLLED, False)


def test_failsafe_ends_if_no_limit_follows_the_heartbeat():
    # LPC-921: heartbeat back, but no limit within 120 s: unlimited/autonomous
    m = LPCStateMachine(now=0)
    m.heartbeat(1)
    m.limit(1, True, 4200, None)
    m.update(122, FAILSAFE)
    assert m.state == LPCState.FAILSAFE
    m.heartbeat(300)
    m.heartbeat(360)
    assert not m.update(419, FAILSAFE)
    assert m.heartbeat(420, FAILSAFE)
    assert m.state == LPCState.UNLIMITED_AUTONOMOUS
    # the heartbeat lost again in between restarts the 120 s
    m = LPCStateMachine(now=0)
    m.heartbeat(1)
    m.limit(1, True, 4200, None)
    m.update(122, FAILSAFE)
    m.heartbeat(300)
    m.update(421, FAILSAFE)  # heartbeat timed out
    m.heartbeat(500)
    assert not m.update(560, FAILSAFE)
    assert m.state == LPCState.FAILSAFE


def test_failsafe_ends_after_the_failsafe_duration():
    m = LPCStateMachine(now=0)
    m.heartbeat(1)
    m.limit(1, True, 4200, None)
    m.update(122, FAILSAFE)
    assert m.failsafe_until == 122 + FAILSAFE
    assert m.update(122 + FAILSAFE, FAILSAFE)
    assert m.state == LPCState.UNLIMITED_AUTONOMOUS


def test_autonomous_needs_heartbeat_and_new_limit():
    m = LPCStateMachine(now=0, autonomous_after_init=True)
    m.update(120)
    assert m.state == LPCState.UNLIMITED_AUTONOMOUS
    assert not m.heartbeat(7400)
    assert m.limit(7401, True, 5000, None)
    assert m.state == LPCState.LIMITED


# --- CS-LPC <-> pyeebus Energy Guard over SHIP ----------------------------------------------------


async def wait_for(condition, timeout: float = 5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        try:
            if result := condition():
                return result
        except DataNotAvailable:
            pass
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def guard_and_cs():
    node = {"announce": False, "discover": False, "host": "127.0.0.1"}
    eg = EebusService(Identity.create("eg"), brand="pyeebus", model="TestEG", serial="1", port=0, **node)
    cs = EebusService(Identity.create("cs"), brand="pyeebus", model="TestCS", serial="2", port=0, **node)
    events: list[str] = []
    approvals: list[object] = []
    clock = Clock()

    def approve_limit(limit: LoadLimit) -> str | None:
        approvals.append(limit)
        return "below the minimum" if limit.is_active and limit.value < 1000 else None

    def approve_configuration(values: dict[str, float]) -> str | None:
        approvals.append(values)
        return None

    lpc = LPC(eg.entities[0]).setup()
    cs_lpc = CsLPC(cs.entities[0], lambda _ski, _entity, name: events.append(name),
                   failsafe_limit=4200, failsafe_duration=FAILSAFE,
                   approve_limit=approve_limit, approve_configuration=approve_configuration,
                   clock=clock).setup()
    cs_lpc.set_consumption_nominal_max(11000)
    cs_lpc.set_consumption_limit(LoadLimit(11000, is_active=False))

    cs.node.trust.trust(eg.ski, eg.identity.cert_pem)
    eg.node.trust.trust(cs.ski)
    await cs.start()
    await eg.start()
    await eg.node.connect("127.0.0.1", cs.node.port, cs.ski)
    remote = await wait_for(lambda: next(iter(eg.remote_devices.values()), None))
    entity = await wait_for(lambda: next((e for e in remote.entities if e.type == "CEM"), None))
    yield lpc, cs_lpc, entity, events, approvals, clock
    cs_lpc.close()
    await eg.stop()
    await cs.stop()


async def test_local_values_and_features():
    cs = EebusService(Identity.create("cs"), brand="pyeebus", model="TestCS", serial="2", port=0,
                      device_type="HeatPumpAppliance", entity_types=("HeatPumpAppliance",))
    with pytest.raises(TypeError):
        CsLPC(cs.entities[0])  # the failsafe limit is required
    with pytest.raises(ValueError):
        CsLPC(cs.entities[0], failsafe_limit=4200, failsafe_duration=3600)
    uc = CsLPC(cs.entities[0], failsafe_limit=4200).setup()
    try:
        assert uc.consumption_limit() == LoadLimit(0, is_active=False, is_changeable=True)
        assert uc.failsafe_consumption_limit() == (4200, True)
        assert uc.failsafe_duration_minimum() == (FAILSAFE, True)  # default 2 h
        with pytest.raises(ValueError):
            uc.set_failsafe_duration_minimum(3600)
        uc.set_failsafe_duration_minimum(3 * 3600, changeable=False)
        assert uc.failsafe_duration_minimum() == (10800, False)
        uc.set_consumption_nominal_max(9000)
        assert uc.consumption_nominal_max() == 9000
        ec = cs.entities[0].feature("ElectricalConnection", "server")
        char = ec.data["electricalConnectionCharacteristicListData"]["electricalConnectionCharacteristicData"][0]
        assert char["characteristicType"] == "powerConsumptionNominalMax"  # not a HEMS
        uc.set_consumption_limit(LoadLimit(5000, duration=600))
        assert uc.consumption_limit() == LoadLimit(5000, duration=600)
        uc.set_consumption_limit(LoadLimit(6000, is_active=False))
        assert uc.consumption_limit().duration is None
        assert uc.state == LPCState.INIT
        assert uc.effective_limit() == 4200  # init: the failsafe limit
        use_cases = cs.device.node_management.data["nodeManagementUseCaseData"]["useCaseInformation"]
        assert use_cases[0]["actor"] == "ControllableSystem"
        assert use_cases[0]["useCaseSupport"][0]["scenarioSupport"] == [1, 2, 3, 4]
    finally:
        uc.close()


async def test_energy_guard_controls_cs(guard_and_cs):
    lpc, cs, entity, events, approvals, clock = guard_and_cs

    # the guard sees the start values
    assert await wait_for(lambda: lpc.is_scenario_available(entity, 1))
    limit = await wait_for(lambda: lpc.consumption_limit(entity))
    assert (limit.value, limit.is_active, limit.is_changeable) == (11000, False, True)
    assert await wait_for(lambda: lpc.failsafe_consumption_limit(entity)) == 4200
    assert lpc.failsafe_duration_minimum(entity) == FAILSAFE
    assert await wait_for(lambda: lpc.consumption_nominal_max(entity)) == 11000

    # init: failsafe limit until heartbeat and limit
    assert (cs.state, cs.effective_limit()) == (LPCState.INIT, 4200)
    await wait_for(cs.is_heartbeat_within_duration)
    assert CsLPC.DATA_UPDATE_HEARTBEAT in events
    assert cs.state == LPCState.INIT

    # limit with duration
    await lpc.write_consumption_limit(entity, LoadLimit(4000, True, duration=3600))
    assert approvals[-1] == LoadLimit(4000, True, duration=3600)
    assert (cs.state, cs.effective_limit()) == (LPCState.LIMITED, 4000)
    assert cs.consumption_limit() == LoadLimit(4000, True, True, 3600)
    assert CsLPC.DATA_UPDATE_LIMIT in events and CsLPC.STATE_CHANGED in events

    # denied by the application
    with pytest.raises(SpineError) as err:
        await lpc.write_consumption_limit(entity, LoadLimit(500, True))
    assert err.value.number == 7
    assert cs.consumption_limit().value == 4000

    # release (a write without duration drops the old duration)
    await lpc.write_consumption_limit(entity, LoadLimit(4000, False))
    assert (cs.state, cs.effective_limit()) == (LPCState.UNLIMITED_CONTROLLED, None)
    assert cs.consumption_limit().duration is None

    # failsafe values
    await lpc.write_failsafe_consumption_limit(entity, 5000)
    assert approvals[-1] == {"failsafeConsumptionActivePowerLimit": 5000}
    assert cs.failsafe_consumption_limit() == (5000, True)
    assert CsLPC.DATA_UPDATE_FAILSAFE_POWER in events
    await lpc.write_failsafe_duration_minimum(entity, 3 * 3600)
    assert cs.failsafe_duration_minimum() == (3 * 3600, True)
    assert CsLPC.DATA_UPDATE_FAILSAFE_DURATION in events
    with pytest.raises(SpineError):  # outside 2-24 h: always denied
        await lpc._write_config(entity, KEY_FAILSAFE_DURATION, {"duration": "PT1H"}, None)
    assert cs.failsafe_duration_minimum()[0] == 3 * 3600

    # the duration of a limit runs out: the guard sees the limit inactive
    await lpc.write_consumption_limit(entity, LoadLimit(3000, True, duration=60))
    assert cs.state == LPCState.LIMITED
    clock.now += 61
    cs.tick()
    assert cs.state == LPCState.UNLIMITED_CONTROLLED
    assert await wait_for(lambda: not lpc.consumption_limit(entity).is_active)

    # heartbeat lost: failsafe; a heartbeat alone does not end it, a new limit does
    await lpc.write_consumption_limit(entity, LoadLimit(3000, True))
    clock.now += 121
    events.clear()
    cs.tick()
    assert (cs.state, cs.effective_limit()) == (LPCState.FAILSAFE, 5000)
    assert events == [CsLPC.HEARTBEAT_TIMEOUT, CsLPC.STATE_CHANGED]
    await wait_for(cs.is_heartbeat_within_duration)
    assert cs.state == LPCState.FAILSAFE
    await lpc.write_consumption_limit(entity, LoadLimit(3500, True))
    assert (cs.state, cs.effective_limit()) == (LPCState.LIMITED, 3500)

    # failsafe for the failsafe duration minimum, then unlimited/autonomous
    clock.now += 121
    cs.tick()
    assert cs.state == LPCState.FAILSAFE
    clock.now += 3 * 3600
    cs.tick()
    assert (cs.state, cs.effective_limit()) == (LPCState.UNLIMITED_AUTONOMOUS, None)


async def test_not_changeable_limit_is_denied(guard_and_cs):
    lpc, cs, entity, _events, approvals, _clock = guard_and_cs
    cs.set_consumption_limit(LoadLimit(7000, is_active=False, is_changeable=False))
    await wait_for(lambda: not lpc.consumption_limit(entity).is_changeable)
    # the guard refuses itself; write directly to check the device side
    f = lpc_client(lpc, entity)
    item = {"limitId": cs._limit_id(), "isLimitActive": True, "value": {"number": 4000, "scale": 0}}
    with pytest.raises(SpineError):
        await f.write("loadControlLimitListData", {"loadControlLimitData": [item]})
    assert approvals == []
    assert scaled_value(cs._limit_item()["value"]) == 7000


def lpc_client(lpc: LPC, entity):
    from pyeebus.usecases import ClientFeature

    return ClientFeature("LoadControl", lpc.local, entity)
