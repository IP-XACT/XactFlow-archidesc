"""Checks on the test fixture library itself, so a broken fixture fails here and not deep inside
some other test."""

import ipxact


def test_fixture_library_scans_with_no_diagnostics(library):
    assert library.diagnostics == []


def test_fixture_components_parse_as_expected(library):
    cpu = library.get_component(ipxact.VLNV.parse("example.org:ip:cpu:1.0"))
    assert [p.parameter_id for p in cpu.parameters] == ["XLEN", None]
    assert [(b.name, b.mode) for b in cpu.bus_interfaces] == [("bus_if", ipxact.InterfaceMode.INITIATOR)]

    interconnect = library.get_component(ipxact.VLNV.parse("example.org:ip:interconnect:1.0"))
    assert [(b.name, b.mode) for b in interconnect.bus_interfaces] == [
        ("s_if", ipxact.InterfaceMode.TARGET),
        ("m0", ipxact.InterfaceMode.INITIATOR),
        ("m1", ipxact.InterfaceMode.INITIATOR),
    ]

    debug = library.get_component(ipxact.VLNV.parse("example.org:ip:debug_module:1.0"))
    assert debug.bus_interfaces == []
    assert {p.name: p.wire.direction for p in debug.model.ports}["jtag_io"] == "inout"
