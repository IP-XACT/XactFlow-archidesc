"""The worked example of doc/python-design-description.md, written on the test fixture library."""

from pathlib import Path

from xactflow import Library

from xactflow_archidesc import Design

library = Library.scan(Path(__file__).parent / "library")
design = Design("example.org:soc:example:0.1", library)

cpu = design.add_ip("cpu", "example.org:ip:cpu:1.0", transforms=["tmrg"])
uart = design.add_ip("uart", "example.org:ip:uart:1.0", parameters={"BAUDRATE": 115200})
timer = design.add_ip("timer", "example.org:ip:timer:1.0")
debug = design.add_ip("debug", "example.org:ip:debug_module:1.0")
bus = design.add_ip("bus", "example.org:ip:interconnect:1.0")

design.connect_bus(cpu, bus)
# The interconnect has two free initiators, so the first link names the one it uses. The second
# link takes the only one left.
design.connect_bus(bus.bus("m0"), uart, base_addr=0x4001_0000, size=0x1000)
design.connect_bus(bus, timer, base_addr=0x4002_0000, size=0x1000)

design.connect(debug.debug_req_o, cpu.debug_req_i)
design.connect(cpu.debug_resp_o, debug.debug_resp_i)
