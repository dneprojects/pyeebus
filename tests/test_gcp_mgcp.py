"""GCP-MGCP: the grid connection point, read by the pyeebus Monitoring Appliance (MA-MGCP)."""

from __future__ import annotations

import asyncio

import pytest

from pyeebus.service import EebusService
from pyeebus.ship import Identity
from pyeebus.usecases import (
    MGCP,
    DataNotAvailable,
    GcpMGCP,
    GridPowerConfig,
    MonitorCurrentConfig,
    MonitorFrequencyConfig,
    MonitorPowerConfig,
    MonitorVoltageConfig,
    MuMPC,
)

ABC = {"a": "measuredValue", "b": "measuredValue", "c": "measuredValue"}
NODE = {"announce": False, "discover": False, "host": "127.0.0.1"}


def service(name: str, **kwargs) -> EebusService:
    return EebusService(Identity.create(name), brand="pyeebus", model=name, serial="1", port=0, **NODE, **kwargs)


def full_gcp(entity, on_event=None) -> GcpMGCP:
    return GcpMGCP(entity, on_event, pv_curtailment=True,
                   power=GridPowerConfig(constraints={"valueRangeMin": {"number": -30000, "scale": 0}}),
                   current=MonitorCurrentConfig(ABC),
                   voltage=MonitorVoltageConfig({**ABC, "ab": "measuredValue", "bc": "measuredValue",
                                                 "ca": "measuredValue"}),
                   frequency=MonitorFrequencyConfig()).setup()


def all_values(gcp: GcpMGCP, power: float = -2500) -> None:
    gcp.update(power_total=power, energy_feed_in=5000, energy_consumed=12000,
               current_per_phase={"a": 4, "b": 3, "c": 4},
               voltage_per_phase={"a": 230, "b": 231, "c": 232, "ab": 400, "bc": 401, "ca": 402},
               frequency=50, power_limitation_factor=70)


def test_descriptions_and_values():
    gw = service("gcp")
    entity = gw.entities[0]
    gcp = full_gcp(entity)
    assert [s.id for s in gcp.scenarios] == [1, 2, 3, 4, 5, 6, 7]
    meas = entity.feature("Measurement", "server")
    descs = meas.data["measurementDescriptionListData"]["measurementDescriptionData"]
    assert [d["scopeType"] for d in descs] == (["acPowerTotal", "gridFeedIn", "gridConsumption"]
                                               + ["acCurrent"] * 3 + ["acVoltage"] * 6 + ["acFrequency"])
    ec = entity.feature("ElectricalConnection", "server")
    params = ec.data["electricalConnectionParameterDescriptionListData"][
        "electricalConnectionParameterDescriptionData"]
    voltage_ca = next(p for p in params if p["measurementId"] == gcp.ids["voltage:ca"])
    assert (voltage_ca["acMeasuredPhases"], voltage_ca["acMeasuredInReferenceTo"]) == ("c", "a")
    dc = entity.feature("DeviceConfiguration", "server")
    assert dc.data["deviceConfigurationKeyValueDescriptionListData"]["deviceConfigurationKeyValueDescriptionData"] \
        == [{"keyId": 0, "keyName": "pvCurtailmentLimitFactor", "valueType": "scaledNumber", "unit": "pct"}]
    assert not dc.operations["deviceConfigurationKeyValueListData"].write

    with pytest.raises(DataNotAvailable):
        gcp.power_limitation_factor()
    all_values(gcp)
    assert gcp.power() == -2500
    assert gcp.energy_feed_in() == 5000
    assert gcp.energy_consumed() == 12000
    assert gcp.current_per_phase() == {"a": 4, "b": 3, "c": 4}
    assert gcp.voltage_per_phase() == {"a": 230, "b": 231, "c": 232, "ab": 400, "bc": 401, "ca": 402}
    assert gcp.frequency() == 50
    assert gcp.power_limitation_factor() == 70


def test_minimal_config_and_errors():
    gw = service("gcp")
    gcp = GcpMGCP(gw.entities[0]).setup()
    assert [s.id for s in gcp.scenarios] == [2, 3, 4]
    with pytest.raises(ValueError):
        gcp.update(power_total=100, power_limitation_factor=60)
    with pytest.raises(ValueError):
        gcp.update(frequency=50)
    with pytest.raises(DataNotAvailable):
        gcp.power()
    with pytest.raises(ValueError):
        GcpMGCP(gw.entities[0], voltage=MonitorVoltageConfig({"ac": "measuredValue"}))


def test_gcp_next_to_mpc_shares_the_connection():
    gw = service("gcp")
    entity = gw.entities[0]
    mpc = MuMPC(entity, power=MonitorPowerConfig("abc")).setup()
    gcp = GcpMGCP(entity).setup()
    ec = entity.feature("ElectricalConnection", "server")
    assert len(ec.data["electricalConnectionDescriptionListData"]["electricalConnectionDescriptionData"]) == 1
    assert mpc.ids["powerTotal"] != gcp.ids["powerTotal"]
    mpc.update(power_total=3000)
    gcp.update(power_total=-1000)
    assert (mpc.power(), gcp.power()) == (3000, -1000)


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


async def test_monitoring_appliance_reads_grid_connection_point():
    ma = service("ma", device_type="HeatPumpAppliance", entity_types=("HeatPumpAppliance",))
    gw = service("gcp")
    events: list[str] = []
    ma_mgcp = MGCP(ma.entities[0], lambda _s, _e, name: events.append(name)).setup()
    gcp = full_gcp(gw.entities[0])
    all_values(gcp)
    gw.node.trust.trust(ma.ski, ma.identity.cert_pem)
    ma.node.trust.trust(gw.ski)
    await gw.start()
    await ma.start()
    try:
        await ma.node.connect("127.0.0.1", gw.node.port, gw.ski)
        remote = await wait_for(lambda: next(iter(ma.remote_devices.values()), None))
        entity = await wait_for(lambda: next((e for e in remote.entities if e.type == "CEM"), None))
        assert await wait_for(lambda: ma_mgcp.is_scenario_available(entity, 7))
        assert ma_mgcp.is_scenario_available(entity, 1)
        assert await wait_for(lambda: ma_mgcp.power(entity)) == -2500
        assert await wait_for(lambda: ma_mgcp.power_limitation_factor(entity)) == 70
        assert ma_mgcp.energy_feed_in(entity) == 5000
        assert ma_mgcp.energy_consumed(entity) == 12000
        assert ma_mgcp.current_per_phase(entity) == [4, 3, 4]
        assert ma_mgcp.voltage_per_phase(entity) == [230, 231, 232]
        assert ma_mgcp.frequency(entity) == 50

        gcp.update(power_total=1500, power_limitation_factor=60)
        assert await wait_for(lambda: ma_mgcp.power(entity) == 1500)
        assert await wait_for(lambda: ma_mgcp.power_limitation_factor(entity) == 60)
        assert MGCP.DATA_UPDATE_POWER_LIMITATION_FACTOR in events
        assert MGCP.DATA_UPDATE_ENERGY_FEED_IN in events
    finally:
        await ma.stop()
        await gw.stop()
