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


@pytest.mark.parametrize(
    "lookup, available",
    [
        (lambda cpu: cpu.nope, "Available ports: bus_clk, bus_data, debug_req_i, debug_resp_o, irq_i"),
        (lambda cpu: cpu.bus("nope"), "Available bus interfaces: bus_if"),
    ],
)
def test_unknown_name_lists_what_exists(cpu, lookup, available):
    with pytest.raises(ValueError, match=available):
        lookup(cpu)


def test_real_attribute_wins_over_port_with_the_same_name(design):
    debug = design.add_ip("debug", "example.org:ip:debug_module:1.0")

    assert debug.name == "debug"
    assert debug.port("name").name == "name"
