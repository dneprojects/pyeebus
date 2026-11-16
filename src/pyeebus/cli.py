"""pyeebus command line: discover EEBUS devices, connect as an energy manager, simulate an EVSE."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .probe import ProbeFiles
from .service import EebusService
from .ship import Identity, RemoteAbortError, State, TrustStore, is_ski_valid, normalize_ski
from .ship.mdns import ShipMdns, ShipService
from .spine import Change, Event, EventType, RemoteDevice, RemoteEntity, Role, scaled_number, spawn
from .spine.device import partial_filter
from .usecases import (
    ALL_EV_USE_CASES,
    EVCC,
    EVCEM,
    EVSECC,
    LPC,
    MPC,
    OPEV,
    OSCEV,
    ClientFeature,
    DataNotAvailable,
    LoadLimit,
    UseCase,
)

CONFIG_DIR = Path.home() / ".config" / "pyeebus"

# client features of the read-only probe: everything a device may serve
PROBE_FEATURES = (
    "Alarm", "Bill", "DeviceClassification", "DeviceConfiguration", "DeviceDiagnosis", "DirectControl",
    "ElectricalConnection", "HVAC", "Identification", "IncentiveTable", "LoadControl", "Measurement",
    "Messaging", "PowerSequences", "Sensing", "Setpoint", "SmartEnergyManagementPs", "TariffInformation",
    "Threshold", "TimeInformation", "TimeSeries", "TimeTable")


def interface_address(name: str) -> str:
    """The IPv4 address of a network interface (e.g. ``ovs_eth0`` on a Synology NAS)."""
    import ifaddr

    for adapter in ifaddr.get_adapters():
        if name in (adapter.name, adapter.nice_name):
            for ip in adapter.ips:
                if isinstance(ip.ip, str):
                    return ip.ip
    raise SystemExit(f"interface {name!r} not found or without IPv4 address; available: "
                     + ", ".join(a.nice_name for a in ifaddr.get_adapters()))


def _zeroconf(interface: str | None):
    """mDNS bound to one interface (and announcing only its address), or None for all."""
    if not interface:
        return None
    from zeroconf.asyncio import AsyncZeroconf

    address = interface_address(interface)
    os.environ["EEBUS_ANNOUNCE_IP"] = address
    print(f"Interface {interface}: {address}")
    return AsyncZeroconf(interfaces=[address])


def _print_service(s: ShipService) -> None:
    print(f"- {s.brand} {s.model} ({s.device_type})".strip())
    print(f"    SKI      {s.ski}")
    print(f"    SHIP id  {s.ship_id}")
    print(f"    address  {', '.join(s.addresses) or s.host}:{s.port}{s.path}")
    print(f"    register {s.register}")


async def discover(seconds: float, interface: str | None = None) -> int:
    mdns = ShipMdns(_zeroconf(interface))
    await mdns.browse()
    print(f"Looking for EEBUS devices for {seconds:g} s ...")
    await asyncio.sleep(seconds)
    await mdns.close()
    if not mdns.services:
        print("No EEBUS (SHIP) devices found. Same network? mDNS allowed?")
        return 1
    for service in mdns.services.values():
        _print_service(service)
    return 0


# --- connect -------------------------------------------------------------------------------


def _entity_line(entity: RemoteEntity) -> str:
    addr = ".".join(str(e) for e in entity.entity)
    features = []
    for f in entity.features:
        fns = ", ".join(
            f"{fn}{'(rw)' if ops.write else ''}" for fn, ops in f.operations.items())
        features.append(f"      {f.id:>2} {f.type} {f.role}" + (f": {fns}" if fns else ""))
    return f"  [{addr}] {entity.type}" + (f" - {entity.description}" if entity.description else "") + (
        "\n" + "\n".join(features) if features else "")


def _device_text(device: RemoteDevice) -> str:
    lines = [f"SPINE device {device.address} ({device.device_type}, feature set {device.feature_set})"]
    lines += [_entity_line(entity) for entity in device.entities]
    return "\n".join(lines)


def _print_device(device: RemoteDevice) -> None:
    print("\n" + _device_text(device))


def _use_cases_text(device: RemoteDevice) -> str:
    lines = ["Use cases announced by the device:"]
    for info in device.use_cases():
        addr = ".".join(str(e) for e in (info.get("address") or {}).get("entity", []))
        for support in info.get("useCaseSupport", []):
            available = "" if support.get("useCaseAvailable", True) else " (not available)"
            lines.append(f"  {info.get('actor')} [{addr}] {support.get('useCaseName')} "
                         f"{support.get('useCaseVersion', '')} scenarios {support.get('scenarioSupport')}{available}")
    return "\n".join(lines)


def _value(fn, *args) -> Any:
    try:
        return fn(*args)
    except (DataNotAvailable, KeyError, IndexError, TypeError):
        return None


def _status(ucs: dict[str, UseCase], entity: RemoteEntity) -> str:
    parts = []
    if entity.type == "EVSE":
        evsecc: EVSECC = ucs["EVSECC"]
        if (m := _value(evsecc.manufacturer_data, entity)) is not None:
            parts.append(f"{m.get('brandName', '')} {m.get('deviceName', '')} "
                         f"sw {m.get('softwareRevision', '?')}".strip())
        if (s := _value(evsecc.operating_state, entity)) is not None:
            parts.append(f"state {s[0]}" + (f" error {s[1]}" if s[1] else ""))
    if entity.type != "EV":
        lpc: LPC = ucs["LPC"]
        mpc: MPC = ucs["MPC"]
        if (v := _value(lpc.consumption_limit, entity)) is not None:
            parts.append(f"LPC limit {v.value:g} W{'' if v.is_active else ' (inactive)'}"
                         + (f" for {v.duration:g} s" if v.duration else ""))
        if (v := _value(lpc.failsafe_consumption_limit, entity)) is not None:
            parts.append(f"failsafe {v:g} W")
        if (v := _value(lpc.failsafe_duration_minimum, entity)) is not None:
            parts.append(f"failsafe duration {v / 3600:g} h")
        if (v := _value(lpc.consumption_nominal_max, entity)) is not None:
            parts.append(f"nominal max {v:g} W")
        if (v := _value(mpc.power, entity)) is not None:
            parts.append(f"power {v:g} W")
        if (v := _value(mpc.current_per_phase, entity)) is not None:
            parts.append(f"current {v} A")
        if (v := _value(mpc.voltage_per_phase, entity)) is not None:
            parts.append(f"voltage {v} V")
        if (v := _value(mpc.energy_consumed, entity)) is not None:
            parts.append(f"energy {v:g} Wh")
    elif entity.type == "EV":
        evcc: EVCC = ucs["EVCC"]
        evcem: EVCEM = ucs["EVCEM"]
        opev: OPEV = ucs["OPEV"]
        oscev: OSCEV = ucs["OSCEV"]
        parts.append(f"charge state {evcc.charge_state(entity)}")
        if (v := _value(evcc.communication_standard, entity)) and v != "unknown":
            parts.append(v)
        if (v := _value(evcem.current_per_phase, entity)) is not None:
            parts.append(f"current {v} A")
        if (v := _value(evcem.power_per_phase, entity)) is not None:
            parts.append(f"power {v} W")
        if (v := _value(evcem.energy_charged, entity)) is not None:
            parts.append(f"energy {v} Wh")
        if (v := _value(opev.current_limits, entity)) is not None:
            parts.append(f"current range min {v[0]} max {v[1]} A")
        if (v := _value(opev.load_control_limits, entity)) is not None:
            parts.append("OPEV limits " + ", ".join(
                f"{lim.value:g}{'' if lim.is_active else ' (inactive)'}" for lim in v))
        if (v := _value(oscev.load_control_limits, entity)) is not None:
            parts.append("OSCEV limits " + ", ".join(
                f"{lim.value:g}{'' if lim.is_active else ' (inactive)'}" for lim in v))
    return "; ".join(parts)


async def connect(ski: str, host: str | None, port: int, local_port: int, config: Path,
                  raw: bool, read_all: bool = False, lpc_limit: float | None = None,
                  lpc_duration: float = 300, write_failsafe: float | None = None,
                  lpc_release: bool = False, read_only: bool = False, interface: str | None = None,
                  trace_file: Path | None = None, report: Path | None = None) -> int:
    read_all = read_all or read_only
    identity = Identity.load_or_create(config, "pyeebus")
    trust = TrustStore.load(config / "trust.json")
    zeroconf = _zeroconf(interface)
    node_options: dict[str, Any] = {"zeroconf": zeroconf} if zeroconf else {}
    service = EebusService(identity, brand="pyeebus", model="pyeebus-cli", serial=identity.ski[:8],
                           ship_id=f"pyeebus-{identity.ski[:8]}", port=local_port, trust=trust,
                           auto_connect=host is None, **node_options)
    cem = service.entities[0]
    last: dict[tuple, str] = {}
    files = None
    if trace_file or report:
        files = ProbeFiles(trace_file, report, identity.ski, ski,
                           f"read-only {read_only}, read-all {read_all}, interface {interface or 'all'}, "
                           f"local port {local_port}")

    def on_uc_event(_ski: str, entity: RemoteEntity | None, name: str) -> None:
        if entity is None or name == UseCase.USE_CASE_SUPPORT_UPDATE:
            return
        if ((lpc_limit is not None or lpc_release) and name == LPC.DATA_UPDATE_LIMIT
                and entity.device.ski not in limit_written):
            limit_written.add(entity.device.ski)
            spawn(write_limit(entity))
        if (write_failsafe is not None and name == LPC.DATA_UPDATE_FAILSAFE_POWER
                and ("failsafe", entity.device.ski) not in limit_written):
            limit_written.add(("failsafe", entity.device.ski))
            spawn(write_failsafe_limit(entity))
        line = _status(ucs, entity)
        key = (entity.device.ski, entity.entity)
        if line and line != "charge state unknown" and last.get(key) != line:
            last[key] = line
            print(f"  {entity.type} [{'.'.join(map(str, entity.entity))}]: {line}")

    if read_only:
        # no use cases (nothing the device could act on), no bindings, no writes: only
        # client features to read and subscribe whatever the device serves
        ucs: dict[str, UseCase] = {}
        for feature_type in PROBE_FEATURES:
            cem.add_feature(feature_type, Role.CLIENT)
    else:
        ucs = {cls.__name__: cls(cem, on_uc_event).setup() for cls in (*ALL_EV_USE_CASES, LPC, MPC)}
    if read_all:
        cem.add_feature("Bill", Role.CLIENT)
    printed: set[tuple] = set()
    limit_written: set = set()

    def read_everything(device: RemoteDevice) -> None:
        for entity in device.entities:  # incl. [0] (DeviceClassification), NodeManagement is special
            for feature in entity.features:
                local = cem.feature(feature.type, Role.CLIENT)
                if feature.role != Role.SERVER or local is None:
                    continue
                for fn, ops in feature.operations.items():
                    if ops.read:
                        local.read(feature, fn)
                if read_only and not local.has_subscription(feature):
                    spawn(subscribe(local, feature))

    async def subscribe(local, feature) -> None:
        try:
            await local.subscribe(feature)
        except Exception as err:  # noqa: BLE001
            print(f"  subscription to {feature.type} [{'.'.join(map(str, feature.entity.entity))}] "
                  f"failed: {err!r}")

    async def write_limit(entity: RemoteEntity) -> None:
        try:
            if lpc_release:
                await release_limit(entity)
                return
            await ucs["LPC"].write_consumption_limit(
                entity, LoadLimit(lpc_limit, True, duration=lpc_duration or None))
            print(f"  LPC limit {lpc_limit:g} W accepted")
        except Exception as err:  # noqa: BLE001
            print(f"  LPC write failed: {err!r}")

    async def release_limit(entity: RemoteEntity) -> None:
        """Try several ways to lift the limit; devices differ in what they accept."""
        lpc: LPC = ucs["LPC"]
        limit_id = lpc._limit_description(entity)["limitId"]
        feature = ClientFeature("LoadControl", cem, entity)
        try:
            nominal = lpc.consumption_nominal_max(entity)
        except DataNotAvailable:
            nominal = None
        attempts = [
            ("inactive", {"limitId": limit_id, "isLimitActive": False}),
            ("inactive, 0 W", {"limitId": limit_id, "isLimitActive": False, "value": scaled_number(0)}),
        ]
        if nominal:
            attempts.append((f"active at nominal max {nominal:g} W",
                             {"limitId": limit_id, "isLimitActive": True, "value": scaled_number(round(nominal))}))
        for label, item in attempts:
            try:
                await feature.write("loadControlLimitListData", {"loadControlLimitData": [item]},
                                    [partial_filter()])
            except Exception as err:  # noqa: BLE001
                print(f"  release ({label}) failed: {err!r}")
                continue
            print(f"  LPC limit released ({label})")
            return

    async def write_failsafe_limit(entity: RemoteEntity) -> None:
        try:
            await ucs["LPC"].write_failsafe_consumption_limit(entity, write_failsafe)
            print(f"  failsafe limit {write_failsafe:g} W accepted")
        except Exception as err:  # noqa: BLE001
            print(f"  failsafe limit {write_failsafe:g} W failed: {err!r}")

    def on_spine_event(event: Event) -> None:
        if (files and event.type == EventType.DATA and event.feature and event.classifier in ("reply", "notify")
                and event.function != "deviceDiagnosisHeartbeatData"):
            f = event.feature
            files.data_update(f"[{'.'.join(map(str, f.entity.entity))}]:{f.id} {f.type}", event.function or "",
                              event.classifier, event.data)
        if (read_only and event.type == EventType.DATA and event.classifier == "notify" and event.feature
                and event.function not in ("deviceDiagnosisHeartbeatData", "nodeManagementUseCaseData")):
            f = event.feature
            print(f"  notify [{'.'.join(map(str, f.entity.entity))}]:{f.id} {f.type} {event.function} = "
                  f"{json.dumps(event.data, separators=(',', ':'))}")
        if event.type == EventType.DATA and event.function == "nodeManagementUseCaseData" and event.device:
            text = _use_cases_text(event.device)
            if last.get(("use cases", event.device.ski)) != text:
                last[("use cases", event.device.ski)] = text
                print("\n" + text)
                if files:
                    files.set_use_cases(text)
        if event.type == EventType.DEVICE and event.change == Change.ADD and event.device:
            _print_device(event.device)
            if files:
                files.set_device(_device_text(event.device))
            if read_all:
                read_everything(event.device)
        elif (read_all and event.type == EventType.DATA and event.classifier == "reply" and event.feature
              and event.function != "deviceDiagnosisHeartbeatData"
              and (key := (event.feature.address, event.function)) not in printed):
            printed.add(key)
            f = event.feature
            print(f"  [{'.'.join(map(str, f.entity.entity))}]:{f.id} {f.type} {event.function} = "
                  f"{json.dumps(event.data, separators=(',', ':'))}")
        elif event.type == EventType.DEVICE and event.change == Change.REMOVE:
            print("SPINE device gone")
        elif event.type == EventType.ENTITY and event.entity and event.change == Change.ADD:
            if event.device and event.device.address and len(event.entity.entity) > 1:
                print("\nEntity added:\n" + _entity_line(event.entity))
                if files:
                    files.set_device(_device_text(event.device))
        elif event.type == EventType.ENTITY and event.entity and event.change == Change.REMOVE:
            print(f"\nEntity removed: {event.entity.type} {list(event.entity.entity)}")

    service.device.subscribe_events(on_spine_event)
    def trace(_ski: str, direction: str, payload: dict[str, Any]) -> None:
        if raw:
            print(f"{'<<' if direction == 'in' else '>>'} {json.dumps(payload, separators=(',', ':'))}")
        if files:
            files.trace(direction, payload)

    if raw or files:
        service.trace = trace

    print(f"Our SKI: {identity.ski}")
    print("In the device's app or web UI, trust the EEBUS device 'pyeebus' with this SKI "
          "(Elli: Connections > HEMS connection > found EEBUS devices > Pair; "
          "myVAILLANT: Options > Network settings > EEBUS).\n")

    def on_state(remote: str, state: State, err: Exception | None) -> None:
        if files and state != State.HELLO_WAITING_FOR_TRUST:
            files.ship_state(f"[{remote[:8]}] {state.value}" + (f": {err!r}" if err else ""))
        if state == State.HELLO_WAITING_FOR_TRUST:
            return
        if isinstance(err, RemoteAbortError):
            print(f"[{remote[:8]}] not paired yet: trust 'pyeebus' in the device's app, retrying ...")
            return
        if state in (State.COMPLETE, State.CLOSED, State.ERROR):
            print(f"[{remote[:8]}] SHIP {state.value}" + (f": {err}" if err else ""))

    service.node.on_state = on_state
    await service.start()
    service.trust(ski)
    try:
        if host:
            while True:
                delay = 5
                try:
                    conn = await service.node.connect(host, port, ski)
                    await conn.wait_closed()
                except RemoteAbortError:
                    delay = 15
                except Exception as err:  # noqa: BLE001
                    print(f"connection failed: {err}")
                await asyncio.sleep(delay)
        else:
            print("Waiting for the device via mDNS (Ctrl+C to stop) ...")
            await asyncio.Event().wait()
    finally:
        await service.stop()


# --- simulator -------------------------------------------------------------------------------


async def simulate(config: Path, port: int, trust_ski: str | None) -> int:
    from .simulator import SimulatedEVSE

    identity = Identity.load_or_create(config / "sim-evse", "pyeebus-sim-evse")
    trust = TrustStore.load(config / "sim-evse" / "trust.json")
    sim = SimulatedEVSE(identity, port=port, trust=trust)
    if trust_ski:
        sim.service.trust(trust_ski)
    await sim.start()
    print(f"Simulated EVSE running on port {sim.service.node.port}, SKI {identity.ski}")
    print("Commands: plug, unplug, charge <A> <Wh>, limits, quit")
    loop = asyncio.get_running_loop()
    while True:
        line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
        if not line or line == "quit":
            break
        cmd, *args = line.split()
        if cmd == "plug":
            sim.plug_in()
        elif cmd == "unplug":
            sim.unplug()
        elif cmd == "charge" and len(args) == 2:
            sim.set_charging(float(args[0]), float(args[1]))
        elif cmd == "limits":
            print(sim.limits)
    await sim.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyeebus", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("discover", help="list EEBUS devices on the network")
    sp.add_argument("--seconds", type=float, default=5)
    sp.add_argument("--interface", help="network interface for mDNS (e.g. ovs_eth0), default all")
    sp = sub.add_parser("connect", help="pair with a device and act as energy manager (CEM)")
    sp.add_argument("ski", help="SKI of the device (from 'discover' or its web UI)")
    sp.add_argument("--host", help="connect directly instead of waiting for mDNS")
    sp.add_argument("--port", type=int, default=4712)
    sp.add_argument("--local-port", type=int, default=4712)
    sp.add_argument("--config", type=Path, default=CONFIG_DIR)
    sp.add_argument("--raw", action="store_true", help="print all SPINE messages")
    sp.add_argument("--read-all", action="store_true", help="read and print all data the device offers")
    sp.add_argument("--lpc-limit", type=float, metavar="W",
                    help="test: set this power limit (LPC)")
    sp.add_argument("--lpc-duration", type=float, default=300, metavar="S",
                    help="duration of --lpc-limit in seconds, 0 = no end (default 300)")
    sp.add_argument("--lpc-release", action="store_true", help="deactivate the LPC limit")
    sp.add_argument("--write-failsafe", type=float, metavar="W",
                    help="test: write this failsafe power limit (LPC)")
    sp.add_argument("--read-only", action="store_true",
                    help="probe: announce no use cases, never bind or write; read and subscribe everything")
    sp.add_argument("--interface", help="network interface for mDNS (e.g. ovs_eth0), default all")
    sp.add_argument("--trace-file", type=Path, help="append every SPINE message (JSON lines) to this file")
    sp.add_argument("--report", type=Path, help="keep a text report (device, use cases, data) in this file")
    sp = sub.add_parser("simulate-evse", help="run a simulated wallbox (for development)")
    sp.add_argument("--port", type=int, default=4713)
    sp.add_argument("--trust", help="SKI of the energy manager to accept")
    sp.add_argument("--config", type=Path, default=CONFIG_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(message)s")
    try:
        if args.cmd == "discover":
            return asyncio.run(discover(args.seconds, args.interface))
        if args.cmd == "simulate-evse":
            return asyncio.run(simulate(args.config, args.port, args.trust))
        if not is_ski_valid(args.ski):
            parser.error("SKI must be 40 hex characters")
        if args.read_only and (args.lpc_limit is not None or args.lpc_release or args.write_failsafe is not None):
            parser.error("--read-only does not write: drop --lpc-limit, --lpc-release and --write-failsafe")
        return asyncio.run(connect(normalize_ski(args.ski), args.host, args.port,
                                   args.local_port, args.config, args.raw, args.read_all,
                                   args.lpc_limit, args.lpc_duration, args.write_failsafe,
                                   args.lpc_release, args.read_only, args.interface,
                                   args.trace_file, args.report))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
