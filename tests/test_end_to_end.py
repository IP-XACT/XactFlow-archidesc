"""The full flow on the worked example: run the script through the importer, link the
result, export it to an IP-XACT design file, and read that file back."""

from dataclasses import fields, is_dataclass
from pathlib import Path
from xml.etree.ElementTree import canonicalize

import ipxact
from xactflow import link
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


def test_worked_example_imports_links_exports_and_reads_back(library, tmp_path):
    design = ArchiDescImporter().import_(SCRIPT)

    assert [i.instance_name for i in design.component_instances] == ["cpu", "uart", "timer", "debug", "bus"]
    assert [i.name for i in design.interconnections] == ["cpu_to_bus", "bus_to_uart", "bus_to_timer"]
    assert [c.name for c in design.ad_hoc_connections] == [
        "debug_debug_req_o_to_cpu_debug_req_i",
        "cpu_debug_resp_o_to_debug_debug_resp_i",
    ]

    linked = link(design, library)
    assert linked.diagnostics == []
    assert len(linked.instances) == 5
    assert len(linked.interconnections) == 3

    DesignExporter().export(design, tmp_path)
    assert _canonical(ipxact.parse_file(tmp_path / "example.xml")) == _canonical(design)
