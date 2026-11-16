"""Monitored Unit use cases (port of eebus-go's mu/mpc).

* :class:`MuMPC` monitoring of power consumption, Monitored Unit side: the
  device publishes its power (total and per phase), energy, currents, voltages
  and frequency for a Monitoring Appliance (e.g. an energy guard or a HEMS).

Set up MuMPC before :class:`~pyeebus.usecases.cs.CsLPC` on the same entity, so
the LPC limit refers to the acPowerTotal measurement (as eebus-go).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from ..spine.device import LocalEntity, LocalFeature
from ..spine.model import Role, as_list, filter_items, format_datetime, scaled_number, scaled_value
from .base import (
    ELECTRICAL_CONNECTION,
    MEASUREMENT,
    PHASES,
    DataNotAvailable,
    Scenario,
    UseCase,
    UseCaseEventCallback,
    next_id,
)
from .grid import (
    FN_EC_DESCRIPTIONS,
    FN_EC_PARAMETERS,
    FN_MEAS_CONSTRAINTS,
    FN_MEAS_DESCRIPTIONS,
    FN_MEASUREMENTS,
)

MEASURED = "measuredValue"
CALCULATED = "calculatedValue"
EMPIRICAL = "empiricalValue"

VOLTAGE_PHASES = ("a", "b", "c", "ab", "bc", "ac")


@dataclass
class MonitorPowerConfig:
    """Scenario 1 (required): acPowerTotal and, per phase in ``value_source_per_phase``, acPower.

    Value sources are ``measuredValue``, ``calculatedValue`` or ``empiricalValue``;
    constraints are SPINE ``measurementConstraintsData`` fields (``valueRangeMin``, ...).
    """

    connected_phases: str = "abc"
    value_source_total: str = MEASURED
    value_source_per_phase: dict[str, str] = field(default_factory=dict)
    constraints_total: dict[str, Any] | None = None
    constraints_per_phase: dict[str, dict[str, Any]] = field(default_factory=dict)

    def supports(self, *phases: str) -> bool:
        return all(p.lower() in self.connected_phases.lower() for p in phases)


@dataclass
class MonitorEnergyConfig:
    """Scenario 2: acEnergyConsumed and/or acEnergyProduced (a source enables it)."""

    value_source_consumption: str | None = None
    value_source_production: str | None = None
    constraints_consumption: dict[str, Any] | None = None
    constraints_production: dict[str, Any] | None = None


@dataclass
class MonitorCurrentConfig:
    """Scenario 3: acCurrent per phase."""

    value_source_per_phase: dict[str, str] = field(default_factory=dict)
    constraints_per_phase: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class MonitorVoltageConfig:
    """Scenario 4: acVoltage per phase (``a``, ``b``, ``c``) or between phases (``ab``, ``bc``, ``ac``)."""

    value_source_per_phase: dict[str, str] = field(default_factory=dict)
    constraints_per_phase: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class MonitorFrequencyConfig:
    """Scenario 5: acFrequency."""

    value_source: str = MEASURED
    constraints: dict[str, Any] | None = None


@dataclass
class MeasuredValue:
    """A value with optional timestamp, value state (if not ``normal``) and evaluation period."""

    value: float
    timestamp: dt.datetime | None = None
    value_state: str | None = None
    evaluation_start: dt.datetime | None = None
    evaluation_end: dt.datetime | None = None


Value = float | MeasuredValue


@dataclass
class MeasurementSpec:
    """One measurement a monitoring use case offers (description, parameter, constraint)."""

    key: str  # "powerTotal", "power:a", "energyConsumed", "current:a", "voltage:ab", ...
    measurement_type: str
    unit: str
    scope: str
    source: str
    parameter: dict[str, Any]
    constraint: dict[str, Any] | None = None


REAL_RMS = {"acMeasurementType": "real", "acMeasurementVariant": "rms"}


def voltage_parameter(phase: str) -> dict[str, Any]:
    """Phase to neutral (``a``) or phase to phase (``ab``: a in reference to b)."""
    if len(phase) == 1:
        reference = {"acMeasuredPhases": phase, "acMeasuredInReferenceTo": "neutral"}
    else:
        reference = {"acMeasuredPhases": phase[0], "acMeasuredInReferenceTo": phase[1]}
    return {**reference, "acMeasurementType": "apparent", "acMeasurementVariant": "rms"}


class MeasurementServer(UseCase):
    """Server side of a monitoring use case (MU-MPC, GCP-MGCP): Measurement and
    ElectricalConnection server features filled from :meth:`measurement_specs`."""

    def __init__(self, local_entity: LocalEntity, on_event: UseCaseEventCallback | None = None) -> None:
        super().__init__(local_entity, on_event)
        self.ids: dict[str, int] = {}
        """Measurement ids by key ("powerTotal", "power:a", "voltage:ab", ...)."""
        self._sources: dict[str, str] = {}

    def measurement_specs(self) -> list[MeasurementSpec]:
        raise NotImplementedError

    def add_features(self) -> None:
        super().add_features()
        ec = self.local.add_feature(ELECTRICAL_CONNECTION, Role.SERVER)
        ec.add_function(FN_EC_DESCRIPTIONS, read=True)
        ec.add_function(FN_EC_PARAMETERS, read=True)
        meas = self.local.add_feature(MEASUREMENT, Role.SERVER)
        for fn in (FN_MEAS_DESCRIPTIONS, FN_MEAS_CONSTRAINTS, FN_MEASUREMENTS):
            meas.add_function(fn, read=True)

        connection_id = self._connection_id(ec)
        descs = _items(meas, FN_MEAS_DESCRIPTIONS, "measurementDescriptionData")
        params = _items(ec, FN_EC_PARAMETERS, "electricalConnectionParameterDescriptionData")
        constraints = _items(meas, FN_MEAS_CONSTRAINTS, "measurementConstraintsData")
        for spec in self.measurement_specs():
            measurement_id = next_id(descs, "measurementId")
            descs.append({"measurementId": measurement_id, "measurementType": spec.measurement_type,
                          "commodityType": "electricity", "unit": spec.unit, "scopeType": spec.scope})
            params.append({"electricalConnectionId": connection_id,
                           "parameterId": next_id(params, "parameterId"),
                           "measurementId": measurement_id, "voltageType": "ac", **spec.parameter})
            if spec.constraint:
                constraints.append({**spec.constraint, "measurementId": measurement_id})
            self.ids[spec.key] = measurement_id
            self._sources[spec.key] = spec.source
        meas.set_data(FN_MEAS_DESCRIPTIONS, {"measurementDescriptionData": descs})
        ec.set_data(FN_EC_PARAMETERS, {"electricalConnectionParameterDescriptionData": params})
        if constraints:
            meas.set_data(FN_MEAS_CONSTRAINTS, {"measurementConstraintsData": constraints})

    @staticmethod
    def _connection_id(ec: LocalFeature) -> int:
        """The id of the AC consumption connection, added if missing (eebus-go GetOrAddIdForDescription)."""
        descs = _items(ec, FN_EC_DESCRIPTIONS, "electricalConnectionDescriptionData")
        wanted = {"powerSupplyType": "ac", "positiveEnergyDirection": "consume"}
        for desc in descs:
            rest = {k: v for k, v in desc.items() if k != "electricalConnectionId"}
            if desc.get("electricalConnectionId") is not None and rest == wanted:
                return desc["electricalConnectionId"]
        connection_id = max((d["electricalConnectionId"] for d in descs
                             if isinstance(d.get("electricalConnectionId"), int)), default=0) + 1
        descs.append({"electricalConnectionId": connection_id, **wanted})
        ec.set_data(FN_EC_DESCRIPTIONS, {"electricalConnectionDescriptionData": descs})
        return connection_id

    def publish(self, values: dict[str, Value]) -> None:
        """Publish values by key in one notify; raises ValueError (and changes nothing)
        for keys the configuration does not provide."""
        unsupported = [key for key in values if key not in self.ids]
        if unsupported:
            raise ValueError(f"not supported by the configuration: {', '.join(unsupported)}")
        meas = self._server(MEASUREMENT)
        items = {i.get("measurementId"): i for i in _items(meas, FN_MEASUREMENTS, "measurementData")}
        for key, value in values.items():
            items[self.ids[key]] = self._measurement(self.ids[key], value, self._sources[key])
        meas.set_data(FN_MEASUREMENTS, {"measurementData": [items[k] for k in sorted(items)]})

    @staticmethod
    def _measurement(measurement_id: int, value: Value, source: str) -> dict[str, Any]:
        if not isinstance(value, MeasuredValue):
            value = MeasuredValue(float(value))
        item: dict[str, Any] = {"measurementId": measurement_id, "valueType": "value",
                                "value": scaled_number(value.value), "valueSource": source}
        if value.timestamp is not None:
            item["timestamp"] = format_datetime(value.timestamp)
        if value.value_state is not None:
            item["valueState"] = value.value_state
        if value.evaluation_start is not None and value.evaluation_end is not None:
            item["evaluationPeriod"] = {"startTime": format_datetime(value.evaluation_start),
                                        "endTime": format_datetime(value.evaluation_end)}
        return item

    def _server(self, feature_type: str) -> LocalFeature:
        feature = self.local.feature(feature_type, Role.SERVER)
        if feature is None:
            raise DataNotAvailable(f"local {feature_type} server not found")
        return feature

    def _value(self, key: str) -> float:
        if key not in self.ids:
            raise DataNotAvailable(f"{key} is not configured")
        items = filter_items(_items(self._server(MEASUREMENT), FN_MEASUREMENTS, "measurementData"),
                             measurementId=self.ids[key])
        if not items or scaled_value(items[0].get("value")) is None:
            raise DataNotAvailable(f"no {key} value")
        return scaled_value(items[0]["value"])

    def _per_phase(self, prefix: str) -> dict[str, float]:
        return {key.split(":", 1)[1]: self._value(key) for key in self.ids if key.startswith(prefix + ":")}


class MuMPC(MeasurementServer):
    """Monitoring of power consumption, Monitored Unit actor.

    Positive values are consumption (``positiveEnergyDirection`` consume).
    Publish values with :meth:`update`; subscribers are notified once per call.
    """

    actor = "MonitoredUnit"
    name = "monitoringOfPowerConsumption"
    version = "1.0.0"
    valid_actors = ("MonitoringAppliance",)
    valid_entity_types = ()  # any (eebus-go: no entity type check)

    def __init__(self, local_entity: LocalEntity, on_event: UseCaseEventCallback | None = None, *,
                 power: MonitorPowerConfig | None = None, energy: MonitorEnergyConfig | None = None,
                 current: MonitorCurrentConfig | None = None, voltage: MonitorVoltageConfig | None = None,
                 frequency: MonitorFrequencyConfig | None = None) -> None:
        super().__init__(local_entity, on_event)
        self.power_config = power or MonitorPowerConfig()
        self.energy_config, self.current_config = energy, current
        self.voltage_config, self.frequency_config = voltage, frequency
        self._check_config()
        features = (ELECTRICAL_CONNECTION, MEASUREMENT)
        scenarios = [Scenario(1, True, features)]
        for number, config in ((2, energy), (3, current), (4, voltage), (5, frequency)):
            if config is not None:
                scenarios.append(Scenario(number, False, features))
        self.scenarios = tuple(scenarios)

    def _check_config(self) -> None:
        power = self.power_config
        for phase in [*power.value_source_per_phase,
                      *(self.current_config.value_source_per_phase if self.current_config else ())]:
            if phase not in PHASES or not power.supports(phase):
                raise ValueError(f"phase {phase} is not in the connected phases {power.connected_phases!r}")
        for phase in self.voltage_config.value_source_per_phase if self.voltage_config else ():
            if phase not in VOLTAGE_PHASES or not power.supports(*phase):
                raise ValueError(f"voltage phase {phase} is not in the connected phases {power.connected_phases!r}")

    def measurement_specs(self) -> list[MeasurementSpec]:
        power = self.power_config
        specs = [MeasurementSpec("powerTotal", "power", "W", "acPowerTotal", power.value_source_total,
                                 {"acMeasuredPhases": power.connected_phases,
                                  "acMeasuredInReferenceTo": "neutral", **REAL_RMS},
                                 power.constraints_total)]
        for phase in _ordered(power.value_source_per_phase):
            specs.append(MeasurementSpec(f"power:{phase}", "power", "W", "acPower",
                                         power.value_source_per_phase[phase],
                                         {"acMeasuredPhases": phase, "acMeasuredInReferenceTo": "neutral",
                                          **REAL_RMS},
                                         power.constraints_per_phase.get(phase)))
        if (energy := self.energy_config) is not None:
            if energy.value_source_consumption:
                specs.append(MeasurementSpec("energyConsumed", "energy", "Wh", "acEnergyConsumed",
                                             energy.value_source_consumption, {"acMeasurementType": "real"},
                                             energy.constraints_consumption))
            if energy.value_source_production:
                specs.append(MeasurementSpec("energyProduced", "energy", "Wh", "acEnergyProduced",
                                             energy.value_source_production, {"acMeasurementType": "real"},
                                             energy.constraints_production))
        if (current := self.current_config) is not None:
            for phase in _ordered(current.value_source_per_phase):
                specs.append(MeasurementSpec(f"current:{phase}", "current", "A", "acCurrent",
                                             current.value_source_per_phase[phase],
                                             {"acMeasuredPhases": phase, **REAL_RMS},
                                             current.constraints_per_phase.get(phase)))
        if (voltage := self.voltage_config) is not None:
            for phase in _ordered(voltage.value_source_per_phase):
                specs.append(MeasurementSpec(f"voltage:{phase}", "voltage", "V", "acVoltage",
                                             voltage.value_source_per_phase[phase], voltage_parameter(phase),
                                             voltage.constraints_per_phase.get(phase)))
        if (frequency := self.frequency_config) is not None:
            specs.append(MeasurementSpec("frequency", "frequency", "Hz", "acFrequency",
                                         frequency.value_source, {}, frequency.constraints))
        return specs

    # --- update --------------------------------------------------------------------------------

    def update(self, *, power_total: Value | None = None, power_per_phase: dict[str, Value] | None = None,
               energy_consumed: Value | None = None, energy_produced: Value | None = None,
               current_per_phase: dict[str, Value] | None = None,
               voltage_per_phase: dict[str, Value] | None = None,
               frequency: Value | None = None) -> None:
        """Publish new values (only the given ones change); raises ValueError for values
        the configuration does not provide, without changing anything."""
        values: dict[str, Value] = {}
        for key, value in (("powerTotal", power_total), ("energyConsumed", energy_consumed),
                           ("energyProduced", energy_produced), ("frequency", frequency)):
            if value is not None:
                values[key] = value
        for prefix, per_phase in (("power", power_per_phase), ("current", current_per_phase),
                                  ("voltage", voltage_per_phase)):
            for phase, value in (per_phase or {}).items():
                values[f"{prefix}:{phase}"] = value
        self.publish(values)

    def power(self) -> float:
        return self._value("powerTotal")

    def power_per_phase(self) -> dict[str, float]:
        return self._per_phase("power")

    def energy_consumed(self) -> float:
        return self._value("energyConsumed")

    def energy_produced(self) -> float:
        return self._value("energyProduced")

    def current_per_phase(self) -> dict[str, float]:
        return self._per_phase("current")

    def voltage_per_phase(self) -> dict[str, float]:
        return self._per_phase("voltage")

    def frequency(self) -> float:
        return self._value("frequency")


def _items(feature: LocalFeature, function: str, list_field: str) -> list[dict[str, Any]]:
    return [dict(i) for i in as_list((feature.data.get(function) or {}).get(list_field))]


def _ordered(phases: dict[str, Any]) -> list[str]:
    return sorted(phases, key=lambda p: (len(p), p))
