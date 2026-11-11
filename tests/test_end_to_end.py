"""The full flow on the worked example: run the script through the importer into a library,
link the result against it, export the design and the component containing it to IP-XACT files,
and read them back."""

from dataclasses import fields, is_dataclass
from pathlib import Path
from xml.etree.ElementTree import canonicalize

import ipxact
from xactflow import Library, link
from xactflow_component import ComponentExporter
from xactflow_design import DesignExporter

from xactflow_archidesc import ArchiDescImporter

SCRIPT = Path(__file__).parent / "fixtures" / "soc.py"


def _canonical(value):
    """The value as plain lists and dicts, with every vendor extension in canonical form.

    Reading a file back adds the namespaces in scope to each vendor extension string, which
    does not change what it says.
    """
    if is_dataclass(value):
        return {
            f.name: [canonicalize(e) for e in value.vendor_extensions]
            if f.name == "vendor_extensions"
            else _canonical(getattr(value, f.name))
            for f in fields(value)
        }
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    return value


def test_worked_example_imports_links_exports_and_reads_back(tmp_path):
    library = Library.scan(SCRIPT.parent / "library")
    ArchiDescImporter().import_(SCRIPT, library)
    [component, design] = library.targets()

    assert [i.instance_name for i in design.component_instances] == ["cpu", "uart", "timer", "debug", "bus"]
    assert [i.name for i in design.interconnections] == ["cpu_to_bus", "bus_to_uart", "bus_to_timer"]
    assert [c.name for c in design.ad_hoc_connections] == [
        "debug_debug_req_o_to_cpu_debug_req_i",
        "cpu_debug_resp_o_to_debug_debug_resp_i",
        "jtag_io_to_debug_jtag_io",
        "irq_o_to_uart_irq_o",
    ]
    assert [port.name for port in component.model.ports] == ["jtag_io", "irq_o"]

    linked = link(design, library)
    assert linked.diagnostics == []
    assert linked.component is component
    assert len(linked.instances) == 5
    assert len(linked.interconnections) == 3
    assert len(linked.ad_hoc_external_ports) == 2

    DesignExporter().export(library, tmp_path)
    ComponentExporter().export(library, tmp_path)
    for document in (design, component):
        path = tmp_path / f"example.org_soc_{document.vlnv.name}_0.1.xml"
        assert _canonical(ipxact.parse_file(path)) == _canonical(document)
