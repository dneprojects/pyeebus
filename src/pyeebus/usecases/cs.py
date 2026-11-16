"""Controllable System use cases (port of eebus-go's cs/lpc and cs/lpp).

* :class:`CsLPP` limitation of power production (feed-in), the same with the
  production direction, failsafe production limit and production nominal max.
* :class:`CsLPC` limitation of power consumption, Controllable System side:
  the device (e.g. a heat pump, wallbox or HEMS behind a §14a EnWG control box)
  offers an active power limit, the failsafe values and its nominal maximum to
  an Energy Guard, receives the guard's heartbeat and approves or denies the
  guard's writes.
* :class:`LimitationStateMachine` the LPC/LPP states of the Controllable System (init,
  unlimited/controlled, limited, failsafe, unlimited/autonomous). eebus-go leaves
  this to the application; pyeebus runs it inside :class:`CsLPC`, so the
  application only has to apply :meth:`CsLPC.effective_limit`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..spine.device import (
    Change,
    Event,
    EventType,
    LocalFeature,
    Message,
    RemoteEntity,
    RemoteFeature,
    spawn,
)
from ..spine.model import (
    Address,
    CmdClassifier,
    ErrorNumber,
    Role,
    SpineError,
    as_list,
    filter_items,
    format_duration,
    parse_duration,
    scaled_number,
    scaled_value,
)
from .base import (
    DEVICE_CONFIGURATION,
    DEVICE_DIAGNOSIS,
    ELECTRICAL_CONNECTION,
    LOAD_CONTROL,
    MEASUREMENT,
    ClientFeature,
    DataNotAvailable,
    Scenario,
    UseCase,
    UseCaseEventCallback,
    add_write_approval,
    next_id,
)
from .grid import (
    FN_CONFIG_DESCRIPTIONS,
    FN_CONFIG_VALUES,
    FN_EC_CHARACTERISTICS,
    FN_HEARTBEAT,
    FN_LIMIT_DESCRIPTIONS,
    FN_LIMITS,
    KEY_FAILSAFE_DURATION,
    LoadLimit,
)

KEY_FAILSAFE_CONSUMPTION = "failsafeConsumptionActivePowerLimit"
KEY_FAILSAFE_PRODUCTION = "failsafeProductionActivePowerLimit"

_LOGGER = logging.getLogger(__name__)

FAILSAFE_DURATION_MIN = 2 * 3600
FAILSAFE_DURATION_MAX = 24 * 3600
HEARTBEAT_TIMEOUT_SECONDS = 120.0
"""The Controllable System goes to failsafe if no heartbeat came for this long (LPC: 120 s)."""

LimitApproval = Callable[[LoadLimit], str | None]
"""Return None to accept an incoming limit, or the reason for denying it."""
ConfigurationApproval = Callable[[dict[str, float]], str | None]
"""Called with the written failsafe values (``failsafeConsumptionActivePowerLimit`` in W,
``failsafeDurationMinimum`` in s); return None to accept, or the reason for denying."""


# --- state machine ---------------------------------------------------------------------------


class LimitationState:
    INIT = "init"
    UNLIMITED_CONTROLLED = "unlimitedControlled"
    LIMITED = "limited"
    FAILSAFE = "failsafe"
    UNLIMITED_AUTONOMOUS = "unlimitedAutonomous"


@dataclass
class LimitationStateMachine:
    """The LPC states of a Controllable System, driven with monotonic timestamps.

    * init (after start): the failsafe limit applies. A heartbeat plus a limit
      write lead to limited or unlimited/controlled; no heartbeat plus limit
      within ``heartbeat_timeout`` leads to failsafe.
    * unlimited/controlled and limited follow the limit writes; an active limit
      with a duration ends after it (back to unlimited/controlled).
    * heartbeat missing for longer than ``heartbeat_timeout``: failsafe, the
      failsafe limit applies for the failsafe duration minimum.
    * failsafe and unlimited/autonomous are left only by a heartbeat together
      with a limit written after entering the state; failsafe ends after the
      failsafe duration minimum (unlimited/autonomous).
    """

    heartbeat_timeout: float = HEARTBEAT_TIMEOUT_SECONDS
    now: float = 0.0
    state: str = LimitationState.INIT
    since: float = field(init=False)
    last_heartbeat: float | None = None
    limit_active: bool = False
    limit_value: float | None = None
    limit_until: float | None = None
    failsafe_until: float | None = None
    _limit_received: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.since = self.now

    def heartbeat_ok(self, now: float) -> bool:
        return self.last_heartbeat is not None and now - self.last_heartbeat <= self.heartbeat_timeout

    def _set(self, state: str, now: float, failsafe_duration: float = 0.0) -> bool:
        if state == self.state:
            return False
        self.state, self.since = state, now
        self._limit_received = False
        self.failsafe_until = now + failsafe_duration if state == LimitationState.FAILSAFE else None
        return True

    def _controlled(self, now: float) -> bool:
        if self.limit_active and self.limit_until is not None and now >= self.limit_until:
            self.limit_active, self.limit_until = False, None
        return self._set(LimitationState.LIMITED if self.limit_active else LimitationState.UNLIMITED_CONTROLLED, now)

    def heartbeat(self, now: float, failsafe_duration: float = 0.0) -> bool:
        """A heartbeat of the Energy Guard arrived; returns True if the state changed."""
        self.last_heartbeat = now
        return self.update(now, failsafe_duration)

    def limit(self, now: float, active: bool, value: float | None, duration: float | None,
              failsafe_duration: float = 0.0) -> bool:
        """An (approved) limit write arrived; returns True if the state changed."""
        if duration is not None and duration <= 0:
            active = False  # LPC implementation guideline 2.2: duration 0 deactivates the limit
            duration = None
        self.limit_active, self.limit_value = active, value
        self.limit_until = now + duration if active and duration else None
        self._limit_received = True
        if self.state in (LimitationState.LIMITED, LimitationState.UNLIMITED_CONTROLLED):
            return self._controlled(now)
        return self.update(now, failsafe_duration)

    def update(self, now: float, failsafe_duration: float = 0.0) -> bool:
        """Apply the timeouts; returns True if the state changed."""
        hb_ok = self.heartbeat_ok(now)
        if self.state in (LimitationState.LIMITED, LimitationState.UNLIMITED_CONTROLLED):
            if not hb_ok:
                return self._set(LimitationState.FAILSAFE, now, failsafe_duration)
            return self._controlled(now)
        if self._limit_received and hb_ok:
            return self._controlled(now)
        if self.state == LimitationState.INIT and now - self.since >= self.heartbeat_timeout:
            return self._set(LimitationState.FAILSAFE, now, failsafe_duration)
        if (self.state == LimitationState.FAILSAFE and self.failsafe_until is not None
                and now >= self.failsafe_until):
            return self._set(LimitationState.UNLIMITED_AUTONOMOUS, now)
        return False


# --- CS-LPC / CS-LPP ---------------------------------------------------------------------------


class CsPowerLimitation(UseCase):
    """Common Controllable System side of LPC and LPP (eebus-go cs/lpc and cs/lpp are identical
    except for the direction, the failsafe key and the nominal max characteristic).

    Server features: LoadControl (active power limit), DeviceConfiguration
    (failsafe limit and duration, the duration is shared by LPC and LPP),
    DeviceDiagnosis (own heartbeat) and ElectricalConnection (nominal maximum);
    client feature DeviceDiagnosis for the Energy Guard's heartbeat.

    Incoming writes go through ``approve_limit`` / ``approve_configuration``
    (synchronously, via :attr:`LocalFeature.write_approval`); without them every
    valid write is accepted. Writes to a value that is not changeable and
    failsafe durations outside 2-24 h are always denied.

    Each use case runs its own :class:`LimitationStateMachine` with the shared heartbeat.
    """

    actor = "ControllableSystem"
    version = "1.0.0"
    scenarios = (Scenario(1, True), Scenario(2, True), Scenario(3, True, (DEVICE_DIAGNOSIS,)),
                 Scenario(4, True))
    valid_actors = ("EnergyGuard",)
    valid_entity_types = ("GridGuard", "CEM")
    client_features = (DEVICE_DIAGNOSIS,)

    DIRECTION = ""
    KEY_FAILSAFE_POWER = ""
    CHARACTERISTIC_HEMS = ""
    CHARACTERISTIC_DEVICE = ""

    # eebus-go events
    DATA_UPDATE_LIMIT = "DataUpdateLimit"
    DATA_UPDATE_FAILSAFE_POWER = ""
    DATA_UPDATE_FAILSAFE_DURATION = "DataUpdateFailsafeDurationMinimum"
    DATA_UPDATE_HEARTBEAT = "DataUpdateHeartbeat"
    # pyeebus events (state machine)
    STATE_CHANGED = "StateChanged"
    HEARTBEAT_TIMEOUT = "HeartbeatTimeout"

    @property
    def limit_filter(self) -> dict[str, str]:
        return {"limitType": "signDependentAbsValueLimit", "limitCategory": "obligation",
                "limitDirection": self.DIRECTION, "scopeType": "activePowerLimit"}

    def __init__(self, local_entity, on_event: UseCaseEventCallback | None = None, *,
                 approve_limit: LimitApproval | None = None,
                 approve_configuration: ConfigurationApproval | None = None,
                 heartbeat_timeout: float = HEARTBEAT_TIMEOUT_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(local_entity, on_event)
        self.approve_limit = approve_limit
        self.approve_configuration = approve_configuration
        self.clock = clock
        self.machine = LimitationStateMachine(heartbeat_timeout, clock())
        self._heartbeat_feature: RemoteFeature | None = None
        self._several_guards = False  # KEO workaround: several entities with DeviceDiagnosis
        self._guard: tuple[str, RemoteEntity | None] = ("", None)
        self._written: dict[str, Any] | None = None  # limit item of the write being processed
        self._written_keys: set[str] = set()  # failsafe keys of the write being processed
        self._watchdog: asyncio.Task | None = None
        self._heartbeat_was_ok = False

    # --- setup -----------------------------------------------------------------------------

    def add_features(self) -> None:
        super().add_features()
        local = self.local

        lc = local.add_feature(LOAD_CONTROL, Role.SERVER)
        lc.add_function(FN_LIMIT_DESCRIPTIONS, read=True)
        lc.add_function(FN_LIMITS, read=True, write=True)
        add_write_approval(lc, self._approve_limit_write)
        descs = as_list((lc.data.get(FN_LIMIT_DESCRIPTIONS) or {}).get("loadControlLimitDescriptionData"))
        if not filter_items(descs, self.limit_filter):
            limit_id = next_id(descs, "limitId")
            descs.append({"limitId": limit_id, **self.limit_filter,
                          "measurementId": self._power_total_measurement_id(), "unit": "W"})
            lc.set_data(FN_LIMIT_DESCRIPTIONS, {"loadControlLimitDescriptionData": descs})
            self._update_limit_item({"limitId": limit_id, "isLimitChangeable": True,
                                     "isLimitActive": False, "value": scaled_number(0)})

        dc = local.add_feature(DEVICE_CONFIGURATION, Role.SERVER)
        dc.add_function(FN_CONFIG_DESCRIPTIONS, read=True)
        dc.add_function(FN_CONFIG_VALUES, read=True, write=True)
        add_write_approval(dc, self._approve_configuration_write)
        descs = as_list((dc.data.get(FN_CONFIG_DESCRIPTIONS) or {}).get(
            "deviceConfigurationKeyValueDescriptionData"))
        for key, value_type, initial in ((self.KEY_FAILSAFE_POWER, "scaledNumber", scaled_number(0)),
                                         (KEY_FAILSAFE_DURATION, "duration", format_duration(0))):
            if filter_items(descs, keyName=key):
                continue
            desc = {"keyId": next_id(descs, "keyId"), "keyName": key, "valueType": value_type}
            if key == self.KEY_FAILSAFE_POWER:
                desc["unit"] = "W"
            descs.append(desc)
            dc.set_data(FN_CONFIG_DESCRIPTIONS, {"deviceConfigurationKeyValueDescriptionData": descs})
            self._update_config_item(key, {value_type: initial}, True)

        dd = local.add_feature(DEVICE_DIAGNOSIS, Role.SERVER)
        dd.add_function(FN_HEARTBEAT, read=True)

        ec = local.add_feature(ELECTRICAL_CONNECTION, Role.SERVER)
        ec.add_function(FN_EC_CHARACTERISTICS, read=True)
        chars = as_list((ec.data.get(FN_EC_CHARACTERISTICS) or {}).get("electricalConnectionCharacteristicData"))
        if not filter_items(chars, characteristicContext="entity", characteristicType=self._characteristic_type()):
            measurement_id = self._power_total_measurement_id()
            connection_id = self._electrical_connection_id()
            chars.append({"electricalConnectionId": connection_id,
                          "parameterId": self._power_total_parameter_id(connection_id, measurement_id),
                          "characteristicId": next_id(chars, "characteristicId"),
                          "characteristicContext": "entity", "characteristicType": self._characteristic_type(),
                          "unit": "W"})
            ec.set_data(FN_EC_CHARACTERISTICS, {"electricalConnectionCharacteristicData": chars})

    def setup(self) -> CsPowerLimitation:
        super().setup()
        with contextlib.suppress(RuntimeError):
            self._start_watchdog()
        return self

    def close(self) -> None:
        """Stop the state machine watchdog."""
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    # --- local feature helpers (eebus-go features/server) -----------------------------------

    def _server(self, feature_type: str) -> LocalFeature:
        feature = self.local.feature(feature_type, Role.SERVER)
        if feature is None:
            raise DataNotAvailable(f"local {feature_type} server not found")
        return feature

    def _local_items(self, feature_type: str, function: str, list_field: str) -> list[dict[str, Any]]:
        feature = self.local.feature(feature_type, Role.SERVER)
        if feature is None:
            return []
        return as_list((feature.data.get(function) or {}).get(list_field))

    def _power_total_measurement_id(self) -> int:
        descs = filter_items(self._local_items(MEASUREMENT, "measurementDescriptionListData",
                                               "measurementDescriptionData"),
                             measurementType="power", commodityType="electricity", unit="W",
                             scopeType="acPowerTotal")
        if len(descs) == 1 and descs[0].get("measurementId") is not None:
            return descs[0]["measurementId"]
        return 50  # eebus-go's default

    def _electrical_connection_id(self) -> int:
        descs = filter_items(self._local_items(ELECTRICAL_CONNECTION, "electricalConnectionDescriptionListData",
                                               "electricalConnectionDescriptionData"),
                             powerSupplyType="ac", positiveEnergyDirection="consume")
        if len(descs) == 1 and descs[0].get("electricalConnectionId") is not None:
            return descs[0]["electricalConnectionId"]
        return 0

    def _power_total_parameter_id(self, connection_id: int, measurement_id: int) -> int:
        params = filter_items(self._local_items(ELECTRICAL_CONNECTION,
                                                "electricalConnectionParameterDescriptionListData",
                                                "electricalConnectionParameterDescriptionData"),
                              electricalConnectionId=connection_id, measurementId=measurement_id,
                              voltageType="ac", acMeasurementType="real")
        if len(params) == 1 and params[0].get("parameterId") is not None:
            return params[0]["parameterId"]
        return 0

    def _characteristic_type(self) -> str:
        # LPC/LPP 1.0 2.2: a HEMS provides the contractual nominal max, other devices their own
        if self.local.device.device_type in (None, "EnergyManagementSystem"):
            return self.CHARACTERISTIC_HEMS
        return self.CHARACTERISTIC_DEVICE

    def _limit_id(self) -> int:
        descs = filter_items(self._local_items(LOAD_CONTROL, FN_LIMIT_DESCRIPTIONS,
                                               "loadControlLimitDescriptionData"), self.limit_filter)
        if len(descs) != 1 or descs[0].get("limitId") is None:
            raise DataNotAvailable("no active power limit description")
        return descs[0]["limitId"]

    def _limit_item(self) -> dict[str, Any] | None:
        limit_id = self._limit_id()
        return next((i for i in self._local_items(LOAD_CONTROL, FN_LIMITS, "loadControlLimitData")
                     if i.get("limitId") == limit_id), None)

    def _update_limit_item(self, item: dict[str, Any], delete: tuple[str, ...] = ()) -> None:
        lc = self._server(LOAD_CONTROL)
        items = [dict(i) for i in as_list((lc.data.get(FN_LIMITS) or {}).get("loadControlLimitData"))]
        current = next((i for i in items if i.get("limitId") == item["limitId"]), None)
        if current is None:
            items.append(dict(item))
        else:
            current.update(item)
            for key in delete:
                current.pop(key, None)
        lc.set_data(FN_LIMITS, {"loadControlLimitData": sorted(items, key=lambda i: i.get("limitId", 0))})

    def _config_key(self, key: str) -> dict[str, Any]:
        descs = filter_items(self._local_items(DEVICE_CONFIGURATION, FN_CONFIG_DESCRIPTIONS,
                                               "deviceConfigurationKeyValueDescriptionData"), keyName=key)
        if len(descs) != 1 or descs[0].get("keyId") is None:
            raise DataNotAvailable(f"no {key} description")
        return descs[0]

    def _config_item(self, key: str) -> dict[str, Any] | None:
        key_id = self._config_key(key)["keyId"]
        return next((i for i in self._local_items(DEVICE_CONFIGURATION, FN_CONFIG_VALUES,
                                                  "deviceConfigurationKeyValueData")
                     if i.get("keyId") == key_id), None)

    def _update_config_item(self, key: str, value: dict[str, Any], changeable: bool) -> None:
        dc = self._server(DEVICE_CONFIGURATION)
        key_id = self._config_key(key)["keyId"]
        items = [dict(i) for i in as_list((dc.data.get(FN_CONFIG_VALUES) or {}).get(
            "deviceConfigurationKeyValueData")) if i.get("keyId") != key_id]
        items.append({"keyId": key_id, "value": value, "isValueChangeable": changeable})
        dc.set_data(FN_CONFIG_VALUES, {"deviceConfigurationKeyValueData":
                                       sorted(items, key=lambda i: i.get("keyId", 0))})

    # --- scenario 1: limit -------------------------------------------------------------------

    def limit(self) -> LoadLimit:
        """The current active power consumption limit (as last written or set)."""
        item = self._limit_item()
        if item is None or scaled_value(item.get("value")) is None:
            raise DataNotAvailable("no limit value")
        limit = LoadLimit(scaled_value(item["value"]), bool(item.get("isLimitActive")),
                          bool(item.get("isLimitChangeable")))
        end = (item.get("timePeriod") or {}).get("endTime")
        if end:
            with contextlib.suppress(ValueError):
                limit.duration = parse_duration(end)
                if limit.duration == 0:
                    limit.is_active = False  # LPC implementation guideline 2.2
        return limit

    def set_limit(self, limit: LoadLimit) -> None:
        """Set the limit data, e.g. the start values or a locally changed limit.

        Does not drive the state machine: only limits from the Energy Guard do.
        """
        item: dict[str, Any] = {"limitId": self._limit_id(), "isLimitChangeable": limit.is_changeable,
                                "isLimitActive": limit.is_active, "value": scaled_number(limit.value)}
        delete: tuple[str, ...] = ("timePeriod",)
        if limit.duration and limit.duration > 0:
            item["timePeriod"] = {"endTime": format_duration(limit.duration)}
            delete = ()
        self._update_limit_item(item, delete)

    @staticmethod
    def _load_limit(item: dict[str, Any]) -> LoadLimit:
        limit = LoadLimit(scaled_value(item.get("value")) or 0.0, bool(item.get("isLimitActive")))
        end = (item.get("timePeriod") or {}).get("endTime")
        if end:
            with contextlib.suppress(ValueError):
                limit.duration = parse_duration(end)
        return limit

    def _approve_limit_write(self, msg: Message) -> SpineError | None:
        try:
            limit_id = self._limit_id()
        except DataNotAvailable:
            return None
        items = [i for i in as_list((msg.data or {}).get("loadControlLimitData")) if i.get("limitId") == limit_id]
        if not items:
            return None  # not for this use case
        item = items[-1]
        current = self._limit_item() or {}
        if current.get("isLimitChangeable") is False:
            return SpineError(ErrorNumber.COMMAND_REJECTED, "limit is not changeable")
        limit = self._load_limit(item)
        if "value" in item and (scaled_value(item.get("value")) is None or limit.value < 0):
            return SpineError(ErrorNumber.COMMAND_REJECTED, "invalid limit value")
        if "value" not in item:
            limit.value = scaled_value(current.get("value")) or 0.0
        if self.approve_limit is not None:
            try:
                reason = self.approve_limit(limit)
            except Exception:
                _LOGGER.exception("limit approval failed")
                reason = "approval failed"
            if reason is not None:
                return SpineError(ErrorNumber.COMMAND_REJECTED, reason or "denied")
        self._written = item
        return None

    # --- scenario 2: failsafe values ----------------------------------------------------------

    def failsafe_limit(self) -> tuple[float, bool]:
        """(failsafe active power limit in W, changeable by the Energy Guard)."""
        item = self._config_item(self.KEY_FAILSAFE_POWER) or {}
        value = scaled_value((item.get("value") or {}).get("scaledNumber"))
        if value is None:
            raise DataNotAvailable("no failsafe limit")
        return value, bool(item.get("isValueChangeable"))

    def set_failsafe_limit(self, watts: float, changeable: bool = True) -> None:
        self._update_config_item(self.KEY_FAILSAFE_POWER, {"scaledNumber": scaled_number(watts)}, changeable)

    def failsafe_duration_minimum(self) -> tuple[float, bool]:
        """(failsafe duration minimum in s, changeable by the Energy Guard)."""
        item = self._config_item(KEY_FAILSAFE_DURATION) or {}
        duration = (item.get("value") or {}).get("duration")
        if not duration:
            raise DataNotAvailable("no failsafe duration")
        return parse_duration(duration), bool(item.get("isValueChangeable"))

    def set_failsafe_duration_minimum(self, seconds: float, changeable: bool = True) -> None:
        if not FAILSAFE_DURATION_MIN <= seconds <= FAILSAFE_DURATION_MAX:
            raise ValueError("failsafe duration must be between 2 and 24 hours")
        self._update_config_item(KEY_FAILSAFE_DURATION, {"duration": format_duration(seconds)}, changeable)

    def _approve_configuration_write(self, msg: Message) -> SpineError | None:
        values: dict[str, float] = {}
        for item in as_list((msg.data or {}).get("deviceConfigurationKeyValueData")):
            for key in (self.KEY_FAILSAFE_POWER, KEY_FAILSAFE_DURATION):
                try:
                    desc = self._config_key(key)
                except DataNotAvailable:
                    continue
                if item.get("keyId") != desc["keyId"]:
                    continue
                current = self._config_item(key) or {}
                if current.get("isValueChangeable") is False:
                    return SpineError(ErrorNumber.COMMAND_REJECTED, f"{key} is not changeable")
                value = item.get("value") or {}
                if key == self.KEY_FAILSAFE_POWER:
                    watts = scaled_value(value.get("scaledNumber"))
                    if watts is None or watts < 0:
                        return SpineError(ErrorNumber.COMMAND_REJECTED, "invalid failsafe limit")
                    values[key] = watts
                else:
                    try:
                        seconds = parse_duration(value.get("duration") or "")
                    except ValueError:
                        return SpineError(ErrorNumber.COMMAND_REJECTED, "invalid failsafe duration")
                    if not FAILSAFE_DURATION_MIN <= seconds <= FAILSAFE_DURATION_MAX:
                        return SpineError(ErrorNumber.COMMAND_REJECTED,
                                          "failsafe duration must be between 2 and 24 hours")
                    values[key] = seconds
        if values and self.approve_configuration is not None:
            try:
                reason = self.approve_configuration(values)
            except Exception:
                _LOGGER.exception("configuration approval failed")
                reason = "approval failed"
            if reason is not None:
                return SpineError(ErrorNumber.COMMAND_REJECTED, reason or "denied")
        self._written_keys = set(values)
        return None

    # --- scenario 3: heartbeat ----------------------------------------------------------------

    def start_heartbeat(self) -> None:
        feature = self._server(DEVICE_DIAGNOSIS)
        self.local.start_heartbeat(feature)

    def stop_heartbeat(self) -> None:
        self.local.stop_heartbeat()

    def is_heartbeat_within_duration(self) -> bool:
        """True if the Energy Guard's last heartbeat arrived within the heartbeat timeout (120 s).

        Unlike eebus-go this uses the local receive time, not the guard's timestamp,
        so clock differences between the devices do not matter.
        """
        return self.machine.heartbeat_ok(self.clock())

    # --- scenario 4: nominal max --------------------------------------------------------------

    def _characteristic(self) -> dict[str, Any]:
        chars = filter_items(self._local_items(ELECTRICAL_CONNECTION, FN_EC_CHARACTERISTICS,
                                               "electricalConnectionCharacteristicData"),
                             characteristicContext="entity", characteristicType=self._characteristic_type())
        if not chars or chars[0].get("characteristicId") is None:
            raise DataNotAvailable("no nominal max characteristic")
        return chars[0]

    def nominal_max(self) -> float:
        value = scaled_value(self._characteristic().get("value"))
        if value is None:
            raise DataNotAvailable("no nominal max")
        return value

    def set_nominal_max(self, watts: float) -> None:
        char = self._characteristic()
        ec = self._server(ELECTRICAL_CONNECTION)
        items = [dict(i) for i in as_list((ec.data.get(FN_EC_CHARACTERISTICS) or {}).get(
            "electricalConnectionCharacteristicData"))]
        for item in items:
            if item.get("characteristicId") == char["characteristicId"]:
                item["value"] = scaled_number(watts)
        ec.set_data(FN_EC_CHARACTERISTICS, {"electricalConnectionCharacteristicData": items})

    # --- state machine ------------------------------------------------------------------------

    @property
    def state(self) -> str:
        """The current :class:`LimitationState`."""
        return self.machine.state

    def effective_limit(self) -> float | None:
        """The active power limit (W) the device has to keep now; None: no limit.

        init and failsafe: the failsafe limit; limited: the written limit.
        """
        if self.machine.state in (LimitationState.INIT, LimitationState.FAILSAFE):
            try:
                return self.failsafe_limit()[0]
            except DataNotAvailable:
                return 0.0
        if self.machine.state == LimitationState.LIMITED:
            return self.machine.limit_value
        return None

    def _failsafe_duration(self) -> float:
        # not set (PT0S) or invalid: stay in failsafe for at least the LPC minimum of 2 h
        try:
            return max(self.failsafe_duration_minimum()[0], FAILSAFE_DURATION_MIN)
        except DataNotAvailable:
            return FAILSAFE_DURATION_MIN

    def tick(self) -> None:
        """Apply heartbeat and duration timeouts (called every second by the watchdog)."""
        now = self.clock()
        before = self.machine.state
        changed = self.machine.update(now, self._failsafe_duration())
        hb_ok = self.machine.heartbeat_ok(now)
        if self._heartbeat_was_ok and not hb_ok:
            self._emit(*self._guard, self.HEARTBEAT_TIMEOUT)
        self._heartbeat_was_ok = hb_ok
        if changed:
            self._state_changed(before)

    def _state_changed(self, before: str) -> None:
        _LOGGER.info("%s state %s -> %s (limit %s)", self.name, before, self.machine.state, self.effective_limit())
        if before == LimitationState.LIMITED and self.machine.state == LimitationState.UNLIMITED_CONTROLLED:
            # the duration ran out: show the guard that the limit is no longer active
            with contextlib.suppress(DataNotAvailable):
                item = self._limit_item()
                if item is not None and item.get("isLimitActive"):
                    self._update_limit_item({"limitId": item["limitId"], "isLimitActive": False},
                                            ("timePeriod",))
        self._emit(*self._guard, self.STATE_CHANGED)

    def _start_watchdog(self) -> None:
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = spawn(self._watchdog_loop())

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                self.tick()
            except Exception:
                _LOGGER.exception("%s watchdog failed", self.name)

    # --- events ---------------------------------------------------------------------------------

    def handle_event(self, event: Event) -> None:
        with contextlib.suppress(RuntimeError):
            self._start_watchdog()
        super().handle_event(event)
        if event.type == EventType.DEVICE and event.change == Change.ADD and event.device is not None:
            self._device_connected(event)
        elif event.type == EventType.DEVICE and event.change == Change.REMOVE:
            if self._heartbeat_feature is not None and self._heartbeat_feature.device is event.device:
                self._heartbeat_feature = None
        elif (event.type == EventType.BINDING and event.change == Change.ADD and event.device is not None
              and self._several_guards):
            self._binding_added(event)

    def _device_connected(self, event: Event) -> None:
        entities = [e for e in event.device.entities if self.is_compatible(e)
                    and e.feature(DEVICE_DIAGNOSIS, Role.SERVER) is not None]
        if len(entities) == 1:
            self._subscribe_heartbeat(entities[0])
        elif len(entities) > 1:
            # KEO: several identical entities; use the one that binds to our LoadControl server
            self._several_guards = True

    def _binding_added(self, event: Event) -> None:
        data = event.data or {}
        client, server = data.get("clientAddress"), data.get("serverAddress")
        lc = self.local.feature(LOAD_CONTROL, Role.SERVER)
        if not isinstance(client, Address) or lc is None or server != lc.address:
            return
        entity = event.device.entity(client.entity)
        if self.is_compatible(entity):
            self._subscribe_heartbeat(entity)

    def _subscribe_heartbeat(self, entity: RemoteEntity) -> None:
        f = ClientFeature.find(DEVICE_DIAGNOSIS, self.local, entity)
        if f is None:
            return
        self._heartbeat_feature = f.remote
        self._guard = (entity.device.ski, entity)
        f.subscribe()
        f.request(FN_HEARTBEAT)

    def handle_entity_event(self, event: Event) -> None:
        if (self.is_data_update(event, FN_HEARTBEAT) and event.classifier == CmdClassifier.NOTIFY
                and event.local_feature is not None and event.local_feature.role == Role.CLIENT):
            if self._heartbeat_feature is not None and event.feature is not self._heartbeat_feature:
                return
            self._guard = (event.ski, event.entity)
            self._emit(event.ski, event.entity, self.DATA_UPDATE_HEARTBEAT)
            before = self.machine.state
            if self.machine.heartbeat(self.clock(), self._failsafe_duration()):
                self._state_changed(before)
            self._heartbeat_was_ok = True
            return
        if event.type != EventType.DATA or event.classifier != CmdClassifier.WRITE:
            return
        if event.local_feature is self.local.feature(LOAD_CONTROL, Role.SERVER) and event.function == FN_LIMITS:
            self._limit_written(event)
        elif (event.local_feature is self.local.feature(DEVICE_CONFIGURATION, Role.SERVER)
              and event.function == FN_CONFIG_VALUES):
            self._configuration_written(event)

    def _limit_written(self, event: Event) -> None:
        written, self._written = self._written, None
        if written is None:
            return
        self._guard = (event.ski, event.entity)
        if "timePeriod" not in written:
            # a new limit without duration: drop a duration left over from an earlier write
            item = self._limit_item()
            if item is not None and "timePeriod" in item:
                self._update_limit_item({"limitId": item["limitId"]}, ("timePeriod",))
        self._emit(event.ski, event.entity, self.DATA_UPDATE_LIMIT)
        limit = self.limit()
        before = self.machine.state
        if self.machine.limit(self.clock(), limit.is_active, limit.value, limit.duration,
                              self._failsafe_duration()):
            self._state_changed(before)

    def _configuration_written(self, event: Event) -> None:
        written, self._written_keys = self._written_keys, set()
        if self.KEY_FAILSAFE_POWER in written:
            self._emit(event.ski, event.entity, self.DATA_UPDATE_FAILSAFE_POWER)
        if KEY_FAILSAFE_DURATION in written:
            self._emit(event.ski, event.entity, self.DATA_UPDATE_FAILSAFE_DURATION)


class CsLPC(CsPowerLimitation):
    """Limitation of power consumption (LPC), Controllable System actor (eebus-go cs/lpc)."""

    name = "limitationOfPowerConsumption"
    DIRECTION = "consume"
    KEY_FAILSAFE_POWER = KEY_FAILSAFE_CONSUMPTION
    CHARACTERISTIC_HEMS = "contractualConsumptionNominalMax"
    CHARACTERISTIC_DEVICE = "powerConsumptionNominalMax"
    DATA_UPDATE_FAILSAFE_POWER = "DataUpdateFailsafeConsumptionActivePowerLimit"

    consumption_limit = CsPowerLimitation.limit
    set_consumption_limit = CsPowerLimitation.set_limit
    failsafe_consumption_limit = CsPowerLimitation.failsafe_limit
    set_failsafe_consumption_limit = CsPowerLimitation.set_failsafe_limit
    consumption_nominal_max = CsPowerLimitation.nominal_max
    set_consumption_nominal_max = CsPowerLimitation.set_nominal_max


class CsLPP(CsPowerLimitation):
    """Limitation of power production (LPP, e.g. feed-in limit), Controllable System actor
    (eebus-go cs/lpp). Limits are positive values in W of produced power."""

    name = "limitationOfPowerProduction"
    DIRECTION = "produce"
    KEY_FAILSAFE_POWER = KEY_FAILSAFE_PRODUCTION
    CHARACTERISTIC_HEMS = "contractualProductionNominalMax"
    CHARACTERISTIC_DEVICE = "powerProductionNominalMax"
    DATA_UPDATE_FAILSAFE_POWER = "DataUpdateFailsafeProductionActivePowerLimit"

    production_limit = CsPowerLimitation.limit
    set_production_limit = CsPowerLimitation.set_limit
    failsafe_production_limit = CsPowerLimitation.failsafe_limit
    set_failsafe_production_limit = CsPowerLimitation.set_failsafe_limit
    production_nominal_max = CsPowerLimitation.nominal_max
    set_production_nominal_max = CsPowerLimitation.set_nominal_max


# names of the first version (CS-LPC only)
LPCState = LimitationState
LPCStateMachine = LimitationStateMachine
