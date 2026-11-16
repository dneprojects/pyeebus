"""MU-MPC: a monitored unit, read by the pyeebus Monitoring Appliance (MA-MPC)."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from pyeebus.service import EebusService
from pyeebus.ship import Identity
from pyeebus.usecases import (
    MPC,
    CsLPC,
    DataNotAvailable,
    MeasuredValue,
    MonitorCurrentConfig,
    MonitorEnergyConfig,
    MonitorFrequencyConfig,
    MonitorPowerConfig,
    MonitorVoltageConfig,
    MuMPC,
)

ABC = {"a": "measuredValue", "b": "measuredValue", "c": "measuredValue"}
NODE = {"announce": False, "discover": False, "host": "127.0.0.1"}


def full_mpc(entity, on_event=None) -> MuMPC:
    return MuMPC(entity, on_event,
                 power=MonitorPowerConfig("abc", "measuredValue", ABC,
                                          constraints_total={"valueRangeMin": {"number": -11000, "scale": 0},
                                                             "valueRangeMax": {"number": 11000, "scale": 0}}),
                 energy=MonitorEnergyConfig("measuredValue", "calculatedValue"),
                 current=MonitorCurrentConfig(ABC),
                 voltage=MonitorVoltageConfig({**ABC, "ab": "measuredValue"}),
                 frequency=MonitorFrequencyConfig()).setup()


def service(name: str, **kwargs) -> EebusService:
    return EebusService(Identity.create(name), brand="pyeebus", model=name, serial="1", port=0, **NODE, **kwargs)


def all_values(mpc: MuMPC, power: float = 3000) -> None:
    mpc.update(power_total=power, power_per_phase={"a": 1000, "b": 1000, "c": 1000}, energy_consumed=12345,
               energy_produced=10, current_per_phase={"a": 4.35, "b": 4.35, "c": 4.35},
               voltage_per_phase={"a": 230, "b": 231, "c": 232, "ab": 400}, frequency=50)


def test_descriptions_and_values():
    cs = service("mu", device_type="HeatPumpAppliance", entity_types=("HeatPumpAppliance",))
    entity = cs.entities[0]
    mpc = full_mpc(entity)
    assert [s.id for s in mpc.scenarios] == [1, 2, 3, 4, 5]
    meas = entity.feature("Measurement", "server")
    descs = meas.data["measurementDescriptionListData"]["measurementDescriptionData"]
    assert [d["scopeType"] for d in descs] == (
        ["acPowerTotal"] + ["acPower"] * 3 + ["acEnergyConsumed", "acEnergyProduced"] + ["acCurrent"] * 3
        + ["acVoltage"] * 4 + ["acFrequency"])
    assert [d["measurementId"] for d in descs] == list(range(14))
    constraints = meas.data["measurementConstraintsListData"]["measurementConstraintsData"]
    assert constraints == [{"valueRangeMin": {"number": -11000, "scale": 0},
                            "valueRangeMax": {"number": 11000, "scale": 0}, "measurementId": 0}]
    ec = entity.feature("ElectricalConnection", "server")
    assert ec.data["electricalConnectionDescriptionListData"]["electricalConnectionDescriptionData"] == [
        {"electricalConnectionId": 1, "powerSupplyType": "ac", "positiveEnergyDirection": "consume"}]
    params = ec.data["electricalConnectionParameterDescriptionListData"][
        "electricalConnectionParameterDescriptionData"]
    assert params[0]["acMeasuredPhases"] == "abc"
    voltage_ab = next(p for p in params if p["measurementId"] == mpc.ids["voltage:ab"])
    assert (voltage_ab["acMeasuredPhases"], voltage_ab["acMeasuredInReferenceTo"]) == ("a", "b")

    with pytest.raises(DataNotAvailable):
        mpc.power()
    all_values(mpc)
    stamp = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.UTC)
    mpc.update(power_total=MeasuredValue(3000, timestamp=stamp), energy_consumed=12345.5)
    assert mpc.power() == 3000
    assert mpc.power_per_phase() == {"a": 1000, "b": 1000, "c": 1000}
    assert mpc.energy_consumed() == pytest.approx(12345.5)
    assert mpc.energy_produced() == 10
    assert mpc.current_per_phase() == pytest.approx({"a": 4.35, "b": 4.35, "c": 4.35})
    assert mpc.voltage_per_phase() == {"a": 230, "b": 231, "c": 232, "ab": 400}
    assert mpc.frequency() == 50
    data = {m["measurementId"]: m for m in meas.data["measurementListData"]["measurementData"]}
    assert data[0] == {"measurementId": 0, "valueType": "value", "value": {"number": 3000, "scale": 0},
                       "valueSource": "measuredValue", "timestamp": "2026-11-16T12:00:00Z"}
    assert data[mpc.ids["energyProduced"]]["valueSource"] == "calculatedValue"
    mpc.update(power_total=MeasuredValue(0, value_state="error"))
    data = {m["measurementId"]: m for m in meas.data["measurementListData"]["measurementData"]}
    assert data[0]["valueState"] == "error"
    assert scaled(data[mpc.ids["frequency"]]) == 50  # the other values stay


def scaled(item):
    return item["value"]["number"] * 10 ** item["value"].get("scale", 0)


def test_unsupported_values_and_phases():
    cs = service("mu")
    mpc = MuMPC(cs.entities[0], power=MonitorPowerConfig("a", "measuredValue", {"a": "measuredValue"})).setup()
    assert [s.id for s in mpc.scenarios] == [1]
    with pytest.raises(ValueError):
        mpc.update(power_total=100, energy_consumed=5)
    with pytest.raises(DataNotAvailable):
        mpc.power()  # nothing was changed
    with pytest.raises(ValueError):
        MuMPC(cs.entities[0], power=MonitorPowerConfig("ab", "measuredValue", {"c": "measuredValue"}))
    with pytest.raises(ValueError):
        MuMPC(cs.entities[0], power=MonitorPowerConfig("ab"), voltage=MonitorVoltageConfig({"bc": "measuredValue"}))


async def test_lpc_uses_the_mpc_measurement():
    cs = service("mu")
    mpc = full_mpc(cs.entities[0])
    lpc = CsLPC(cs.entities[0]).setup()
    try:
        lc = cs.entities[0].feature("LoadControl", "server")
        desc = lc.data["loadControlLimitDescriptionListData"]["loadControlLimitDescriptionData"][0]
        assert desc["measurementId"] == mpc.ids["powerTotal"]
        ec = cs.entities[0].feature("ElectricalConnection", "server")
        char = ec.data["electricalConnectionCharacteristicListData"]["electricalConnectionCharacteristicData"][0]
        assert (char["electricalConnectionId"], char["parameterId"]) == (1, 0)
    finally:
        lpc.close()


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


async def test_monitoring_appliance_reads_monitored_unit():
    ma = service("ma")
    mu = service("mu", device_type="HeatPumpAppliance", entity_types=("HeatPumpAppliance",))
    events: list[str] = []
    ma_mpc = MPC(ma.entities[0], lambda _s, _e, name: events.append(name)).setup()
    mu_mpc = full_mpc(mu.entities[0], lambda _s, _e, name: events.append("mu:" + name))
    all_values(mu_mpc)
    mu.node.trust.trust(ma.ski, ma.identity.cert_pem)
    ma.node.trust.trust(mu.ski)
    await mu.start()
    await ma.start()
    try:
        await ma.node.connect("127.0.0.1", mu.node.port, mu.ski)
        remote = await wait_for(lambda: next(iter(ma.remote_devices.values()), None))
        entity = await wait_for(lambda: next((e for e in remote.entities if e.type == "HeatPumpAppliance"), None))
        assert await wait_for(lambda: ma_mpc.is_scenario_available(entity, 5))
        assert await wait_for(lambda: ma_mpc.power(entity)) == 3000
        assert ma_mpc.power_per_phase(entity) == [1000, 1000, 1000]
        assert ma_mpc.energy_consumed(entity) == 12345
        assert ma_mpc.energy_produced(entity) == 10
        assert ma_mpc.current_per_phase(entity) == pytest.approx([4.35, 4.35, 4.35])
        assert ma_mpc.voltage_per_phase(entity) == [230, 231, 232]
        assert ma_mpc.frequency(entity) == 50
        assert await wait_for(lambda: "mu:UseCaseSupportUpdate" in events)  # the MU sees the MA

        mu_mpc.update(power_total=6000)
        assert await wait_for(lambda: ma_mpc.power(entity) == 6000)
        assert MPC.DATA_UPDATE_POWER in events
    finally:
        await ma.stop()
        await mu.stop()
