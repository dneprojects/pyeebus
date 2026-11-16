"""Grid connection point use cases (port of eebus-go's gcp/mgcp).

* :class:`GcpMGCP` monitoring of the grid connection point, Grid Connection
  Point side: a HEMS or smart meter gateway publishes the power at the grid
  connection point (positive: consumption from the grid, negative: feed-in),
  the grid feed-in and consumption energy, currents, voltages, frequency and the
  PV feed-in power limitation factor for a Monitoring Appliance (e.g. a heat pump).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..spine.device import LocalEntity
from ..spine.model import Role, as_list, filter_items, scaled_number, scaled_value
from .base import (
    DEVICE_CONFIGURATION,
    ELECTRICAL_CONNECTION,
    MEASUREMENT,
    PHASES,
    DataNotAvailable,
    Scenario,
    UseCaseEventCallback,
    next_id,
)
from .grid import FN_CONFIG_DESCRIPTIONS, FN_CONFIG_VALUES
from .mu import (
    MEASURED,
    REAL_RMS,
    MeasurementServer,
    MeasurementSpec,
    MonitorCurrentConfig,
    MonitorFrequencyConfig,
    MonitorVoltageConfig,
    Value,
    voltage_parameter,
)

KEY_PV_CURTAILMENT = "pvCurtailmentLimitFactor"
GCP_VOLTAGE_PHASES = ("a", "b", "c", "ab", "bc", "ca")


@dataclass
class GridPowerConfig:
    """Scenario 2 (required): acPowerTotal at the grid connection point."""

    value_source: str = MEASURED
    constraints: dict[str, Any] | None = None


@dataclass
class GridEnergyConfig:
    """Scenarios 3 and 4 (required): gridFeedIn and gridConsumption energy."""

    value_source_feed_in: str = MEASURED
    value_source_consumption: str = MEASURED
    constraints_feed_in: dict[str, Any] | None = None
    constraints_consumption: dict[str, Any] | None = None


class GcpMGCP(MeasurementServer):
    """Monitoring of the grid connection point, Grid Connection Point actor.

    Scenarios: 1 PV feed-in power limitation factor (``pv_curtailment``), 2 power,
    3 feed-in energy, 4 consumed energy, 5 currents, 6 voltages (``a``, ``b``, ``c``,
    ``ab``, ``bc``, ``ca``), 7 frequency. Publish values with :meth:`update`.

    eebus-go announces this use case with the actor MonitoringAppliance; pyeebus
    uses GridConnectionPoint, which is what monitoring appliances (and eebus-go's
    ma/mgcp) look for.
    """

    actor = "GridConnectionPoint"
    name = "monitoringOfGridConnectionPoint"
    version = "1.0.0"
    valid_actors = ("MonitoringAppliance",)
    valid_entity_types = ()  # any

    def __init__(self, local_entity: LocalEntity, on_event: UseCaseEventCallback | None = None, *,
                 power: GridPowerConfig | None = None, energy: GridEnergyConfig | None = None,
                 current: MonitorCurrentConfig | None = None, voltage: MonitorVoltageConfig | None = None,
                 frequency: MonitorFrequencyConfig | None = None, pv_curtailment: bool = False) -> None:
        super().__init__(local_entity, on_event)
        self.power_config = power or GridPowerConfig()
        self.energy_config = energy or GridEnergyConfig()
        self.current_config, self.voltage_config, self.frequency_config = current, voltage, frequency
        self.pv_curtailment = pv_curtailment
        for phase in current.value_source_per_phase if current else ():
            if phase not in PHASES:
                raise ValueError(f"invalid current phase {phase}")
        for phase in voltage.value_source_per_phase if voltage else ():
            if phase not in GCP_VOLTAGE_PHASES:
                raise ValueError(f"invalid voltage phase {phase} (a, b, c, ab, bc or ca)")
        measured = (MEASUREMENT, ELECTRICAL_CONNECTION)
        scenarios = [Scenario(1, False, (DEVICE_CONFIGURATION,))] if pv_curtailment else []
        scenarios += [Scenario(2, True, measured), Scenario(3, True, measured), Scenario(4, False, measured)]
        for number, config in ((5, current), (6, voltage), (7, frequency)):
            if config is not None:
                scenarios.append(Scenario(number, False, measured))
        self.scenarios = tuple(scenarios)
        self._factor_key: int | None = None

    def add_features(self) -> None:
        dc = self.local.add_feature(DEVICE_CONFIGURATION, Role.SERVER)
        dc.add_function(FN_CONFIG_DESCRIPTIONS, read=True)
        dc.add_function(FN_CONFIG_VALUES, read=True)
        if self.pv_curtailment:
            descs = [dict(i) for i in as_list((dc.data.get(FN_CONFIG_DESCRIPTIONS) or {}).get(
                "deviceConfigurationKeyValueDescriptionData"))]
            existing = filter_items(descs, keyName=KEY_PV_CURTAILMENT)
            if existing:
                self._factor_key = existing[0]["keyId"]
            else:
                self._factor_key = next_id(descs, "keyId")
                descs.append({"keyId": self._factor_key, "keyName": KEY_PV_CURTAILMENT,
                              "valueType": "scaledNumber", "unit": "pct"})
                dc.set_data(FN_CONFIG_DESCRIPTIONS, {"deviceConfigurationKeyValueDescriptionData": descs})
        super().add_features()

    def measurement_specs(self) -> list[MeasurementSpec]:
        power, energy = self.power_config, self.energy_config
        specs = [
            MeasurementSpec("powerTotal", "power", "W", "acPowerTotal", power.value_source,
                            {"acMeasuredPhases": "abc", "acMeasuredInReferenceTo": "neutral", **REAL_RMS},
                            power.constraints),
            MeasurementSpec("energyFeedIn", "energy", "Wh", "gridFeedIn", energy.value_source_feed_in,
                            {"acMeasurementType": "real"}, energy.constraints_feed_in),
            MeasurementSpec("energyConsumed", "energy", "Wh", "gridConsumption", energy.value_source_consumption,
                            {"acMeasurementType": "real"}, energy.constraints_consumption),
        ]
        if (current := self.current_config) is not None:
            for phase in PHASES:
                if phase in current.value_source_per_phase:
                    specs.append(MeasurementSpec(f"current:{phase}", "current", "A", "acCurrent",
                                                 current.value_source_per_phase[phase],
                                                 {"acMeasuredPhases": phase, **REAL_RMS},
                                                 current.constraints_per_phase.get(phase)))
        if (voltage := self.voltage_config) is not None:
            for phase in GCP_VOLTAGE_PHASES:
                if phase in voltage.value_source_per_phase:
                    specs.append(MeasurementSpec(f"voltage:{phase}", "voltage", "V", "acVoltage",
                                                 voltage.value_source_per_phase[phase], voltage_parameter(phase),
                                                 voltage.constraints_per_phase.get(phase)))
        if (frequency := self.frequency_config) is not None:
            specs.append(MeasurementSpec("frequency", "frequency", "Hz", "acFrequency", frequency.value_source,
                                         {}, frequency.constraints))
        return specs

    # --- update -------------------------------------------------------------------------------

    def update(self, *, power_total: Value | None = None, energy_feed_in: Value | None = None,
               energy_consumed: Value | None = None, current_per_phase: dict[str, Value] | None = None,
               voltage_per_phase: dict[str, Value] | None = None, frequency: Value | None = None,
               power_limitation_factor: float | None = None) -> None:
        """Publish new values (only the given ones change); raises ValueError for values the
        configuration does not provide, without changing anything.

        ``power_total``: positive is consumption from the grid, negative feed-in (W).
        ``power_limitation_factor``: PV feed-in limitation in percent.
        """
        values: dict[str, Value] = {}
        for key, value in (("powerTotal", power_total), ("energyFeedIn", energy_feed_in),
                           ("energyConsumed", energy_consumed), ("frequency", frequency)):
            if value is not None:
                values[key] = value
        for prefix, per_phase in (("current", current_per_phase), ("voltage", voltage_per_phase)):
            for phase, value in (per_phase or {}).items():
                values[f"{prefix}:{phase}"] = value
        if power_limitation_factor is not None and self._factor_key is None:
            raise ValueError("not supported by the configuration: powerLimitationFactor")
        if values:
            self.publish(values)
        if power_limitation_factor is not None:
            dc = self._server(DEVICE_CONFIGURATION)
            items = [dict(i) for i in as_list((dc.data.get(FN_CONFIG_VALUES) or {}).get(
                "deviceConfigurationKeyValueData")) if i.get("keyId") != self._factor_key]
            items.append({"keyId": self._factor_key, "value": {"scaledNumber": scaled_number(power_limitation_factor)}})
            dc.set_data(FN_CONFIG_VALUES, {"deviceConfigurationKeyValueData":
                                           sorted(items, key=lambda i: i.get("keyId", 0))})

    # --- local values (as eebus-go) ------------------------------------------------------------

    def power_limitation_factor(self) -> float:
        if self._factor_key is None:
            raise DataNotAvailable("the power limitation factor is not configured")
        dc = self._server(DEVICE_CONFIGURATION)
        for item in as_list((dc.data.get(FN_CONFIG_VALUES) or {}).get("deviceConfigurationKeyValueData")):
            if item.get("keyId") == self._factor_key:
                value = scaled_value((item.get("value") or {}).get("scaledNumber"))
                if value is not None:
                    return value
        raise DataNotAvailable("no power limitation factor")

    def power(self) -> float:
        return self._value("powerTotal")

    def energy_feed_in(self) -> float:
        return self._value("energyFeedIn")

    def energy_consumed(self) -> float:
        return self._value("energyConsumed")

    def current_per_phase(self) -> dict[str, float]:
        return self._per_phase("current")

    def voltage_per_phase(self) -> dict[str, float]:
        return self._per_phase("voltage")

    def frequency(self) -> float:
        return self._value("frequency")
