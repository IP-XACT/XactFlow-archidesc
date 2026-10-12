import pytest

from xactflow_archidesc import Design


@pytest.fixture
def design(library):
    return Design("example.org:soc:top:1.0", library)


@pytest.fixture
def instances(design):
    return {
        "cpu": design.add_ip("cpu", "example.org:ip:cpu:1.0"),
        "debug": design.add_ip("debug", "example.org:ip:debug_module:1.0"),
        "debug2": design.add_ip("debug2", "example.org:ip:debug_module:1.0"),
    }


def _port(instances, path):
    """Look up "instance.port" among the fixture instances."""
    instance, port = path.split(".")
    return instances[instance].port(port)


@pytest.mark.parametrize(
    "source, target",
    [
        ("debug.debug_req_o", "cpu.debug_req_i"),  # out to in
        ("cpu.debug_req_i", "debug.debug_req_o"),  # in to out
        ("debug.jtag_io", "cpu.irq_i"),  # inout to in
        ("cpu.debug_resp_o", "debug.jtag_io"),  # out to inout
        ("debug.jtag_io", "debug2.jtag_io"),  # inout to inout
    ],
)
def test_connect_adds_one_ad_hoc_connection(design, instances, source, target):
    design.connect(_port(instances, source), _port(instances, target))

    [connection] = design.build().ad_hoc_connections
    assert connection.name == source.replace(".", "_") + "_to_" + target.replace(".", "_")
    assert [(r.component_instance_ref, r.port_ref) for r in connection.internal_port_references] == [
        tuple(source.split(".")),
        tuple(target.split(".")),
    ]
    assert connection.external_port_references == []


@pytest.mark.parametrize(
    "source, target, described",
    [
        ("cpu.debug_req_i", "cpu.irq_i", ["cpu.debug_req_i (in)", "cpu.irq_i (in)"]),
        ("debug.debug_req_o", "debug.debug_req_x", ["debug.debug_req_o (out)", "debug.debug_req_x (out)"]),
        ("debug.sense_phantom", "debug.jtag_io", ["debug.sense_phantom (phantom)", "debug.jtag_io (inout)"]),
        ("debug.jtag_io", "debug.jtag_io", ["debug.jtag_io (inout)", "to itself"]),
    ],
)
def test_connect_rejects_ports_that_cannot_be_connected(design, instances, source, target, described):
    with pytest.raises(ValueError) as exc_info:
        design.connect(_port(instances, source), _port(instances, target))

    assert all(text in str(exc_info.value) for text in described)
    assert design.build().ad_hoc_connections == []


@pytest.mark.parametrize("bad", ["cpu.debug_req_i", None, "bus"])
def test_connect_rejects_arguments_that_are_not_port_refs(design, instances, bad):
    bad = instances["cpu"].bus("bus_if") if bad == "bus" else bad

    with pytest.raises(TypeError):
        design.connect(bad, instances["debug"].debug_resp_i)
    with pytest.raises(TypeError):
        design.connect(instances["debug"].debug_req_o, bad)


@pytest.mark.parametrize("second", [("debug.debug_req_o", "cpu.debug_req_i"), ("cpu.debug_req_i", "debug.debug_req_o")])
def test_connect_rejects_a_pair_already_connected_in_either_order(design, instances, second):
    design.connect(instances["debug"].debug_req_o, instances["cpu"].debug_req_i)

    with pytest.raises(ValueError, match="already connected"):
        design.connect(_port(instances, second[0]), _port(instances, second[1]))
    assert len(design.build().ad_hoc_connections) == 1


def test_connect_rejects_ports_of_instances_not_in_this_design(library, design, instances):
    other = Design("example.org:soc:other:1.0", library)
    # Same instance name as in design, but a different instance.
    other_cpu = other.add_ip("cpu", "example.org:ip:cpu:1.0")

    with pytest.raises(ValueError, match="was not added to this design"):
        design.connect(instances["debug"].debug_req_o, other_cpu.debug_req_i)
    assert design.build().ad_hoc_connections == []


def test_connect_adds_a_number_when_the_name_is_already_taken(design, instances):
    # "a" + "m0_clk" and "a_m0" + "clk" give the same name.
    a = design.add_ip("a", "example.org:ip:interconnect:1.0")
    a_m0 = design.add_ip("a_m0", "example.org:ip:other_ip:1.0")

    design.connect(instances["debug"].jtag_io, a.m0_clk)
    design.connect(instances["debug"].jtag_io, a_m0.clk)

    assert [c.name for c in design.build().ad_hoc_connections] == [
        "debug_jtag_io_to_a_m0_clk",
        "debug_jtag_io_to_a_m0_clk_2",
    ]
