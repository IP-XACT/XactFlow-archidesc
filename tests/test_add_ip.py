import inspect
from xml.etree import ElementTree

import ipxact
import pytest

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
    assert design.vlnv == ipxact.VLNV("example.org", "soc", "top", "1.0")
    assert design.build().vlnv == design.vlnv


@pytest.mark.parametrize(
    "vlnv, error",
    [
        ("example.org:soc:top", ValueError),
        ("a:b:c:d:e", ValueError),
        ("example.org::top:1.0", ValueError),
        ("", ValueError),
        (None, TypeError),
    ],
)
def test_design_rejects_bad_vlnv(library, vlnv, error):
    with pytest.raises(error):
        Design(vlnv, library)


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
    assert "'uart'" in message
    assert f"{__file__}:{first_line}" in message
    assert f"{__file__}:{second_line}" in message


def test_add_ip_rejects_unknown_vlnv(design):
    with pytest.raises(ValueError, match="no document with VLNV example.org:ip:nope:1.0"):
        design.add_ip("x", "example.org:ip:nope:1.0")


def test_add_ip_rejects_vlnv_that_is_not_a_component(design):
    with pytest.raises(ValueError, match="BusDefinition, not a Component"):
        design.add_ip("x", "example.org:bus:simplebus:1.0")


@pytest.mark.parametrize(
    "args, kwargs, error",
    [
        (("", UART), {}, ValueError),
        (("2uart", UART), {}, ValueError),
        (("my uart", UART), {}, ValueError),
        ((None, UART), {}, TypeError),
        (("x", "example.org:ip:uart"), {}, ValueError),
        (("x", "example.org::uart:1.0"), {}, ValueError),
        (("x", None), {}, TypeError),
        (("x", UART), {"parameters": [("BAUDRATE", 1)]}, TypeError),
        (("x", UART), {"parameters": {"BAUDRATE": None}}, TypeError),
        (("x", UART), {"transforms": "tmrg"}, TypeError),
        (("x", UART), {"transforms": ["tmrg", ""]}, TypeError),
        (("x", UART), {"transforms": [1]}, TypeError),
    ],
)
def test_add_ip_rejects_bad_arguments_and_leaves_design_unchanged(design, args, kwargs, error):
    with pytest.raises(error):
        design.add_ip(*args, **kwargs)

    # Nothing was recorded, so the same name can still be used.
    design.add_ip("x", UART)
    assert [i.instance_name for i in design.build().component_instances] == ["x"]


@pytest.mark.parametrize(
    "value, expression",
    [(115200, "115200"), (1.5, "1.5"), (True, "true"), (False, "false"), ("2*WIDTH", "2*WIDTH")],
)
def test_add_ip_records_parameter_override_by_parameter_id(design, value, expression):
    design.add_ip("uart", UART, parameters={"BAUDRATE": value})

    ref = design.build().component_instances[0].component_ref
    assert ref.config_element_values == {"BAUDRATE": expression}


def test_parameter_overrides_do_not_leak_between_instances(design):
    design.add_ip("uart0", UART, parameters={"BAUDRATE": 115200})
    design.add_ip("uart1", UART)

    refs = [i.component_ref for i in design.build().component_instances]
    assert refs[0].config_element_values == {"BAUDRATE": "115200"}
    assert refs[1].config_element_values == {}


def test_add_ip_rejects_unknown_parameter_and_lists_available_ones(design):
    with pytest.raises(ValueError) as exc_info:
        design.add_ip("cpu", CPU, parameters={"XLENN": 64})

    message = str(exc_info.value)
    assert "'cpu'" in message
    assert "'XLENN'" in message
    assert "Available parameters: XLEN, NO_ID" in message


def test_add_ip_rejects_parameter_without_parameter_id(design):
    with pytest.raises(ValueError, match="parameter 'NO_ID' .* has no parameterId"):
        design.add_ip("cpu", CPU, parameters={"NO_ID": 1})


def _recorded_transforms(vendor_extensions):
    """Read the transform names back, the way a transformer would."""
    roots = [ElementTree.fromstring(extension) for extension in vendor_extensions]
    assert all(root.tag == f"{{{XACTFLOW_NAMESPACE}}}transforms" for root in roots)
    assert all(item.tag == f"{{{XACTFLOW_NAMESPACE}}}transform" for root in roots for item in root)
    return [item.get("name") for root in roots for item in root]


@pytest.mark.parametrize(
    "transforms, recorded",
    [(None, []), ([], []), (["tmrg"], ["tmrg"]), (["tmrg", "scan", "a\"b<c"], ["tmrg", "scan", "a\"b<c"])],
)
def test_add_ip_records_transforms_as_vendor_extension(design, transforms, recorded):
    design.add_ip("cpu", CPU, transforms=transforms)

    [instance] = design.build().component_instances
    # At most one extension, holding every name in order.
    assert len(instance.vendor_extensions) == (1 if recorded else 0)
    assert _recorded_transforms(instance.vendor_extensions) == recorded
