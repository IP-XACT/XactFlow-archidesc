from xml.etree import ElementTree

import pytest

from xactflow_archidesc import Design
from xactflow_archidesc.design import XACTFLOW_NAMESPACE


@pytest.fixture
def design(library):
    return Design("example.org:soc:top:1.0", library)


@pytest.fixture
def ip(design):
    """One instance of each fixture component, by instance name."""
    components = {
        "cpu": "cpu",
        "ic": "interconnect",
        "uart": "uart",
        "timer": "timer",
        "other": "other_ip",
        "bridge": "bridge",
        "bridge2": "bridge",
    }
    return {name: design.add_ip(name, f"example.org:ip:{component}:1.0") for name, component in components.items()}


def _sides(interconnection):
    active = [interconnection.active_interface, *interconnection.other_active_interfaces]
    return [(i.component_instance_ref, i.bus_ref) for i in active]


@pytest.mark.parametrize(
    "connect, name, sides",
    [
        # Both sides picked automatically.
        (lambda ip: (ip["cpu"], ip["ic"]), "cpu_to_ic", [("cpu", "bus_if"), ("ic", "s_if")]),
        # One side named, because ic has two free initiators.
        (lambda ip: (ip["ic"].bus("m1"), ip["uart"]), "ic_to_uart", [("ic", "m1"), ("uart", "bus_if")]),
        # Target first: the first argument is still the active interface.
        (lambda ip: (ip["uart"], ip["ic"].bus("m0")), "uart_to_ic", [("uart", "bus_if"), ("ic", "m0")]),
        # The other modes connect when named.
        (lambda ip: (ip["cpu"], ip["bridge"].bus("mi")), "cpu_to_bridge", [("cpu", "bus_if"), ("bridge", "mi")]),
        (
            lambda ip: (ip["bridge"].bus("sys"), ip["bridge2"].bus("msys")),
            "bridge_to_bridge2",
            [("bridge", "sys"), ("bridge2", "msys")],
        ),
    ],
)
def test_connect_bus_adds_one_interconnection(design, ip, connect, name, sides):
    design.connect_bus(*connect(ip))

    [interconnection] = design.build().interconnections
    assert interconnection.name == name
    assert _sides(interconnection) == sides


@pytest.mark.parametrize(
    "connect, error, message",
    [
        (lambda ip: (ip["ic"], ip["uart"]), ValueError, "ic.m0 with uart.bus_if, ic.m1 with uart.bus_if"),
        (lambda ip: (ip["uart"], ip["timer"]), ValueError, "their modes cannot be connected"),
        (lambda ip: (ip["ic"].bus("m0"), ip["other"]), ValueError, "they share no bus type"),
        (lambda ip: (ip["ic"].bus("m0"), ip["ic"]), ValueError, "cannot connect an instance to itself"),
        # Other modes are never picked by themselves.
        (lambda ip: (ip["cpu"], ip["bridge"]), ValueError, "one side has no free bus interface"),
        (lambda ip: (ip["cpu"].bus_clk, ip["ic"]), TypeError, "must be an IPInstance or a BusInterfaceRef"),
    ],
)
def test_connect_bus_rejects_what_cannot_be_connected(design, ip, connect, error, message):
    with pytest.raises(error, match=message):
        design.connect_bus(*connect(ip))

    assert design.build().interconnections == []


@pytest.mark.parametrize(
    "second, message",
    [
        (lambda ip: ip["cpu"], "one side has no free bus interface"),
        (lambda ip: ip["cpu"].bus("bus_if"), "cpu.bus_if is already used by cpu_to_ic"),
    ],
)
def test_connect_bus_does_not_reuse_an_interface(design, ip, second, message):
    design.connect_bus(ip["cpu"], ip["ic"])

    with pytest.raises(ValueError, match=message):
        design.connect_bus(second(ip), ip["uart"])


def test_connect_bus_rejects_instances_not_in_this_design(library, design, ip):
    other_cpu = Design("example.org:soc:other:1.0", library).add_ip("cpu", "example.org:ip:cpu:1.0")

    with pytest.raises(ValueError, match="was not added to this design"):
        design.connect_bus(other_cpu, ip["ic"])


@pytest.mark.parametrize(
    "connect, target_side",
    [
        (lambda ip: (ip["ic"].bus("m0"), ip["uart"]), 1),  # a target
        (lambda ip: (ip["bridge"].bus("mi"), ip["cpu"]), 0),  # a mirrored initiator
    ],
)
def test_connect_bus_records_address_mapping_on_the_target_side(design, ip, connect, target_side):
    design.connect_bus(*connect(ip), base_addr=0x4001_0000, size=0x1000)

    [interconnection] = design.build().interconnections
    sides = [interconnection.active_interface, *interconnection.other_active_interfaces]
    assert sides[1 - target_side].vendor_extensions == []
    [extension] = sides[target_side].vendor_extensions
    root = ElementTree.fromstring(extension)
    assert root.tag == f"{{{XACTFLOW_NAMESPACE}}}addressMapping"
    assert root.attrib == {"baseAddr": "0x40010000", "size": "0x1000"}


@pytest.mark.parametrize(
    "connect, kwargs, error",
    [
        (lambda ip: (ip["ic"].bus("m0"), ip["uart"]), {"base_addr": -1}, ValueError),
        (lambda ip: (ip["ic"].bus("m0"), ip["uart"]), {"size": 0}, ValueError),
        # Checked before anything else, so this is the error even though the sides cannot match.
        (lambda ip: (ip["uart"], ip["timer"]), {"base_addr": "0x0"}, TypeError),
        # No target side to put the mapping on.
        (lambda ip: (ip["bridge"].bus("sys"), ip["bridge2"].bus("msys")), {"base_addr": 0}, ValueError),
        (lambda ip: (ip["bridge"].bus("mon"), ip["uart"]), {"size": 4}, ValueError),
    ],
)
def test_connect_bus_rejects_bad_address_mapping(design, ip, connect, kwargs, error):
    with pytest.raises(error):
        design.connect_bus(*connect(ip), **kwargs)

    assert design.build().interconnections == []


def test_monitor_can_watch_an_interface_already_connected(design, ip):
    design.connect_bus(ip["ic"].bus("m0"), ip["uart"])
    design.connect_bus(ip["bridge"].bus("mon"), ip["uart"].bus("bus_if"))

    built = design.build()
    [monitor] = built.monitor_interconnections
    watched = monitor.monitored_active_interface
    assert (watched.component_instance_ref, watched.bus_ref) == ("uart", "bus_if")
    assert [(m.component_instance_ref, m.bus_ref) for m in monitor.monitor_interfaces] == [("bridge", "mon")]
    assert len(built.interconnections) == 1


def test_connect_and_connect_bus_never_give_two_connections_the_same_name(design, ip):
    # The ad hoc connection below and this bus connection have the same base name.
    design.connect_bus(
        design.add_ip("debug_debug_req_o", "example.org:ip:cpu:1.0"),
        design.add_ip("cpu_debug_req_i", "example.org:ip:uart:1.0"),
    )
    design.connect(design.add_ip("debug", "example.org:ip:debug_module:1.0").debug_req_o, ip["cpu"].debug_req_i)

    built = design.build()
    assert built.interconnections[0].name == "debug_debug_req_o_to_cpu_debug_req_i"
    assert built.ad_hoc_connections[0].name == "debug_debug_req_o_to_cpu_debug_req_i_2"
