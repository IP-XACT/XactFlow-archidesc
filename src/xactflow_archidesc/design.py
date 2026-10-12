"""design.py: the Design object a design description script builds."""

from __future__ import annotations

import inspect
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple, Union
from xml.sax.saxutils import quoteattr

import ipxact
from xactflow import Library

# Namespace of the vendor extensions this importer writes.
XACTFLOW_NAMESPACE = "https://github.com/IP-XACT/XactFlow"

# Instance names end up as HDL instance names, so they must be plain identifiers.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_Direction = ipxact.Direction
_Mode = ipxact.InterfaceMode

# The directions each wire port direction can be connected to by connect().
_CONNECTABLE = {
    _Direction.IN: {_Direction.OUT, _Direction.INOUT},
    _Direction.OUT: {_Direction.IN, _Direction.INOUT},
    _Direction.INOUT: {_Direction.IN, _Direction.OUT, _Direction.INOUT},
}

# The bus interface modes that can be connected together (IEEE 1685-2022 SCR 2.4, 2.5, 2.8, 2.9).
_MODE_PAIRS = {
    frozenset((_Mode.INITIATOR, _Mode.TARGET)),
    frozenset((_Mode.INITIATOR, _Mode.MIRRORED_INITIATOR)),
    frozenset((_Mode.MIRRORED_TARGET, _Mode.TARGET)),
    frozenset((_Mode.SYSTEM, _Mode.MIRRORED_SYSTEM)),
}

# connect_bus() picks among these modes by itself. Any other mode must be named with .bus(name).
_AUTO_MODES = {_Mode.INITIATOR, _Mode.TARGET}

# The modes that act as the target of a connection, where the address mapping goes.
_TARGET_SIDE_MODES = {_Mode.TARGET, _Mode.MIRRORED_INITIATOR}


def _find(items: Sequence[Any], name: str, owner: str, kind: str) -> Any:
    """Return the item called name, or raise ValueError listing the names that do exist."""
    for item in items:
        if item.name == name:
            return item
    available = ", ".join(item.name for item in items) or "none"
    raise ValueError(f"{owner} has no {kind} {name!r}. Available {kind}s: {available}")


class IPInstance:
    """A handle on one component instance"""

    def __init__(self, name: str, component: ipxact.Component) -> None:
        self.name = name
        self.component = component

    def port(self, name: str) -> PortRef:
        ports = self.component.model.ports if self.component.model is not None else []
        port = _find(ports, name, repr(self), "port")
        return PortRef(self, port.name, port)

    def bus(self, name: str) -> BusInterfaceRef:
        bus_interface = _find(self.component.bus_interfaces, name, repr(self), "bus interface")
        return BusInterfaceRef(self, bus_interface.name, bus_interface)

    def __getattr__(self, name: str) -> PortRef:
        # Python only calls this for names that are not real attributes. Names starting with "_"
        # are Python's own lookups, so they stay normal missing attributes.
        if name.startswith("_"):
            raise AttributeError(name)
        return self.port(name)

    def __repr__(self) -> str:
        return f"IPInstance({self.name!r}, {self.component.vlnv})"


# Two refs are equal when they point at the same instance and name. The ipxact object they
# carry is left out of comparing, hashing and printing, it is only there to be read.


@dataclass(frozen=True)
class PortRef:
    """One port of one component instance, used by Design.connect()."""

    instance: IPInstance
    name: str
    port: ipxact.Port = field(compare=False, repr=False)


@dataclass(frozen=True)
class BusInterfaceRef:
    """One bus interface of one component instance, used by Design.connect_bus()."""

    instance: IPInstance
    name: str
    bus_interface: ipxact.BusInterface = field(compare=False, repr=False)


def _port_kind(ref: PortRef) -> str:
    if ref.port.wire is not None:
        return "wire"
    return "transactional" if ref.port.transactional is not None else "structured"


def _describe(ref: PortRef) -> str:
    detail = ref.port.wire.direction.value if ref.port.wire is not None else _port_kind(ref)
    return f"{ref.instance.name}.{ref.name} ({detail})"


def _port_problem(source: PortRef, target: PortRef) -> Optional[str]:
    """Return why the two ports cannot be connected, or None if they can.

    Only wire ports have their direction checked. Two transactional or two structured ports are
    connected as they are.
    """
    if source == target:
        return "cannot connect a port to itself"
    kinds = (_port_kind(source), _port_kind(target))
    if kinds[0] != kinds[1]:
        return f"cannot connect a {kinds[0]} port to a {kinds[1]} port"
    if kinds[0] != "wire":
        return None
    directions = (source.port.wire.direction, target.port.wire.direction)
    if _Direction.PHANTOM in directions:
        return "a phantom port cannot be connected"
    if directions[1] not in _CONNECTABLE[directions[0]]:
        return "needs one 'in' and one 'out' port, or an 'inout' one"
    return None


def _describe_bus(ref: BusInterfaceRef) -> str:
    bus = ref.bus_interface
    return f"{ref.instance.name}.{ref.name} ({bus.bus_type.vlnv}, {bus.mode.value})"


def _label(side: object) -> str:
    """How a connect_bus() side is shown in messages: "cpu", or "cpu.bus_if" when named."""
    if isinstance(side, BusInterfaceRef):
        return f"{side.instance.name}.{side.name}"
    return side.name if isinstance(side, IPInstance) else repr(side)


def _watches(monitor: ipxact.BusInterface, other: ipxact.BusInterface) -> bool:
    return monitor.mode == _Mode.MONITOR and monitor.monitor.interface_mode == other.mode


def _can_connect(a: BusInterfaceRef, b: BusInterfaceRef) -> bool:
    """Same bus type, and modes that can be connected, or a monitor of the other side's mode.

    Only the bus VLNV is compared for now, not the parameter values set on the bus type.
    """
    bus_a, bus_b = a.bus_interface, b.bus_interface
    if bus_a.bus_type.vlnv != bus_b.bus_type.vlnv:
        return False
    return frozenset((bus_a.mode, bus_b.mode)) in _MODE_PAIRS or _watches(bus_a, bus_b) or _watches(bus_b, bus_a)


def _no_match_reason(candidates_a: List[BusInterfaceRef], candidates_b: List[BusInterfaceRef]) -> str:
    if not candidates_a or not candidates_b:
        return "one side has no free bus interface to use"
    same_bus = any(x.bus_interface.bus_type.vlnv == y.bus_interface.bus_type.vlnv for x in candidates_a for y in candidates_b)
    return "their modes cannot be connected" if same_bus else "they share no bus type"


def _active_interface(ref: BusInterfaceRef, address_mapping: List[str]) -> ipxact.ActiveInterface:
    """One side of an interconnection. The address mapping only goes on the target side."""
    on_target_side = ref.bus_interface.mode in _TARGET_SIDE_MODES
    return ipxact.ActiveInterface(
        component_instance_ref=ref.instance.name,
        bus_ref=ref.name,
        vendor_extensions=list(address_mapping) if on_target_side else [],
    )


def _monitor_interface(ref: BusInterfaceRef) -> ipxact.MonitorInterfaceRef:
    return ipxact.MonitorInterfaceRef(component_instance_ref=ref.instance.name, bus_ref=ref.name)


@dataclass
class _InstanceRecord:
    instance: IPInstance
    component_instance: ipxact.ComponentInstance
    call_site: str


def _caller_site() -> str:
    """Return "file:line" of the code that called the public Design method."""
    caller = inspect.currentframe().f_back.f_back  # skip _caller_site itself, then the Design method
    return f"{caller.f_code.co_filename}:{caller.f_lineno}"


def _address_mapping_extensions(where: str, base_addr: Optional[int], size: Optional[int]) -> List[str]:
    """Turn connect_bus()'s base_addr and size into a vendor extension, if either is given."""
    attributes = ""
    for attribute, value, minimum in (("baseAddr", base_addr, 0), ("size", size, 1)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{where}: {attribute} must be an int, got {type(value).__name__}: {value!r}")
        if value < minimum:
            raise ValueError(f"{where}: {attribute} must be at least {minimum}, got: {value}")
        attributes += f' {attribute}="0x{value:X}"'
    if not attributes:
        return []
    return [f'<xactflow:addressMapping xmlns:xactflow="{XACTFLOW_NAMESPACE}"{attributes}/>']


def _check_type(value: object, expected: type, what: str) -> None:
    """Raise TypeError if value is not of the expected type."""
    if not isinstance(value, expected):
        raise TypeError(f"{what} must be a {expected.__name__}, got {type(value).__name__}: {value!r}")


def _parse_vlnv(value: object, what: str) -> ipxact.VLNV:
    _check_type(value, str, what)
    fields = value.split(":")
    if len(fields) != 4 or not all(fields):
        raise ValueError(f"{what} must be a 'vendor:library:name:version' string, got: {value!r}")
    return ipxact.VLNV(*fields)


def _expression(value: object, what: str) -> str:
    """Write a Python value as an IP-XACT expression string."""
    if isinstance(value, bool):  # before int, since bool is an int
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    raise TypeError(f"{what} must be an int, float, bool or str, got {type(value).__name__}: {value!r}")


def _config_element_values(
    where: str, component: ipxact.Component, parameters: Optional[Dict[str, object]]
) -> Dict[str, str]:
    """Turn add_ip()'s parameter overrides into configurableElementValues, keyed by parameterId.

    Only a parameter with resolve="user" can be overridden. The standard says an "immediate"
    one cannot be modified through configuration, and a "generated" one is set by generators.
    """
    if parameters is None:
        return {}
    _check_type(parameters, dict, f"{where}: parameters")
    values = {}
    for name, value in parameters.items():
        parameter = _find(component.parameters, name, f"{where}: {component.vlnv}", "parameter")
        if parameter.parameter_id is None or parameter.resolve != "user":
            raise ValueError(
                f"{where}: parameter {name!r} of {component.vlnv} cannot be overridden, it needs a "
                f"parameterId and resolve=\"user\" (it has parameterId={parameter.parameter_id!r}, "
                f"resolve={parameter.resolve!r}). This needs fixing in the component file"
            )
        values[parameter.parameter_id] = _expression(value, f"{where}: parameter {name!r}")
    return values


def _transforms_extensions(where: str, transforms: Optional[List[str]]) -> List[str]:
    """Turn add_ip()'s transforms into vendor extensions on the component instance.

    The transformations are only recorded here, a separate transformer applies them later.
    """
    transforms = [] if transforms is None else transforms
    if not isinstance(transforms, (list, tuple)) or not all(isinstance(t, str) and t for t in transforms):
        raise TypeError(f"{where}: transforms must be a list of non-empty strings, got: {transforms!r}")
    if not transforms:
        return []
    items = "".join(f"<xactflow:transform name={quoteattr(name)}/>" for name in transforms)
    return [f'<xactflow:transforms xmlns:xactflow="{XACTFLOW_NAMESPACE}">{items}</xactflow:transforms>']


class Design:
    """Describes one flat design: its component instances and how they are connected.

    build() turns it into an ipxact.Design.
    """

    def __init__(self, vlnv: str, library: Library) -> None:
        self.vlnv = _parse_vlnv(vlnv, "Design(): vlnv")
        self.library = library
        self._instances: Dict[str, _InstanceRecord] = {}
        self._ad_hoc_connections: Dict[FrozenSet[PortRef], ipxact.AdHocConnection] = {}
        self._interconnections: List[ipxact.Interconnection] = []
        self._monitor_interconnections: List[ipxact.MonitorInterconnection] = []
        # Names of every connection of any kind, so that no two get the same name.
        self._connection_names: Set[str] = set()
        # Each "in" wire port already driven, and each bus interface already connected, with the
        # name of the connection using it.
        self._driven_ports: Dict[PortRef, str] = {}
        self._used_bus_interfaces: Dict[BusInterfaceRef, str] = {}

    def add_ip(
        self,
        instance_name: str,
        vlnv: str,
        parameters: Optional[Dict[str, object]] = None,
        transforms: Optional[List[str]] = None,
    ) -> IPInstance:
        """Add one component instance, resolving its VLNV against the library right away."""
        call_site = _caller_site()
        self._check_new_instance_name(instance_name, call_site)
        where = f"add_ip('{instance_name}')"
        component_vlnv = _parse_vlnv(vlnv, f"{where}: vlnv")
        component = self._resolve_component(where, component_vlnv)

        component_instance = ipxact.ComponentInstance(
            instance_name=instance_name,
            component_ref=ipxact.VLNVRef(
                **asdict(component_vlnv),
                config_element_values=_config_element_values(where, component, parameters),
            ),
            vendor_extensions=_transforms_extensions(where, transforms),
        )
        instance = IPInstance(instance_name, component)
        self._instances[instance_name] = _InstanceRecord(instance, component_instance, call_site)
        return instance

    def connect(self, source: PortRef, target: PortRef) -> None:
        """Add one ad hoc connection between two whole ports."""
        for what, ref in (("connect(): source", source), ("connect(): target", target)):
            _check_type(ref, PortRef, what)
            self._check_instance(ref.instance, what)
        where = f"connect({_describe(source)}, {_describe(target)})"
        problem = _port_problem(source, target)
        if problem is not None:
            raise ValueError(f"{where}: {problem}")
        ports = frozenset((source, target))
        if ports in self._ad_hoc_connections:
            raise ValueError(f"{where}: these two ports are already connected")
        driven = [ref for ref in (source, target) if ref.port.wire is not None and ref.port.wire.direction == _Direction.IN]
        for ref in driven:
            if ref in self._driven_ports:
                raise ValueError(f"{where}: {_describe(ref)} is already driven by {self._driven_ports[ref]}")

        name = self._new_connection_name(f"{source.instance.name}_{source.name}_to_{target.instance.name}_{target.name}")
        self._ad_hoc_connections[ports] = ipxact.AdHocConnection(
            name=name,
            internal_port_references=[
                ipxact.InternalPortReference(component_instance_ref=ref.instance.name, port_ref=ref.name)
                for ref in (source, target)
            ],
        )
        self._driven_ports.update((ref, name) for ref in driven)

    def connect_bus(
        self,
        a: Union[IPInstance, BusInterfaceRef],
        b: Union[IPInstance, BusInterfaceRef],
        *,
        base_addr: Optional[int] = None,
        size: Optional[int] = None,
    ) -> None:
        """Connect one bus interface of a to one bus interface of b.

        A side given as an IPInstance uses whichever of its free initiator or target interfaces
        matches. A side given as a BusInterfaceRef uses exactly that one, whatever its mode. A
        monitor interface becomes a monitor interconnection, watching the other side.
        """
        where = f"connect_bus({_label(a)}, {_label(b)})"
        address_mapping = _address_mapping_extensions(where, base_addr, size)
        instance_a, candidates_a = self._bus_side(a, f"{where}: a")
        instance_b, candidates_b = self._bus_side(b, f"{where}: b")
        if instance_a is instance_b:
            raise ValueError(f"{where}: cannot connect an instance to itself")

        pairs = [(x, y) for x in candidates_a for y in candidates_b if _can_connect(x, y)]
        if not pairs:
            tried = ", ".join(map(_describe_bus, candidates_a + candidates_b)) or "nothing"
            raise ValueError(
                f"{where}: nothing to connect, {_no_match_reason(candidates_a, candidates_b)}. Tried: {tried}"
            )
        if len(pairs) > 1:
            choices = ", ".join(f"{_label(x)} with {_label(y)}" for x, y in pairs)
            raise ValueError(f"{where}: more than one possible pair ({choices}). Pick one with .bus(name)")
        [(ref_a, ref_b)] = pairs

        # A monitor only watches the other side, which stays free for a normal connection.
        monitor = next((ref for ref in (ref_a, ref_b) if ref.bus_interface.mode == _Mode.MONITOR), None)
        connected = [monitor] if monitor is not None else [ref_a, ref_b]
        for ref in connected:
            if ref in self._used_bus_interfaces:
                raise ValueError(f"{where}: {_label(ref)} is already used by {self._used_bus_interfaces[ref]}")
        has_target_side = any(ref.bus_interface.mode in _TARGET_SIDE_MODES for ref in (ref_a, ref_b))
        if address_mapping and (monitor is not None or not has_target_side):
            raise ValueError(f"{where}: base_addr and size need a connection with a target side")

        name = self._new_connection_name(f"{instance_a.name}_to_{instance_b.name}")
        if monitor is not None:
            watched = ref_b if monitor is ref_a else ref_a
            self._monitor_interconnections.append(
                ipxact.MonitorInterconnection(
                    name=name,
                    monitored_active_interface=_monitor_interface(watched),
                    monitor_interfaces=[_monitor_interface(monitor)],
                )
            )
        else:
            self._interconnections.append(
                ipxact.Interconnection(
                    name=name,
                    active_interface=_active_interface(ref_a, address_mapping),
                    other_active_interfaces=[_active_interface(ref_b, address_mapping)],
                )
            )
        self._used_bus_interfaces.update((ref, name) for ref in connected)

    def build(self) -> ipxact.Design:
        """Return the ipxact.Design described so far."""
        return ipxact.Design(
            vlnv=self.vlnv,
            component_instances=[record.component_instance for record in self._instances.values()],
            interconnections=list(self._interconnections),
            monitor_interconnections=list(self._monitor_interconnections),
            ad_hoc_connections=list(self._ad_hoc_connections.values()),
        )

    def _new_connection_name(self, base: str) -> str:
        """Return base, or base with a number added if a connection already has that name."""
        name, number = base, 1
        while name in self._connection_names:
            number += 1
            name = f"{base}_{number}"
        self._connection_names.add(name)
        return name

    def _check_new_instance_name(self, instance_name: object, call_site: str) -> None:
        _check_type(instance_name, str, "add_ip(): instance name")
        if not _IDENTIFIER.fullmatch(instance_name):
            raise ValueError(
                f"add_ip(): instance name must start with a letter or '_' and contain only "
                f"letters, digits and '_', got: {instance_name!r}"
            )
        earlier = self._instances.get(instance_name)
        if earlier is not None:
            raise ValueError(
                f"add_ip() at {call_site}: instance name '{instance_name}' is already used "
                f"by add_ip() at {earlier.call_site}"
            )

    def _check_instance(self, instance: IPInstance, what: str) -> None:
        """Check that instance was added to this design."""
        record = self._instances.get(instance.name)
        if record is None or record.instance is not instance:
            raise ValueError(f"{what}: {instance!r} was not added to this design")

    def _bus_side(self, side: object, what: str) -> Tuple[IPInstance, List[BusInterfaceRef]]:
        """Return the instance of one connect_bus() side, and the bus interfaces to try: the one
        given, or the instance's initiators and targets that no connect_bus() has used yet."""
        if isinstance(side, BusInterfaceRef):
            instance, candidates = side.instance, [side]
        elif isinstance(side, IPInstance):
            instance = side
            candidates = [
                ref
                for ref in (BusInterfaceRef(side, bus.name, bus) for bus in side.component.bus_interfaces)
                if ref.bus_interface.mode in _AUTO_MODES and ref not in self._used_bus_interfaces
            ]
        else:
            raise TypeError(f"{what} must be an IPInstance or a BusInterfaceRef, got {type(side).__name__}: {side!r}")
        self._check_instance(instance, what)
        return instance, candidates

    def _resolve_component(self, where: str, vlnv: ipxact.VLNV) -> ipxact.Component:
        document = self.library.get(vlnv)
        if document is None:
            message = f"{where}: no document with VLNV {vlnv} in the library"
            if self.library.diagnostics:
                message += f" (library.diagnostics has {len(self.library.diagnostics)} entries, a file may have failed to load)"
            raise ValueError(message)
        if not isinstance(document, ipxact.Component):
            raise ValueError(f"{where}: VLNV {vlnv} is a {type(document).__name__}, not a Component")
        return document
