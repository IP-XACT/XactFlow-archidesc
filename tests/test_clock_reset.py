"""connect_clk() and connect_rst(): one source to every instance port with the isClock or isReset qualifier."""

from pathlib import Path

import ipxact
import pytest
from xactflow import Library, link

from xactflow_archidesc import Design

LIBRARY = Path(__file__).parent / "fixtures" / "library"
CLOCKED = "example.org:ip:clocked:1.0"

# Each call, the qualifier it gives a top-level source, and the ports of the clocked fixture it
# connects: every "in" port with that qualifier, whatever the reset level.
CALLS = [
    ("connect_clk", ipxact.Qualifier(is_clock=True), ["clk_i", "ref_clk_i"]),
    ("connect_rst", ipxact.Qualifier(is_reset=ipxact.LevelFlag(True)), ["rst_ni", "srst_i"]),
]


@pytest.fixture
def design(library):
    return Design("example.org:soc:top:1.0", library)


@pytest.fixture
def ip(design):
    return {name: design.add_ip(name, CLOCKED) for name in ("a", "b")}


def _connected(design):
    """Each ad hoc connection as (source, target), "instance.port" or a top-level port name."""
    return [
        tuple(r.port_ref for r in c.external_port_references)
        + tuple(f"{r.component_instance_ref}.{r.port_ref}" for r in c.internal_port_references)
        for c in design.build().ad_hoc_connections
    ]


@pytest.mark.parametrize("call, qualifier, ports", CALLS)
def test_a_top_level_port_drives_every_qualified_instance_input(design, ip, call, qualifier, ports):
    top = design.add_port("in", "p")

    connected = getattr(design, call)(top)

    expected = [f"{instance}.{port}" for instance in ip for port in ports]
    assert [f"{ref.instance.name}.{ref.name}" for ref in connected] == expected
    assert _connected(design) == [("p", end) for end in expected]
    [port] = design.build_component().model.ports
    assert port.wire.qualifier == qualifier


@pytest.mark.parametrize(
    "port",
    [
        "clk",  # named like a clock, no qualifier
        "rst_n",  # named like a reset, no qualifier
        "no_clk_i",  # isClock set to false
        "data_i",  # no qualifier
    ],
)
def test_a_port_without_the_qualifier_is_not_connected(design, ip, port):
    design.connect_clk(design.add_port("in", "clk_i"))
    design.connect_rst(design.add_port("in", "rst_ni"))

    assert [end for end in _connected(design) if end[1].endswith(f".{port}")] == []


@pytest.mark.parametrize("call, qualifier, ports", CALLS)
def test_an_instance_output_drives_the_other_instances(design, ip, call, qualifier, ports):
    source = ip["a"].clk_o if call == "connect_clk" else ip["a"].rst_o

    getattr(design, call)(source)

    assert _connected(design) == [(f"a.{source.name}", f"b.{port}") for port in ports]


@pytest.mark.parametrize("call, qualifier, ports", CALLS)
def test_ports_already_driven_are_left_as_they_are(design, ip, call, qualifier, ports):
    design.connect(design.add_port("in", "other"), ip["a"].port(ports[0]))

    connected = getattr(design, call)(design.add_port("in", "p"))

    assert [f"{ref.instance.name}.{ref.name}" for ref in connected] == [f"a.{ports[1]}"] + [f"b.{p}" for p in ports]


@pytest.mark.parametrize("call, qualifier, ports", CALLS)
def test_only_instances_added_before_are_connected(design, call, qualifier, ports):
    design.add_ip("a", CLOCKED)
    top = design.add_port("in", "p")
    getattr(design, call)(top)
    design.add_ip("b", CLOCKED)

    assert [end for _, end in _connected(design)] == [f"a.{port}" for port in ports]


@pytest.mark.parametrize("call, qualifier, ports", CALLS)
@pytest.mark.parametrize(
    "source, error, message",
    [
        (lambda design, ip: design.add_port("out", "p"), ValueError, "the source must drive the ports it connects to"),
        (lambda design, ip: ip["a"].clk_i, ValueError, "the source must drive the ports it connects to"),
        (lambda design, ip: design.add_ip("bridge", "example.org:ip:bridge:1.0").st_o, ValueError, "must drive"),
        (lambda design, ip: Design("example.org:soc:other:1.0", design.library).add_port("in", "p"), ValueError, "was not added to this design"),
        (lambda design, ip: "p", TypeError, "source must be a PortRef"),
    ],
)
def test_a_bad_source_is_refused(design, ip, call, qualifier, ports, source, error, message):
    with pytest.raises(error, match=message):
        getattr(design, call)(source(design, ip))


@pytest.mark.parametrize("call, qualifier, ports", CALLS)
def test_nothing_left_to_connect_is_refused(design, call, qualifier, ports):
    design.add_ip("cpu", "example.org:ip:cpu:1.0")  # no qualified port
    element = "isClock" if call == "connect_clk" else "isReset"

    with pytest.raises(ValueError, match=f"no instance has an 'in' port with the {element} qualifier left to connect"):
        getattr(design, call)(design.add_port("in", "p"))


@pytest.mark.parametrize("call, qualifier, ports", CALLS)
def test_a_second_call_finds_nothing_left(design, ip, call, qualifier, ports):
    getattr(design, call)(design.add_port("in", "p"))

    with pytest.raises(ValueError, match="left to connect"):
        getattr(design, call)(design.add_port("in", "q"))


def test_the_design_links_with_clock_and_reset():
    library = Library.scan(LIBRARY)
    design = Design("example.org:soc:top:1.0", library)
    design.add_ip("a", CLOCKED)
    design.connect_clk(design.add_port("in", "clk_i"))
    design.connect_rst(design.add_port("in", "rst_ni"))
    library.add(design.build_component())
    library.add(design.build())

    assert link(design.build(), library).diagnostics == []
