"""The top-level ports and bus interfaces of the component containing the design."""

from pathlib import Path

import pytest
from xactflow import Library, link

from xactflow_archidesc import Design

LIBRARY = Path(__file__).parent / "fixtures" / "library"
SIMPLEBUS = "example.org:bus:simplebus:1.0"


@pytest.fixture
def design(library):
    return Design("example.org:soc:top:1.0", library)


@pytest.fixture
def ip(design):
    """One instance of each fixture component the tests use, by instance name."""
    components = {"cpu": "cpu", "ic": "interconnect", "uart": "uart", "debug": "debug_module", "bridge": "bridge"}
    return {name: design.add_ip(name, f"example.org:ip:{component}:1.0") for name, component in components.items()}


def _port(ip, path):
    """Look up "instance.port" among the fixture instances."""
    instance, port = path.split(".")
    return ip[instance].port(port)


def test_the_component_has_a_view_instantiating_the_design(design):
    model = design.build_component().model

    [view] = model.views
    [instantiation] = model.design_instantiations
    assert view.design_instantiation_ref == instantiation.name
    assert instantiation.design_ref.vlnv == design.build().vlnv


@pytest.mark.parametrize("kwargs, module_name", [({}, "top"), ({"module_name": "soc_top"}, "soc_top")])
def test_the_view_names_the_module_of_the_component(library, kwargs, module_name):
    model = Design("example.org:soc:top:1.0", library, **kwargs).build_component().model

    [view] = model.views
    [instantiation] = model.component_instantiations
    assert view.component_instantiation_ref == instantiation.name
    assert instantiation.module_name == module_name


@pytest.mark.parametrize(
    "module_name, error, message",
    [("", ValueError, "module_name must not be empty"), (1, TypeError, "module_name must be a str")],
)
def test_design_rejects_a_bad_module_name(library, module_name, error, message):
    with pytest.raises(error, match=message):
        Design("example.org:soc:top:1.0", library, module_name=module_name)


# Ports


@pytest.mark.parametrize(
    "direction, width, vectors",
    [("in", None, []), ("out", 8, [("7", "0")]), ("inout", 1, [("0", "0")])],
)
def test_add_port_adds_a_wire_port_to_the_component(design, direction, width, vectors):
    design.add_port(direction, "p", width)

    [port] = design.build_component().model.ports
    assert port.name == "p"
    assert port.wire.direction.value == direction
    assert [(v.left.value, v.right.value) for v in port.wire.vectors] == vectors


@pytest.mark.parametrize(
    "args, error, message",
    [
        (("input", "p"), ValueError, "direction must be one of 'in', 'out', 'inout'"),
        (("phantom", "p"), ValueError, "direction must be one of"),
        (("in", "1p"), ValueError, "naming rule"),
        (("in", 1), TypeError, "name must be a str"),
        (("in", "p", 0), ValueError, "width must be at least 1"),
        (("in", "p", True), TypeError, "width must be an int"),
        (("in", "p", "8"), TypeError, "width must be an int"),
        (("out", "clk_i"), ValueError, "top-level port 'clk_i' already exists, added by add_port"),
    ],
)
def test_add_port_rejects_bad_arguments(design, args, error, message):
    design.add_port("in", "clk_i")

    with pytest.raises(error, match=message):
        design.add_port(*args)


@pytest.mark.parametrize(
    "direction, internal",
    [
        ("in", "cpu.debug_req_i"),  # a top-level input drives an instance input
        ("out", "cpu.debug_resp_o"),  # an instance output drives a top-level output
        ("inout", "debug.jtag_io"),
        ("in", "debug.jtag_io"),
    ],
)
@pytest.mark.parametrize("shortcut", [False, True])
def test_connect_adds_an_external_port_reference(design, ip, direction, internal, shortcut):
    if shortcut:
        design.add_port(direction, "p", connect=_port(ip, internal))
    else:
        design.connect(design.add_port(direction, "p"), _port(ip, internal))

    [connection] = design.build().ad_hoc_connections
    instance, port = internal.split(".")
    assert connection.name == f"p_to_{instance}_{port}"
    assert [(r.component_instance_ref, r.port_ref) for r in connection.internal_port_references] == [(instance, port)]
    assert [r.port_ref for r in connection.external_port_references] == ["p"]


@pytest.mark.parametrize(
    "direction, internal, message",
    [
        ("in", "cpu.debug_resp_o", "a top-level port needs an instance port of the same direction"),
        ("out", "cpu.debug_req_i", "a top-level port needs an instance port of the same direction"),
        ("in", "debug.sense_phantom", "a phantom port cannot be connected"),
        ("in", "bridge.tlm_req", "cannot connect a wire port to a transactional port"),
    ],
)
def test_connect_rejects_incompatible_top_level_ports(design, ip, direction, internal, message):
    top = design.add_port(direction, "p")

    with pytest.raises(ValueError, match=message):
        design.connect(top, _port(ip, internal))


def test_connect_rejects_two_top_level_ports(design):
    with pytest.raises(ValueError, match="cannot connect two top-level ports"):
        design.connect(design.add_port("in", "p"), design.add_port("out", "q"))


def test_connect_rejects_a_top_level_port_of_another_design(design, ip, library):
    other = Design("example.org:soc:other:1.0", library)

    with pytest.raises(ValueError, match="was not added to this design"):
        design.connect(other.add_port("in", "p"), ip["cpu"].debug_req_i)


def test_a_top_level_input_drives_several_instance_inputs(design, ip):
    clk = design.add_port("in", "p", connect=ip["cpu"].debug_req_i)
    design.connect(clk, ip["debug"].debug_resp_i)

    with pytest.raises(ValueError, match=r"cpu.debug_req_i \(in\) is already driven by p_to_cpu_debug_req_i"):
        design.connect(ip["debug"].debug_req_o, ip["cpu"].debug_req_i)


def test_a_top_level_output_is_driven_once(design, ip):
    out = design.add_port("out", "p", connect=ip["cpu"].debug_resp_o)

    with pytest.raises(ValueError, match=r"p \(top-level out\) is already driven by p_to_cpu_debug_resp_o"):
        design.connect(ip["debug"].debug_req_o, out)


# Bus interfaces


@pytest.mark.parametrize(
    "mode, group, element",
    [
        ("initiator", None, "initiator"),
        ("target", None, "target"),
        ("mirroredInitiator", None, "mirrored_initiator"),
        ("mirroredTarget", None, "mirrored_target"),
        ("system", "g", "system"),
        ("mirroredSystem", "g", "mirrored_system"),
    ],
)
def test_add_bus_adds_a_bus_interface_to_the_component(design, mode, group, element):
    design.add_bus("ext_if", SIMPLEBUS, mode, group=group)

    [bus] = design.build_component().bus_interfaces
    assert (bus.name, str(bus.bus_type.vlnv), bus.mode.value) == ("ext_if", SIMPLEBUS, mode)
    assert getattr(bus, element) is not None
    assert getattr(getattr(bus, element), "group", None) == group


@pytest.mark.parametrize(
    "args, kwargs, error, message",
    [
        (("ext", SIMPLEBUS, "monitor"), {}, ValueError, "mode must be one of 'initiator', 'target'"),
        (("ext", SIMPLEBUS, "slave"), {}, ValueError, "mode must be one of"),
        (("ext", SIMPLEBUS, "system"), {}, ValueError, "group is needed for a system or mirroredSystem mode"),
        (("ext", SIMPLEBUS, "target"), {"group": "g"}, ValueError, "and only for them"),
        (("ext", SIMPLEBUS, "system"), {"group": 1}, TypeError, "group must be a str"),
        (("ext", "example.org:bus:nobus:1.0", "target"), {}, ValueError, "no document with VLNV"),
        (("ext", "example.org:ip:cpu:1.0", "target"), {}, ValueError, "is a Component, not a BusDefinition"),
        (("ext", "simplebus", "target"), {}, ValueError, "vendor:library:name:version"),
        (("1ext", SIMPLEBUS, "target"), {}, ValueError, "naming rule"),
        (("ext_if", SIMPLEBUS, "target"), {}, ValueError, "top-level bus interface 'ext_if' already exists"),
    ],
)
def test_add_bus_rejects_bad_arguments(design, args, kwargs, error, message):
    design.add_bus("ext_if", SIMPLEBUS, "initiator")

    with pytest.raises(error, match=message):
        design.add_bus(*args, **kwargs)


@pytest.mark.parametrize(
    "mode, internal, active, direction",
    [
        # A top-level interface connects to an instance interface of the same mode.
        ("target", lambda ip: ip["ic"], ("ic", "s_if"), "in"),
        ("initiator", lambda ip: ip["ic"].bus("m0"), ("ic", "m0"), "out"),
        ("initiator", lambda ip: ip["cpu"], ("cpu", "bus_if"), "out"),
    ],
)
@pytest.mark.parametrize("shortcut", [False, True])
def test_connect_bus_adds_a_hier_interface_and_its_ports(design, ip, mode, internal, active, direction, shortcut):
    if shortcut:
        design.add_bus("ext_if", SIMPLEBUS, mode, connect=internal(ip))
    else:
        design.connect_bus(design.add_bus("ext_if", SIMPLEBUS, mode), internal(ip))

    [interconnection] = design.build().interconnections
    assert interconnection.name == f"ext_if_to_{active[0]}"
    assert (interconnection.active_interface.component_instance_ref, interconnection.active_interface.bus_ref) == active
    assert interconnection.other_active_interfaces == []
    assert [h.bus_ref for h in interconnection.hier_interfaces] == ["ext_if"]

    component = design.build_component()
    [bus] = component.bus_interfaces
    [abstraction_type] = bus.abstraction_types
    assert str(abstraction_type.abstraction_ref.vlnv) == "example.org:bus:simplebus_rtl:1.0"
    assert [(m.logical_port, m.physical_port) for m in abstraction_type.port_maps] == [
        ("CLK", "ext_if_CLK"),
        ("DATA", "ext_if_DATA"),
    ]
    assert [(p.name, p.wire.direction.value) for p in component.model.ports] == [
        ("ext_if_CLK", direction),
        ("ext_if_DATA", direction),
    ]


@pytest.mark.parametrize(
    "connect, message",
    [
        # The modes of a top-level interface and an instance one must be the same.
        (lambda design, ext, ip: design.connect_bus(ext, ip["uart"]), "nothing to connect, their modes cannot be connected"),
        (lambda design, ext, ip: design.connect_bus(ext, design.add_bus("ext2", SIMPLEBUS, "target")), "cannot connect two top-level bus interfaces"),
        (lambda design, ext, ip: design.connect_bus(ext, ip["ic"].bus("m0"), base_addr=0), "base_addr and size need a connection between two instances"),
        (lambda design, ext, ip: [design.connect_bus(ext, ip["ic"].bus(bus)) for bus in ("m0", "m1")], "ext_if is already used by ext_if_to_ic"),
        (lambda design, ext, ip: [design.add_port("in", "ext_if_CLK"), design.connect_bus(ext, ip["cpu"])], "top-level port 'ext_if_CLK' already exists, added by add_port"),
    ],
)
def test_connect_bus_rejects_a_bad_top_level_connection(design, ip, connect, message):
    ext = design.add_bus("ext_if", SIMPLEBUS, "initiator")

    with pytest.raises(ValueError, match=message):
        connect(design, ext, ip)


def test_a_rejected_connection_adds_no_top_level_port(design, ip):
    design.add_port("in", "ext_if_DATA")
    ext = design.add_bus("ext_if", SIMPLEBUS, "initiator")

    with pytest.raises(ValueError, match="'ext_if_DATA' already exists"):
        design.connect_bus(ext, ip["cpu"])

    assert [p.name for p in design.build_component().model.ports] == ["ext_if_DATA"]
    assert design.build_component().bus_interfaces[0].abstraction_types == []


def test_the_design_links_against_the_component_containing_it():
    library = Library.scan(LIBRARY)
    design = Design("example.org:soc:top:1.0", library)
    cpu = design.add_ip("cpu", "example.org:ip:cpu:1.0")
    ic = design.add_ip("ic", "example.org:ip:interconnect:1.0")
    design.connect_bus(cpu, ic)
    design.add_bus("ext_if", SIMPLEBUS, "initiator", connect=ic.bus("m0"))
    design.add_port("in", "debug_req_i", connect=cpu.debug_req_i)
    library.add(design.build_component())
    library.add(design.build())

    linked = link(design.build(), library)

    assert linked.diagnostics == []
    assert linked.component.vlnv == design.component_vlnv
    assert [h.bus_interface.name for i in linked.interconnections for h in i.hier_interfaces] == ["ext_if"]
    assert [r.port.name for refs in linked.ad_hoc_external_ports.values() for r in refs] == ["debug_req_i"]
