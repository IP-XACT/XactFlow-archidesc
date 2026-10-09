import copy

import pytest

from xactflow_archidesc import BusInterfaceRef, Design, PortRef


@pytest.fixture
def design(library):
    return Design("example.org:soc:top:1.0", library)


@pytest.fixture
def cpu(design):
    return design.add_ip("cpu", "example.org:ip:cpu:1.0")


@pytest.mark.parametrize(
    "lookup, ref_type, name",
    [
        (lambda cpu: cpu.port("debug_req_i"), PortRef, "debug_req_i"),
        (lambda cpu: cpu.debug_req_i, PortRef, "debug_req_i"),
        (lambda cpu: cpu.bus("bus_if"), BusInterfaceRef, "bus_if"),
    ],
)
def test_lookup_returns_ref_on_this_instance(cpu, lookup, ref_type, name):
    ref = lookup(cpu)

    assert isinstance(ref, ref_type)
    assert ref.instance is cpu
    assert ref.name == name


def test_refs_are_equal_by_instance_and_name_and_hashable(design, cpu):
    other_cpu = design.add_ip("other_cpu", "example.org:ip:cpu:1.0")

    assert cpu.port("debug_req_i") == cpu.debug_req_i
    assert cpu.debug_req_i != cpu.debug_resp_o
    assert cpu.debug_req_i != other_cpu.debug_req_i
    assert len({cpu.debug_req_i, cpu.debug_req_i, cpu.bus("bus_if"), cpu.bus("bus_if")}) == 2


def test_port_ref_carries_the_port_direction(cpu):
    assert cpu.debug_req_i.port.wire.direction == "in"
    assert cpu.debug_resp_o.port.wire.direction == "out"


@pytest.mark.parametrize(
    "lookup, available",
    [
        (lambda cpu: cpu.port("nope"), "Available ports: bus_clk, bus_data, debug_req_i, debug_resp_o, irq_i"),
        (lambda cpu: cpu.nope, "Available ports: bus_clk, bus_data, debug_req_i, debug_resp_o, irq_i"),
        (lambda cpu: cpu.bus("nope"), "Available bus interfaces: bus_if"),
    ],
)
def test_unknown_name_lists_what_exists(cpu, lookup, available):
    with pytest.raises(ValueError) as exc_info:
        lookup(cpu)

    message = str(exc_info.value)
    assert "'cpu'" in message
    assert "'nope'" in message
    assert available in message


def test_component_without_bus_interfaces_lists_none(design):
    debug = design.add_ip("debug", "example.org:ip:debug_module:1.0")

    with pytest.raises(ValueError, match="Available bus interfaces: none"):
        debug.bus("bus_if")


def test_real_attribute_wins_over_port_with_the_same_name(design):
    debug = design.add_ip("debug", "example.org:ip:debug_module:1.0")

    assert debug.name == "debug"
    assert debug.port("name").name == "name"


def test_instance_can_be_copied(cpu):
    # copy looks up special names like __deepcopy__, which must not be taken as port names.
    clone = copy.deepcopy(cpu)

    assert clone.name == "cpu"
    assert clone.debug_req_i.name == "debug_req_i"
