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
        "bridge": design.add_ip("bridge", "example.org:ip:bridge:1.0"),
        "bridge2": design.add_ip("bridge2", "example.org:ip:bridge:1.0"),
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
        ("debug.jtag_io", "debug2.jtag_io"),  # inout to inout
        ("bridge.tlm_req", "bridge2.tlm_prov"),  # two ports that are not wire ports
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


@pytest.mark.parametrize(
    "source, target, message",
    [
        ("cpu.debug_req_i", "cpu.irq_i", "needs one 'in' and one 'out' port"),
        ("debug.sense_phantom", "debug.jtag_io", "a phantom port cannot be connected"),
        ("debug.jtag_io", "debug.jtag_io", "cannot connect a port to itself"),
        ("bridge.tlm_req", "cpu.irq_i", "cannot connect a transactional port to a wire port"),
    ],
)
def test_connect_rejects_ports_that_cannot_be_connected(design, instances, source, target, message):
    with pytest.raises(ValueError, match=message):
        design.connect(_port(instances, source), _port(instances, target))

    assert design.build().ad_hoc_connections == []


def test_connect_rejects_arguments_that_are_not_port_refs(design, instances):
    with pytest.raises(TypeError):
        design.connect("cpu.debug_req_i", instances["debug"].debug_resp_i)


def test_connect_rejects_a_pair_already_connected(design, instances):
    design.connect(instances["debug"].debug_req_o, instances["cpu"].debug_req_i)

    with pytest.raises(ValueError, match="already connected"):
        design.connect(instances["cpu"].debug_req_i, instances["debug"].debug_req_o)


def test_connect_rejects_ports_of_instances_not_in_this_design(library, design, instances):
    other_cpu = Design("example.org:soc:other:1.0", library).add_ip("cpu", "example.org:ip:cpu:1.0")

    with pytest.raises(ValueError, match="was not added to this design"):
        design.connect(instances["debug"].debug_req_o, other_cpu.debug_req_i)


def test_connect_rejects_a_second_driver_on_an_in_port(design, instances):
    design.connect(instances["debug"].debug_req_o, instances["cpu"].debug_req_i)

    with pytest.raises(ValueError, match="is already driven by debug_debug_req_o_to_cpu_debug_req_i"):
        design.connect(instances["debug2"].debug_req_o, instances["cpu"].debug_req_i)


def test_connect_lets_one_output_drive_several_inputs(design, instances):
    design.connect(instances["debug"].debug_req_o, instances["cpu"].debug_req_i)
    design.connect(instances["debug"].debug_req_o, instances["cpu"].irq_i)

    assert len(design.build().ad_hoc_connections) == 2
