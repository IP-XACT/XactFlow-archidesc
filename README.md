# xactflow-archidesc

A [XactFlow](https://github.com/IP-XACT/XactFlow) importer plugin that turns a design described
in a Python script (which IPs it uses, and how they are connected) into an `ipxact.Design` object,
built on [`ipxact-compiler`](https://github.com/IP-XACT/ipxact-compiler)'s
[IEEE 1685-2022](https://standards.ieee.org/ieee/1685/10307/) object model.

IP-XACT `design` files are not made to be handwritten. With this package, a short script lists
the IP instances, the bus connections between them, and any direct signal connections. The
importer runs the script and returns the `Design`, which XactFlow's `elaborate()` can then check
against the IP-XACT rules.

The full description of the format is in
[`doc/python-design-description.md`](doc/python-design-description.md). It describes the tool
as it is meant to work once finished, so the current version can differ from it in small ways.
The differences known today are listed in [Known limitations](#known-limitations).

## Installation

```bash
pip install xactflow-archidesc
```

For local development, `requirements-dev.txt` overrides `ipxact-compiler`, `xactflow` and
`xactflow-design` to local installations. It can be modified to adapt the paths or to keep any
of them on PyPI.

```bash
pip install -r requirements-dev.txt -e .
```

Requires Python >= 3.9.

## Usage

A design script builds a `Design` from an IP library and keeps it in a module-level variable
named `design`:

```python
from pathlib import Path

from xactflow import Library
from xactflow_archidesc import Design

library = Library.scan(Path(__file__).parent / "ip")
design = Design("example.org:soc:example:0.1", library)

cpu = design.add_ip("cpu", "example.org:ip:cpu:1.0", transforms=["tmrg"])
uart = design.add_ip("uart", "example.org:ip:uart:1.0", parameters={"BAUDRATE": 115200})
timer = design.add_ip("timer", "example.org:ip:timer:1.0")
debug = design.add_ip("debug", "example.org:ip:debug_module:1.0")
bus = design.add_ip("bus", "example.org:ip:interconnect:1.0")

design.connect_bus(cpu, bus)
design.connect_bus(bus.bus("m0"), uart, base_addr=0x4001_0000, size=0x1000)
design.connect_bus(bus, timer, base_addr=0x4002_0000, size=0x1000)

design.connect(debug.debug_req_o, cpu.debug_req_i)
design.connect(cpu.debug_resp_o, debug.debug_resp_i)
```

A runnable version of this script, with its IP library, is in
[`tests/fixtures/soc.py`](tests/fixtures/soc.py).

In short:

- `add_ip(name, vlnv, ...)` adds an IP instance, with optional `parameters` overrides and
  `transforms`.
- `connect_bus(a, b, ...)` connects a bus interface of `a` to one of `b`. It picks them by
  itself, or uses the one named with `instance.bus("name")`. `base_addr`/`size` place a target
  in the address map.
- `connect(source, target)` connects two ports. `instance.some_port` is short for
  `instance.port("some_port")`.

Each call checks its arguments right away, so a mistake is reported on the line that made it.
All the details are in [the format description](doc/python-design-description.md#the-design-object).

Once installed, the importer registers itself under the `xactflow.importers` entry point group
as `archidesc`, so it runs as a XactFlow CLI command. With `xactflow-design` installed too,
`--then` hands the result straight to that exporter. To try it on the example of this repo,
create a virtual environment and install the package in it first. The `xactflow` command only
exists inside that environment, so activate it again in each new terminal:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
xactflow archidesc tests/fixtures/soc.py --then design --output out/
```

This writes `out/example.xml`, named after the design's VLNV name.

Or as a library, for example to elaborate the design first:

```python
from pathlib import Path

from xactflow import Library, elaborate
from xactflow_archidesc import ArchiDescImporter
from xactflow_design import DesignExporter

design = ArchiDescImporter().import_(Path("soc.py"))
elaborated = elaborate(design, Library.scan("ip"))
DesignExporter().export(design, Path("out"))
```

The script runs like `python soc.py` would: a helper module next to it can be imported, and a
relative path in it is read from the current folder. `Path(__file__).parent` gives paths
that do not depend on where the command is run from. The importer takes no options.

## Design notes

- **Vendor extensions.** Two things this format records have no place in IP-XACT, so they are
  written as vendor extensions in the `https://github.com/IP-XACT/XactFlow` namespace:
  `<xactflow:transforms>` on a component instance, listing the transformations in order, and
  `<xactflow:addressMapping baseAddr="..." size="..."/>` on the target side of a bus
  connection (a target or mirrored initiator interface).
- **Transformations are not applied here.** A separate tool, a transformer, reads
  `<xactflow:transforms>`, applies the transformations and returns the changed `Design`.
- **Parameter overrides follow the standard.** Only a parameter with a `parameterId` and
  `resolve="user"` can be overridden. The standard says an `immediate` parameter (the default)
  cannot be changed through configuration.
- **Connection names are unique.** A bus connection is named `<a>_to_<b>` and a port connection
  `<instance>_<port>_to_<instance>_<port>`. A number is added when a name is already taken, by
  any kind of connection.
- **Monitors.** Connecting a monitor interface adds a `monitorInterconnection`. The interface it
  watches stays free for a normal connection.

## Known limitations

- `connect_bus` compares the bus VLNV of the two interfaces, but not the parameter values set
  on the bus type (a data width, for example). Comparing them needs XactFlow to evaluate
  those values first. This should be changed in a future update.
- XactFlow's `elaborate()` does not use the parameter overrides from `add_ip` yet. They are
  written to the design and exported, but not applied during elaboration.
- `connect` checks direction only for wire ports. Two transactional or two structured ports
  are connected with no further check, only a mix of the two kinds is refused.
- `connect` does not compare port widths.
- A design describes one level only. Hierarchical designs, a real address mapping (instead of
  the `addressMapping` placeholder) and protocol bridges are not handled yet, see the open
  questions at the end of [the format description](doc/python-design-description.md).

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt -e .
pytest
```

The tests use a small IP library in `tests/fixtures/library`. `tests/test_end_to_end.py` runs
the example script through the importer, elaborates the result, exports it with
`xactflow-design` and reads the file back.

## License

LGPL-3.0. See [LICENSE](LICENSE).
