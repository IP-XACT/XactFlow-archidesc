"""design.py: the Design object a design description script builds."""

from __future__ import annotations

import copy
import math
import re
import sys
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple, Type, TypeVar, Union
from xml.sax.saxutils import quoteattr

import ipxact
from xactflow import Library

# Namespace of the vendor extensions this importer writes.
XACTFLOW_NAMESPACE = "https://github.com/IP-XACT/XactFlow"

# IP-XACT's naming rule for an instance, port or bus interface name (an XML Name in the schema).
# ":" is also allowed there but left out.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")

_VIEW = "hierarchical"
_COMPONENT_INSTANTIATION = "hierarchical_rtl"
_DESIGN_INSTANTIATION = "hierarchical_design"

_Direction = ipxact.Direction
_Mode = ipxact.InterfaceMode

# The directions each wire port direction can be connected to by connect().
_CONNECTABLE = {
    _Direction.IN: {_Direction.OUT, _Direction.INOUT},
    _Direction.OUT: {_Direction.IN, _Direction.INOUT},
    _Direction.INOUT: {_Direction.IN, _Direction.OUT, _Direction.INOUT},
}

# Seen from inside the design, a top-level "in" port drives the instance ports it connects to, and
# a top-level "out" port is driven by them.
_INSIDE_DIRECTION = {
    _Direction.IN: _Direction.OUT,
    _Direction.OUT: _Direction.IN,
    _Direction.INOUT: _Direction.INOUT,
    _Direction.PHANTOM: _Direction.PHANTOM,
}

# The directions add_port() accepts.
_TOP_DIRECTIONS = (_Direction.IN, _Direction.OUT, _Direction.INOUT)

_QUALIFIERS = {
    "connect_clk": ("is_clock", "isClock", lambda: True),
    "connect_rst": ("is_reset", "isReset", lambda: ipxact.LevelFlag(True)),
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

# The modes that belong to a system group.
_SYSTEM_MODES = {_Mode.SYSTEM, _Mode.MIRRORED_SYSTEM}

# The modes add_bus() accepts, with the BusInterface field and element each one sets. A top-level
# interface only stands for the internal one it connects to, so a monitor has nothing to watch.
_TOP_MODE_ELEMENTS = {
    _Mode.INITIATOR: ("initiator", ipxact.InitiatorInterface),
    _Mode.TARGET: ("target", ipxact.TargetInterface),
    _Mode.MIRRORED_INITIATOR: ("mirrored_initiator", ipxact.MirroredInitiatorInterface),
    _Mode.MIRRORED_TARGET: ("mirrored_target", ipxact.MirroredTargetInterface),
    _Mode.SYSTEM: ("system", ipxact.SystemInterface),
    _Mode.MIRRORED_SYSTEM: ("mirrored_system", ipxact.SystemInterface),
}


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
# carry is left out of comparing, hashing and printing, it is only there to be read. A ref with
# no instance is a top-level port or bus interface, of the component containing the design.


@dataclass(frozen=True)
class PortRef:
    """One port of one component instance, or a top-level port, used by Design.connect()."""

    instance: Optional[IPInstance]
    name: str
    port: ipxact.Port = field(compare=False, repr=False)


@dataclass(frozen=True)
class BusInterfaceRef:
    """One bus interface of one component instance, or a top-level one, used by Design.connect_bus()."""

    instance: Optional[IPInstance]
    name: str
    bus_interface: ipxact.BusInterface = field(compare=False, repr=False)


def _path(ref: Union[PortRef, BusInterfaceRef], separator: str = ".") -> str:
    """"cpu.clk_i" for a port or bus interface of an instance, "clk_i" for a top-level one."""
    return ref.name if ref.instance is None else f"{ref.instance.name}{separator}{ref.name}"


def _instance(ref: Union[PortRef, BusInterfaceRef]) -> IPInstance:
    """The instance of a ref the caller knows is not top-level."""
    if ref.instance is None:
        raise TypeError(f"{ref!r} is top-level, an instance one was expected")
    return ref.instance


def _port_kind(ref: PortRef) -> str:
    if ref.port.wire is not None:
        return "wire"
    return "transactional" if ref.port.transactional is not None else "structured"


def _top_level(ref: Union[PortRef, BusInterfaceRef]) -> str:
    return "top-level " if ref.instance is None else ""


def _describe(ref: PortRef) -> str:
    detail = ref.port.wire.direction.value if ref.port.wire is not None else _port_kind(ref)
    return f"{_path(ref)} ({_top_level(ref)}{detail})"


def _inside_direction(ref: PortRef) -> Optional[ipxact.Direction]:
    """The direction of a wire port as seen from inside the design, None for another kind of port."""
    if ref.port.wire is None:
        return None
    direction = ref.port.wire.direction
    return direction if ref.instance is not None else _INSIDE_DIRECTION[direction]


def _port_problem(source: PortRef, target: PortRef) -> Optional[str]:
    """Return why the two ports cannot be connected, or None if they can.

    Only wire ports have their direction checked. Two transactional or two structured ports are
    connected as they are. A top-level port is checked by its direction seen from inside, so it
    connects to an instance port of the same direction.
    """
    if source == target:
        return "cannot connect a port to itself"
    if source.instance is None and target.instance is None:
        return "cannot connect two top-level ports"
    kinds = (_port_kind(source), _port_kind(target))
    if kinds[0] != kinds[1]:
        return f"cannot connect a {kinds[0]} port to a {kinds[1]} port"
    source_direction, target_direction = _inside_direction(source), _inside_direction(target)
    if source_direction is None or target_direction is None:
        return None
    if _Direction.PHANTOM in (source_direction, target_direction):
        return "a phantom port cannot be connected"
    if target_direction not in _CONNECTABLE[source_direction]:
        if source.instance is None or target.instance is None:
            return "a top-level port needs an instance port of the same direction, or an 'inout' one"
        return "needs one 'in' and one 'out' port, or an 'inout' one"
    return None


def _qualified(port: ipxact.Port, flag: str) -> bool:
    """Whether a wire port has the qualifier flag set, e.g. isClock, whatever its level."""
    value = getattr(port.wire.qualifier, flag, None) if port.wire is not None else None
    return bool(value.value if isinstance(value, ipxact.LevelFlag) else value)


def _describe_bus(ref: BusInterfaceRef) -> str:
    bus = ref.bus_interface
    return f"{_path(ref)} ({_top_level(ref)}{bus.bus_type.vlnv}, {bus.mode.value})"


def _label(side: object) -> str:
    """How a connect_bus() side is shown in messages: "cpu", or "cpu.bus_if" when named."""
    if isinstance(side, BusInterfaceRef):
        return _path(side)
    return side.name if isinstance(side, IPInstance) else repr(side)


def _end_name(side: Union[IPInstance, BusInterfaceRef]) -> str:
    """One side of a connect_bus() connection name: the instance, or the top-level interface."""
    if isinstance(side, BusInterfaceRef):
        return side.name if side.instance is None else side.instance.name
    return side.name


def _watches(monitor: ipxact.BusInterface, other: ipxact.BusInterface) -> bool:
    watched = monitor.monitor
    return monitor.mode == _Mode.MONITOR and watched is not None and watched.interface_mode == other.mode


def _system_group(bus: ipxact.BusInterface) -> Optional[str]:
    """The group of a system or mirrored-system interface, or of a monitor watching one.

    None for any other interface.
    """
    if bus.monitor is not None and bus.monitor.interface_mode in _SYSTEM_MODES:
        return bus.monitor.group
    system = bus.system or bus.mirrored_system
    return system.group if system is not None else None


def _modes_match(bus_a: ipxact.BusInterface, bus_b: ipxact.BusInterface) -> bool:
    """Modes that can be connected, or a monitor of the other side's mode."""
    if _watches(bus_a, bus_b) or _watches(bus_b, bus_a):
        return True
    return frozenset((bus_a.mode, bus_b.mode)) in _MODE_PAIRS


def _bus_family(vlnv: ipxact.VLNV, library: Library) -> FrozenSet[ipxact.VLNV]:
    """The bus definition and every one it extends, directly or not."""
    family = {vlnv}
    bus_definition = library.get_bus_definition(vlnv)
    while bus_definition is not None and bus_definition.extends is not None:
        vlnv = bus_definition.extends.vlnv
        if vlnv in family:
            break  # a circular extends chain, link() reports it
        family.add(vlnv)
        bus_definition = library.get_bus_definition(vlnv)
    return frozenset(family)


# Why connect_bus() found nothing to connect, from the least to the most specific: the reason
# shown is the most specific one any pair got.
_NO_BUS_TYPE = "they share no bus type"
_NO_MODES = "their modes cannot be connected"
_NO_GROUP = "their system groups differ"
_PROBLEMS = (_NO_BUS_TYPE, _NO_MODES, _NO_GROUP)


def _active_interface(ref: BusInterfaceRef, address_mapping: List[str]) -> ipxact.ActiveInterface:
    """One instance side of an interconnection. The address mapping only goes on the target side."""
    on_target_side = ref.bus_interface.mode in _TARGET_SIDE_MODES
    return ipxact.ActiveInterface(
        component_instance_ref=_instance(ref).name,
        bus_ref=ref.name,
        vendor_extensions=list(address_mapping) if on_target_side else [],
    )


def _monitor_interface(ref: BusInterfaceRef) -> ipxact.MonitorInterfaceRef:
    return ipxact.MonitorInterfaceRef(component_instance_ref=_instance(ref).name, bus_ref=ref.name)


@dataclass
class _InstanceRecord:
    instance: IPInstance
    component_instance: ipxact.ComponentInstance
    call_site: str


def _caller_site() -> str:
    """Return "file:line" of the code that called the public Design method."""
    caller = sys._getframe(2)  # skip _caller_site itself, then the Design method
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


_T = TypeVar("_T")
_E = TypeVar("_E", bound=Enum)


def _check_type(value: object, expected: Type[_T], what: str) -> _T:
    """Return value, or raise TypeError if it is not of the expected type."""
    if not isinstance(value, expected):
        raise TypeError(f"{what} must be a {expected.__name__}, got {type(value).__name__}: {value!r}")
    return value


def _check_name(value: object, what: str) -> str:
    name = _check_type(value, str, what)
    if not _NAME.fullmatch(name):
        raise ValueError(
            f"{what} must start with a letter or '_', not a digit, followed by letters, digits, '_', '.' "
            f"or '-' (IP-XACT naming rule), got: {name!r}"
        )
    return name


def _choice(value: object, choices: Sequence[_E], what: str) -> _E:
    """Return the choice whose IP-XACT value is value, e.g. "in" for Direction.IN."""
    for choice in choices:
        if value == choice.value:
            return choice
    raise ValueError(f"{what} must be one of {', '.join(repr(c.value) for c in choices)}, got: {value!r}")


def _vectors(width: object, what: str) -> List[ipxact.Vector]:
    """A [width-1:0] vector, or none for a single bit when width is None."""
    if width is None:
        return []
    if isinstance(width, bool) or not isinstance(width, int):
        raise TypeError(f"{what} must be an int, got {type(width).__name__}: {width!r}")
    if width < 1:
        raise ValueError(f"{what} must be at least 1, got: {width}")
    return [ipxact.Vector(left=ipxact.Expression(str(width - 1)), right=ipxact.Expression("0"))]


def _mode_element(mode: ipxact.InterfaceMode, group: object, where: str) -> Tuple[str, object]:
    """The BusInterface field add_bus() sets for mode, and its value. A system mode needs its
    group, no other one."""
    if (mode in _SYSTEM_MODES) != (group is not None):
        raise ValueError(f"{where}: group is needed for a system or mirroredSystem mode, and only for them")
    attribute, element = _TOP_MODE_ELEMENTS[mode]
    if group is None:
        return attribute, element()
    return attribute, element(group=_check_type(group, str, f"{where}: group"))


def _exposed(top: BusInterfaceRef, internal: BusInterfaceRef, where: str) -> Tuple[List[ipxact.AbstractionType], List[ipxact.Port]]:
    """What a top-level bus interface takes from the internal one it connects to: its abstraction
    types, and a top-level port for each port they map, named <bus>_<logical port> and copied from
    the internal port.
    """
    component = _instance(internal).component
    internal_ports = component.model.ports if component.model is not None else []
    abstraction_types = copy.deepcopy(internal.bus_interface.abstraction_types)
    sources: Dict[str, str] = {}  # top-level port name -> internal port it copies
    for abstraction_type in abstraction_types:
        abstraction_type.view_refs = []  # they name views of the internal component
        for port_map in abstraction_type.port_maps:
            if port_map.physical_port is None:
                continue  # a tie-off, no port to copy
            if port_map.part_select is not None or port_map.sub_port_refs:
                raise ValueError(
                    f"{where}: {_path(internal)} maps logical port {port_map.logical_port!r} to part of "
                    f"port {port_map.physical_port!r}, which cannot be copied to a top-level port"
                )
            name = f"{top.name}_{port_map.logical_port}"
            source = sources.setdefault(name, port_map.physical_port)
            if source != port_map.physical_port:
                raise ValueError(
                    f"{where}: {_path(internal)} maps logical port {port_map.logical_port!r} to both "
                    f"{source!r} and {port_map.physical_port!r}"
                )
            port_map.physical_port = name
    ports = [
        replace(copy.deepcopy(_find(internal_ports, source, repr(_instance(internal)), "port")), name=name)
        for name, source in sources.items()
    ]
    return abstraction_types, ports


def _parse_vlnv(value: object, what: str) -> ipxact.VLNV:
    fields = _check_type(value, str, what).split(":")
    if len(fields) != 4 or not all(fields):
        raise ValueError(f"{what} must be a 'vendor:library:name:version' string, got: {value!r}")
    return ipxact.VLNV(*fields)


def _expression(value: object, what: str) -> ipxact.Expression:
    """Write a Python value as an IP-XACT expression."""
    if isinstance(value, bool):  # before int, since bool is an int
        return ipxact.Expression("true" if value else "false")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{what} must be a finite number, got: {value!r}")
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{what} must not be empty, got: {value!r}")
    if isinstance(value, (int, float, str)):
        return ipxact.Expression(str(value))
    raise TypeError(f"{what} must be an int, float, bool or str, got {type(value).__name__}: {value!r}")


def _config_element_values(
    where: str, component: ipxact.Component, parameters: Optional[Dict[str, object]]
) -> Dict[str, ipxact.Expression]:
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
    """Describes one design and the component containing it.

    The design holds the component instances and how they are connected. The component holds
    the top-level ports and bus interfaces, and names the module that HDL is generated for.

    For a vlnv named <name>, the design is <name>.design and the component <name>.component.
    The module is called <name> unless module_name gives another name.

    build() returns the ipxact.Design and build_component() the ipxact.Component.
    """

    def __init__(self, vlnv: str, library: Library, *, module_name: Optional[str] = None) -> None:
        base = _parse_vlnv(vlnv, "Design(): vlnv")
        self.vlnv = replace(base, name=f"{base.name}.design")
        self.component_vlnv = replace(base, name=f"{base.name}.component")
        if module_name is None:
            module_name = base.name
        elif not _check_type(module_name, str, "Design(): module_name"):
            raise ValueError("Design(): module_name must not be empty")
        self.module_name = module_name
        self.library = library
        self._instances: Dict[str, _InstanceRecord] = {}
        # The top-level ports, each with the call that added it, and the top-level bus interfaces.
        self._ports: Dict[str, Tuple[ipxact.Port, str]] = {}
        self._buses: Dict[str, ipxact.BusInterface] = {}
        self._ad_hoc_connections: Dict[FrozenSet[PortRef], ipxact.AdHocConnection] = {}
        self._interconnections: List[ipxact.Interconnection] = []
        self._monitor_interconnections: List[ipxact.MonitorInterconnection] = []
        # Names of every connection of any kind, so that no two get the same name.
        self._connection_names: Set[str] = set()
        # Each "in" wire port already driven, and each bus interface already connected, with the
        # name of the connection using it.
        self._driven_ports: Dict[PortRef, str] = {}
        self._used_bus_interfaces: Dict[BusInterfaceRef, str] = {}
        self._bus_families: Dict[ipxact.VLNV, FrozenSet[ipxact.VLNV]] = {}

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
        component = self._resolve(where, component_vlnv, ipxact.Component)

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

    def add_port(
        self, direction: str, name: str, width: Optional[int] = None, *, connect: Optional[PortRef] = None
    ) -> PortRef:
        """Add one top-level wire port, and connect it to connect if given."""
        where = f"add_port({name!r})"
        self._check_new_port_name(_check_name(name, "add_port(): name"), where)
        port = ipxact.Port(
            name=name,
            wire=ipxact.WirePort(
                direction=_choice(direction, _TOP_DIRECTIONS, f"{where}: direction"),
                vectors=_vectors(width, f"{where}: width"),
            ),
        )
        self._ports[name] = (port, "add_port()")
        ref = PortRef(None, name, port)
        if connect is not None:
            self.connect(ref, connect)
        return ref

    def add_bus(
        self,
        name: str,
        bus_type: str,
        mode: str,
        *,
        group: Optional[str] = None,
        connect: Union[IPInstance, BusInterfaceRef, None] = None,
    ) -> BusInterfaceRef:
        """Add one top-level bus interface, and connect it to connect if given.

        Its ports are added when it is connected, copied from the internal interface.
        """
        where = f"add_bus({name!r})"
        if _check_name(name, "add_bus(): name") in self._buses:
            raise ValueError(f"{where}: top-level bus interface {name!r} already exists")
        bus_type_vlnv = _parse_vlnv(bus_type, f"{where}: bus_type")
        self._resolve(where, bus_type_vlnv, ipxact.BusDefinition)
        mode = _choice(mode, list(_TOP_MODE_ELEMENTS), f"{where}: mode")
        bus_interface = ipxact.BusInterface(name=name, bus_type=ipxact.VLNVRef(**asdict(bus_type_vlnv)), mode=mode)
        setattr(bus_interface, *_mode_element(mode, group, where))
        self._buses[name] = bus_interface
        ref = BusInterfaceRef(None, name, bus_interface)
        if connect is not None:
            self.connect_bus(ref, connect)
        return ref

    def connect(self, source: PortRef, target: PortRef) -> None:
        """Add one ad hoc connection between two whole ports."""
        for what, ref in (("connect(): source", source), ("connect(): target", target)):
            self._check_ref(_check_type(ref, PortRef, what), what)
        where = f"connect({_describe(source)}, {_describe(target)})"
        problem = _port_problem(source, target)
        if problem is not None:
            raise ValueError(f"{where}: {problem}")
        ports = frozenset((source, target))
        if ports in self._ad_hoc_connections:
            raise ValueError(f"{where}: these two ports are already connected")
        driven = [ref for ref in (source, target) if _inside_direction(ref) == _Direction.IN]
        for ref in driven:
            if ref in self._driven_ports:
                raise ValueError(f"{where}: {_describe(ref)} is already driven by {self._driven_ports[ref]}")

        name = self._new_connection_name(f"{_path(source, '_')}_to_{_path(target, '_')}")
        self._ad_hoc_connections[ports] = ipxact.AdHocConnection(
            name=name,
            internal_port_references=[
                ipxact.InternalPortReference(component_instance_ref=ref.instance.name, port_ref=ref.name)
                for ref in (source, target)
                if ref.instance is not None
            ],
            external_port_references=[
                ipxact.ExternalPortReference(port_ref=ref.name) for ref in (source, target) if ref.instance is None
            ],
        )
        self._driven_ports.update((ref, name) for ref in driven)

    def connect_clk(self, source: PortRef) -> List[PortRef]:
        """Connect source to every instance input port with the isClock qualifier that nothing
        drives yet, and return them."""
        return self._connect_qualified("connect_clk", source)

    def connect_rst(self, source: PortRef) -> List[PortRef]:
        """Connect source to every instance input port with the isReset qualifier that nothing
        drives yet, and return them. Reset levels are not compared."""
        return self._connect_qualified("connect_rst", source)

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
        monitor interface becomes a monitor interconnection, watching the other side. A top-level
        interface becomes a hierInterface, and gets the ports of the internal one.
        """
        where = f"connect_bus({_label(a)}, {_label(b)})"
        address_mapping = _address_mapping_extensions(where, base_addr, size)
        instance_a, candidates_a = self._bus_side(a, f"{where}: a")
        instance_b, candidates_b = self._bus_side(b, f"{where}: b")
        if instance_a is instance_b:
            what = "two top-level bus interfaces" if instance_a is None else "an instance to itself"
            raise ValueError(f"{where}: cannot connect {what}")
        hierarchical = instance_a is None or instance_b is None

        problems = {(x, y): self._connect_problem(x, y, hierarchical) for x in candidates_a for y in candidates_b}
        pairs = [pair for pair, problem in problems.items() if problem is None]
        if not pairs:
            tried = ", ".join(map(_describe_bus, candidates_a + candidates_b)) or "nothing"
            reason = max(problems.values(), key=_PROBLEMS.index) if problems else "one side has no free bus interface to use"
            raise ValueError(f"{where}: nothing to connect, {reason}. Tried: {tried}")
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
        if address_mapping and (monitor is not None or hierarchical or not has_target_side):
            raise ValueError(f"{where}: base_addr and size need a connection between two instances, with a target side")
        top = next((ref for ref in (ref_a, ref_b) if ref.instance is None), None)
        if top is not None:
            internal = ref_b if top is ref_a else ref_a
            abstraction_types, ports = _exposed(top, internal, where)
            for port in ports:
                self._check_new_port_name(port.name, where)

        name = self._new_connection_name(f"{_end_name(a)}_to_{_end_name(b)}")
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
            active = [_active_interface(ref, address_mapping) for ref in (ref_a, ref_b) if ref.instance is not None]
            self._interconnections.append(
                ipxact.Interconnection(
                    name=name,
                    active_interface=active[0],
                    other_active_interfaces=active[1:],
                    hier_interfaces=[ipxact.HierInterface(bus_ref=top.name)] if top is not None else [],
                )
            )
        if top is not None:
            top.bus_interface.abstraction_types = abstraction_types
            self._ports.update((port.name, (port, where)) for port in ports)
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

    def build_component(self) -> ipxact.Component:
        """Return the ipxact.Component containing the design: the top-level ports and bus
        interfaces, and a view instantiating the design and naming its module."""
        return ipxact.Component(
            vlnv=self.component_vlnv,
            bus_interfaces=list(self._buses.values()),
            model=ipxact.Model(
                views=[
                    ipxact.View(
                        name=_VIEW,
                        component_instantiation_ref=_COMPONENT_INSTANTIATION,
                        design_instantiation_ref=_DESIGN_INSTANTIATION,
                    )
                ],
                component_instantiations=[
                    ipxact.ComponentInstantiation(name=_COMPONENT_INSTANTIATION, module_name=self.module_name)
                ],
                design_instantiations=[
                    ipxact.DesignInstantiation(name=_DESIGN_INSTANTIATION, design_ref=ipxact.VLNVRef(**asdict(self.vlnv)))
                ],
                ports=[port for port, _ in self._ports.values()],
            ),
        )

    def _connect_problem(self, a: BusInterfaceRef, b: BusInterfaceRef, hierarchical: bool) -> Optional[str]:
        """Return why the two bus interfaces cannot be connected, or None if they can.

        They need the same bus definition, or one that extends the other (SCR 2.2), and modes
        that can be connected: opposite ones between two instances, the same one between a
        top-level interface and an instance (SCR 2.4 to 2.10). A system interface, a
        mirrored-system interface and a monitor of either also need the same group (SCR 2.13,
        4.4). Only the bus definitions are compared for now, not the parameter values set on the
        bus type.
        """
        bus_a, bus_b = a.bus_interface, b.bus_interface
        family_a, family_b = self._bus_family(bus_a), self._bus_family(bus_b)
        if bus_a.bus_type.vlnv not in family_b and bus_b.bus_type.vlnv not in family_a:
            return _NO_BUS_TYPE
        if not (bus_a.mode == bus_b.mode if hierarchical else _modes_match(bus_a, bus_b)):
            return _NO_MODES
        if _system_group(bus_a) != _system_group(bus_b):
            return _NO_GROUP
        return None

    def _connect_qualified(self, call: str, source: PortRef) -> List[PortRef]:
        """connect() source to the free input ports of the other instances with the qualifier of
        call. A top-level source gets that qualifier too."""
        flag, element, make_flag = _QUALIFIERS[call]
        self._check_ref(_check_type(source, PortRef, f"{call}(): source"), f"{call}(): source")
        where = f"{call}({_describe(source)})"
        if _inside_direction(source) not in (_Direction.OUT, _Direction.INOUT):
            raise ValueError(
                f"{where}: the source must drive the ports it connects to: a top-level 'in' or 'inout' "
                f"wire port, or an instance 'out' or 'inout' one"
            )
        targets = [
            ref
            for record in self._instances.values()
            if record.instance is not source.instance and record.instance.component.model is not None
            for ref in (PortRef(record.instance, port.name, port) for port in record.instance.component.model.ports)
            if _inside_direction(ref) == _Direction.IN and _qualified(ref.port, flag) and ref not in self._driven_ports
        ]
        if not targets:
            raise ValueError(f"{where}: no instance has an 'in' port with the {element} qualifier left to connect")
        wire = source.port.wire
        if source.instance is None and wire is not None and not _qualified(source.port, flag):
            if wire.qualifier is None:
                wire.qualifier = ipxact.Qualifier()
            setattr(wire.qualifier, flag, make_flag())
        for target in targets:
            self.connect(source, target)
        return targets

    def _bus_family(self, bus: ipxact.BusInterface) -> FrozenSet[ipxact.VLNV]:
        vlnv = bus.bus_type.vlnv
        if vlnv not in self._bus_families:
            self._bus_families[vlnv] = _bus_family(vlnv, self.library)
        return self._bus_families[vlnv]

    def _new_connection_name(self, base: str) -> str:
        """Return base, or base with a number added if a connection already has that name."""
        name, number = base, 1
        while name in self._connection_names:
            number += 1
            name = f"{base}_{number}"
        self._connection_names.add(name)
        return name

    def _check_new_instance_name(self, instance_name: object, call_site: str) -> None:
        instance_name = _check_name(instance_name, "add_ip(): instance name")
        earlier = self._instances.get(instance_name)
        if earlier is not None:
            raise ValueError(
                f"add_ip() at {call_site}: instance name '{instance_name}' is already used "
                f"by add_ip() at {earlier.call_site}"
            )

    def _check_new_port_name(self, name: str, where: str) -> None:
        if name in self._ports:
            raise ValueError(f"{where}: top-level port {name!r} already exists, added by {self._ports[name][1]}")

    def _check_instance(self, instance: IPInstance, what: str) -> None:
        """Check that instance was added to this design."""
        record = self._instances.get(instance.name)
        if record is None or record.instance is not instance:
            raise ValueError(f"{what}: {instance!r} was not added to this design")

    def _check_ref(self, ref: Union[PortRef, BusInterfaceRef], what: str) -> None:
        """Check that ref is on an instance of this design, or is one of its top-level ports or
        bus interfaces."""
        if ref.instance is not None:
            self._check_instance(ref.instance, what)
            return
        added: object
        top_level: object
        if isinstance(ref, PortRef):
            added, top_level = self._ports.get(ref.name, (None,))[0], ref.port
        else:
            added, top_level = self._buses.get(ref.name), ref.bus_interface
        if top_level is not added:
            raise ValueError(f"{what}: {ref!r} was not added to this design")

    def _bus_side(self, side: object, what: str) -> Tuple[Optional[IPInstance], List[BusInterfaceRef]]:
        """Return the instance of one connect_bus() side (None for a top-level interface), and the
        bus interfaces to try: the one given, or the instance's initiators and targets that no
        connect_bus() has used yet."""
        if isinstance(side, BusInterfaceRef):
            self._check_ref(side, what)
            return side.instance, [side]
        if isinstance(side, IPInstance):
            self._check_instance(side, what)
            candidates = [
                ref
                for ref in (BusInterfaceRef(side, bus.name, bus) for bus in side.component.bus_interfaces)
                if ref.bus_interface.mode in _AUTO_MODES and ref not in self._used_bus_interfaces
            ]
            return side, candidates
        raise TypeError(f"{what} must be an IPInstance or a BusInterfaceRef, got {type(side).__name__}: {side!r}")

    def _resolve(self, where: str, vlnv: ipxact.VLNV, kind: Type[_T]) -> _T:
        """Return the document of the library with that VLNV, which must be a kind."""
        document = self.library.get(vlnv)
        if document is None:
            message = f"{where}: no document with VLNV {vlnv} in the library"
            if self.library.diagnostics:
                message += f" (library.diagnostics has {len(self.library.diagnostics)} entries, a file may have failed to load)"
            raise ValueError(message)
        if not isinstance(document, kind):
            raise ValueError(f"{where}: VLNV {vlnv} is a {type(document).__name__}, not a {kind.__name__}")
        return document
