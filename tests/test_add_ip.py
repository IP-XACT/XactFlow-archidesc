import inspect
from xml.etree import ElementTree

import ipxact
import pytest
from xactflow import Library

from xactflow_archidesc import Design, IPInstance
from xactflow_archidesc.design import XACTFLOW_NAMESPACE

UART = "example.org:ip:uart:1.0"
CPU = "example.org:ip:cpu:1.0"


def _line() -> int:
    """Line number of the caller, to check the file:line in error messages."""
    return inspect.currentframe().f_back.f_lineno


@pytest.fixture
def design(library):
    return Design("example.org:soc:top:1.0", library)


def test_design_vlnv_is_parsed(design):
    assert design.build().vlnv == ipxact.VLNV("example.org", "soc", "top", "1.0")


def test_design_rejects_malformed_vlnv(library):
    with pytest.raises(ValueError, match="vendor:library:name:version"):
        Design("example.org:soc:top", library)


def test_add_ip_returns_handle_on_resolved_component(design):
    uart = design.add_ip("uart", UART)

    assert isinstance(uart, IPInstance)
    assert uart.name == "uart"
    assert uart.component.vlnv == ipxact.VLNV("example.org", "ip", "uart", "1.0")


def test_build_has_one_component_instance_per_add_ip_in_call_order(design):
    design.add_ip("uart", UART)
    design.add_ip("cpu", CPU)
    design.add_ip("uart2", UART)

    instances = design.build().component_instances
    assert [i.instance_name for i in instances] == ["uart", "cpu", "uart2"]
    assert str(instances[1].component_ref) == CPU


def test_add_ip_rejects_duplicate_name_and_names_both_calls(design):
    first_line = _line() + 1
    design.add_ip("uart", UART)
    second_line = _line() + 2
    with pytest.raises(ValueError) as exc_info:
        design.add_ip("uart", "example.org:ip:timer:1.0")

    message = str(exc_info.value)
    assert f"{__file__}:{first_line}" in message
    assert f"{__file__}:{second_line}" in message


def test_add_ip_rejects_unknown_vlnv(design):
    with pytest.raises(ValueError, match="no document with VLNV example.org:ip:nope:1.0 in the library$"):
        design.add_ip("x", "example.org:ip:nope:1.0")


def test_unknown_vlnv_points_at_library_diagnostics_when_there_are_some(tmp_path):
    # A bus interface with no mode makes the parser fail, so the library records a diagnostic.
    (tmp_path / "broken.xml").write_text(
        '<ipxact:component xmlns:ipxact="http://www.accellera.org/XMLSchema/IPXACT/1685-2022">'
        "<ipxact:busInterfaces><ipxact:busInterface><ipxact:name>b</ipxact:name>"
        "</ipxact:busInterface></ipxact:busInterfaces></ipxact:component>"
    )

    with pytest.raises(ValueError, match="library.diagnostics has 1 entries"):
        Design("example.org:soc:top:1.0", Library.scan(tmp_path)).add_ip("x", "example.org:ip:nope:1.0")


@pytest.mark.parametrize(
    "args, kwargs, error",
    [
        (("2uart", UART), {}, ValueError),
        ((None, UART), {}, TypeError),
        (("x", "example.org:ip:uart"), {}, ValueError),
        (("x", "example.org:bus:simplebus:1.0"), {}, ValueError),  # a bus definition, not a component
        (("x", UART), {"parameters": [("BAUDRATE", 1)]}, TypeError),
        (("x", UART), {"parameters": {"BAUDRATE": None}}, TypeError),
        (("x", UART), {"transforms": "tmrg"}, TypeError),
    ],
)
def test_add_ip_rejects_bad_arguments_and_leaves_design_unchanged(design, args, kwargs, error):
    with pytest.raises(error):
        design.add_ip(*args, **kwargs)

    # Nothing was recorded, so the same name can still be used.
    design.add_ip("x", UART)
    assert [i.instance_name for i in design.build().component_instances] == ["x"]


@pytest.mark.parametrize("value, expression", [(115200, "115200"), (True, "true"), ("2*WIDTH", "2*WIDTH")])
def test_add_ip_records_parameter_override_by_parameter_id(design, value, expression):
    design.add_ip("uart", UART, parameters={"BAUDRATE": value})

    ref = design.build().component_instances[0].component_ref
    assert ref.config_element_values == {"BAUDRATE": expression}


def test_add_ip_rejects_unknown_parameter_and_lists_available_ones(design):
    with pytest.raises(ValueError, match="no parameter 'XLENN'. Available parameters: XLEN, NO_ID, FIXED"):
        design.add_ip("cpu", CPU, parameters={"XLENN": 64})


@pytest.mark.parametrize(
    "name, details",
    [("NO_ID", "parameterId=None, resolve='user'"), ("FIXED", "parameterId='FIXED', resolve='immediate'")],
)
def test_add_ip_rejects_parameter_that_cannot_be_overridden(design, name, details):
    with pytest.raises(ValueError, match=f"parameter '{name}' .* cannot be overridden") as exc_info:
        design.add_ip("cpu", CPU, parameters={name: 1})

    assert details in str(exc_info.value)


@pytest.mark.parametrize("transforms, recorded", [(None, []), (["tmrg", "a\"b<c"], ["tmrg", "a\"b<c"])])
def test_add_ip_records_transforms_as_vendor_extension(design, transforms, recorded):
    design.add_ip("cpu", CPU, transforms=transforms)

    [instance] = design.build().component_instances
    roots = [ElementTree.fromstring(extension) for extension in instance.vendor_extensions]
    assert [root.tag for root in roots] == ([f"{{{XACTFLOW_NAMESPACE}}}transforms"] if recorded else [])
    assert [item.get("name") for root in roots for item in root] == recorded
