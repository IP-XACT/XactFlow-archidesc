# Python design description format

This document specifies a new `XactFlow` `Importer`: a way to describe a design's
architecture (which IPs it instantiates, how they're wired together over buses, any
ad hoc signal connections, and the ports and bus interfaces it offers to the outside) as a
plain Python script, instead of hand-written IP-XACT XML. Running the script produces an
`ipxact.Design` object and the `ipxact.Component` containing it, which the rest of
`XactFlow` already knows what to do with: `link()` resolves and checks the design against a
`Library`, and `XactFlow-design` and `XactFlow-component` can serialize them back out to
real IP-XACT `design` and `component` XML documents.

This builds on the design description concept from *IP-XACT-Based SoC Generation: Tools
and SoCMake Integration* (M. Travaillard, B. Denkinger, August 2026), specifically its
Section 4.3 and the `design2ipxact` transformation of Section 5.2. That transformation
scoped a full SoCMake-integrated pipeline (`sv2ipxact`, `design2ipxact`, `ipxact2sv`, a
shared bus library, radiation-hardening hooks, CMake integration); this document only
covers the piece of it that becomes a standalone `XactFlow` `Importer`, with no SoCMake
or CMake dependency, consistent with every other `XactFlow` plugin.

## Scope

What this importer does:

1. Execute a Python script that declares IP instances, top-level ports and bus
   interfaces, bus connections, and ad hoc connections using the API below.
2. Resolve each IP reference to a real `ipxact.Component` via a `Library` the script
   itself provides.
3. Build the corresponding `ipxact.Design` object: one `ComponentInstance` per IP
   instance, one `Interconnection` per bus connection (a `MonitorInterconnection` when one
   side is a monitor), one `AdHocConnection` per ad hoc connection. A top-level bus
   interface is reached with a `hierInterface`, a top-level port with an
   `externalPortReference`.
4. Build the `ipxact.Component` containing the design: its top-level ports and bus
   interfaces, and a view instantiating the design through a `designInstantiation`.

What it deliberately does not do, because `XactFlow` already does it elsewhere:

- **Bus protocol compatibility checking.** `connect_bus()` only matches interfaces by bus
  type and interface mode (see below); it does not check signal-level
  compatibility, that's `xactflow.link()`'s job, via its interconnection-compatibility
  SCR checks, run after this importer hands off the `Design`.
- **Address conflict checking, memory map validation.** Out of scope for the reasons
  in [Address mapping](#address-mapping) below.
- **Anything else IP-XACT's semantic consistency rules cover.** This importer's only job
  is producing a syntactically well-formed `Design`; whether that `Design` is
  semantically sound, correct cross-references, compatible interfaces, valid addressing,
  sound hierarchy, and so on, is `SCR`'s job, unchanged from how every other `Design`
  (hand-written or otherwise imported) is checked today.

## The `Design` object

```python
class Design:
    def __init__(self, vlnv: str, library: xactflow.Library, *, module_name: str | None = None) -> None: ...
    def add_ip(self, instance_name: str, vlnv: str, parameters: dict[str, object] | None = None, transforms: list[str] | None = None) -> IPInstance: ...
    def connect_bus(self, a: IPInstance | BusInterfaceRef, b: IPInstance | BusInterfaceRef, *, base_addr: int | None = None, size: int | None = None) -> None: ...
    def connect(self, source: PortRef, target: PortRef) -> None: ...
    def add_port(self, direction: str, name: str, width: int | None = None, *, connect: PortRef | None = None) -> PortRef: ...
    def add_bus(self, name: str, bus_type: str, mode: str, *, group: str | None = None, connect: IPInstance | BusInterfaceRef | None = None) -> BusInterfaceRef: ...
    def build(self) -> ipxact.Design: ...
    def build_component(self) -> ipxact.Component: ...
```

### `Design(vlnv, library, *, module_name=None)`

- `vlnv`: a colon-separated `"vendor:library:name:version"` string naming what the script
  describes. The two documents it produces need their own VLNVs: the component containing
  the design gets `.component` appended to the name, and the design `.design`, so
  `"example.org:soc:example:0.1"` gives `example.org:soc:example.component:0.1` and
  `example.org:soc:example.design:0.1`. Parsed eagerly; a malformed string (not exactly 4
  fields) raises `ValueError` at construction.
- `library`: an already-scanned `xactflow.Library`. The script builds this itself
  (`Library.scan(...)`) and passes it in, `Design` never constructs or owns a `Library`
  implicitly. This keeps the script fully self-contained.
- `module_name`: the name of the module the component stands for, written as the
  `moduleName` of its component instantiation, for an HDL generator to name the module
  after. It defaults to the name in `vlnv`, before `.component` or `.design` is appended:
  `example` for `"example.org:soc:example:0.1"`. It is not checked against the identifier
  rules of an HDL; a value that is not a `str` raises `TypeError`, an empty one
  `ValueError`.

### `add_ip(instance_name, vlnv, parameters=None, transforms=None) -> IPInstance`

Declares one component instance and resolves it immediately, not deferred to `build()`:

- `instance_name` must follow the IP-XACT naming rule: it starts with a letter or `_`,
  not a digit, followed by letters, digits, `_`, `.` or `-`. It must also be unique within
  the design. A duplicate raises `ValueError` immediately, naming both the new call and the
  earlier one that already used it.
- `vlnv` is resolved against `library` right away. An unresolvable VLNV raises
  `ValueError` at the `add_ip()` call site, not at the end of the script, so a mistake is
  reported on the line that made it.
- `parameters` maps a parameter's **name** (as declared on the resolved component) to an
  override value. This is IP-XACT's own `configurableElementValues` feature. For each
  entry, `add_ip` looks up the matching `Parameter` by name on the resolved component:
  - Unknown name: `ValueError`, naming the instance, the bad key, and the parameter names
    that *are* available on that component.
  - Name matches a parameter that cannot be overridden: `ValueError`. It needs a
    `parameterId` and `resolve="user"`. The standard says an `immediate` parameter (the
    default) cannot be modified through configuration. This is a defect in the
    referenced *component* file, not something this importer can paper over (see
    [Requirement on imported components](#requirement-on-imported-components)).
  - Empty or blank value, or a float that is not finite (`nan`, `inf`): `ValueError`,
    since it is not a usable expression.
  - Otherwise, the override is recorded against that parameter's `parameterId`.
- `transforms` names zero or more transformations to apply to this instance, radiation
  hardening being one example. This importer does not apply them. It only records the
  names, in order, as a vendor extension on the resulting component instance.

  A separate tool, a transformer, does the work later. It takes the `ipxact.Design`,
  applies the transformations its instances ask for, and returns the modified
  `ipxact.Design`, ready for linking. A transformer can be kept private, and can be
  turned on or off without changing the design description.

Returns an `IPInstance` handle (see below) used to build bus and ad hoc connections
against this instance.

#### `IPInstance`

A handle returned by `add_ip()`, carrying the instance name and the resolved
`ipxact.Component`.

```python
class IPInstance:
    name: str
    component: ipxact.Component

    def port(self, name: str) -> PortRef: ...
    def bus(self, name: str) -> BusInterfaceRef: ...
```

- `.port(name)` looks up `name` in `component.model.ports`; unknown name raises
  `ValueError` listing the ports that do exist. Used to build ad hoc connections.
- `.bus(name)` looks up `name` in `component.bus_interfaces`; unknown name raises
  `ValueError` the same way. Used only when `connect_bus()` needs help picking the right
  bus (see below).
- `instance.some_port` is a shorthand for `instance.port("some_port")`. If a port has
  the same name as an `IPInstance` attribute, such as `name`, use `.port(...)` instead.

### `connect_bus(a, b, *, base_addr=None, size=None)`

Connects one bus interface on `a` to one bus interface on `b`. Each argument is either a
plain `IPInstance` (pick one of its bus interfaces automatically) or an
`IPInstance.bus(name)` (use that exact one). Here is how it picks:

1. For each side, the candidates are: the exact interface given, if a `bus(name)` was
   passed; otherwise every initiator or target interface on that instance's component
   that an earlier `connect_bus()` call has not already used.
2. A pair `(interface_a, interface_b)` matches if they share the same bus type, and
   their `mode`s are opposite. The same bus type means the same bus definition, or one
   that `extends` the other, directly or not (SCR 2.2), and the same parameter values set
   on it (`bus_type.config_element_values`), compared after evaluation. The
   modes that connect are the ones the standard allows: initiator with target or
   mirrored initiator, mirrored target with target, and system with mirrored system of
   the same `group`. A monitor interface matches an interface of the mode it monitors,
   of the same `group` when it monitors a system or mirrored-system interface.
   Only initiators and targets are picked automatically, the other modes need the exact,
   named call. There is no need to say which side is the initiator and which is the
   target: each side's mode is already set on its own component, so this importer reads
   it instead of asking the script to repeat it.
3. If exactly one matching pair exists, it is used.
4. If more than one exists, `connect_bus` raises `ValueError` listing every candidate
   interface name on both sides. The caller must then say exactly which one to use, by
   passing `a.bus("...")`/`b.bus("...")` for at least one side. A multi-port interconnect
   (several identical initiator interfaces, one per attached peripheral) will usually hit
   this case; it is expected, not an edge case.
5. If none matches, `ValueError` explains why (no shared bus definition, no opposite mode
   available, different system groups, or every candidate already used) and lists what
   bus interfaces are still free on each side.

On success, one `ipxact.Interconnection` is added to the design, with `active_interface`
set from `a`'s side and `other_active_interfaces=[<b's side>]`. Its name always follows
the same pattern, `<instance a>_to_<instance b>`, built from the two instance names even
when an interface is named with `.bus(...)` (a number is added at the end if that name is
already taken, e.g. a second link between the same two instances). Both interfaces are
now marked used, so a later `connect_bus()` call will not reuse them.

When one side is a monitor, an `ipxact.MonitorInterconnection` is added instead. Only the
monitor interface is marked used: the interface it watches stays free for a normal
connection.

#### Address mapping

`base_addr`/`size` are a convenience for the common case: a target sitting at some offset
on an addressable bus. IP-XACT actually handles this through `addressSpace`/`segment` on
the initiator side, resolved through a `designConfiguration`, but `XactFlow` doesn't
apply that yet (see the main `XactFlow`
[README](https://github.com/IP-XACT/XactFlow#known-limitations)). Doing this the real way
is future work, once that support exists. Until then,
when either is given, they're recorded as a vendor extension on the *target* side of the
connection (a target or mirrored initiator interface). A connection with no such side,
like a system or monitor one, raises `ValueError`. This is the same way IP-XACT
represents anything the standard doesn't cover and isn't modeled here yet. Concretely,
something to the effect of:

```xml
<xactflow:addressMapping baseAddr="0x40010000" size="0x1000"/>
```

is appended to that interface's `vendor_extensions`. This is a placeholder, not a
standards-compliant addressing mechanism; anything reading it back needs to know to look
for it specifically. It should be revisited (and likely replaced) once `XactFlow`
applies `DesignConfiguration`/`addressSpace` for real.

### `connect(source, target)`

An ad hoc, signal-level connection between exactly two ports, each a `PortRef` (from
`.port(name)` or the attribute shorthand). Unlike `connect_bus`, which leaves signal-level
compatibility to `link()`, `connect` checks the ports itself, since the check is cheap
and both ports are already in hand:

- Both ports must belong to this design: to its instances, or be its top-level ports
  (see [`add_port`](#add_portdirection-name-widthnone--connectnone---portref)).
- Directions must be compatible: one `out` and one `in`, or either side `inout`. Two
  `out`s or two `in`s raise `ValueError` naming both ports and their directions.
- A port cannot be connected to itself, and the same two ports cannot be connected twice.
- An `in` port can only be driven by one connection.

On success, appends one `ipxact.AdHocConnection` with a single `InternalPortReference`
pair (`component_instance_ref`/`port_ref` for each side), named
`f"{source.instance.name}_{source.name}_to_{target.instance.name}_{target.name}"`
(a number is added at the end if that name is already taken). A top-level port is
written as an `ExternalPortReference` instead, and named by its own name alone.
Bit-slice or struct-sub-port ad hoc connections (`SubPortReference`/`PartSelect` in
`ipxact-compiler`'s model) are out of scope for this first version; `connect()` only
wires whole ports to whole ports.

### `add_port(direction, name, width=None, *, connect=None) -> PortRef`

Declares one top-level wire port: a port of the component containing the design, which
the design reaches with an `externalPortReference`.

- `direction` is `"in"`, `"out"` or `"inout"`, seen from outside the component. Anything
  else, `phantom` included, raises `ValueError`.
- `name` follows the same naming rule as an instance name. It must be unique among the
  top-level ports, including the ones `add_bus` creates; a duplicate raises `ValueError`
  naming the call that added the first one.
- `width` is `None` for a single bit, or a number of bits `N` for an `[N-1:0]` vector.
- `connect`, if given, is a `PortRef` connected to the new port right away, exactly as
  `connect(port, connect)` would.

Returns a `PortRef` with no instance, which `connect()` takes like an instance port.
Inside the design, a top-level port works the other way round from an instance port: a
top-level `in` port drives the instance ports it connects to, and a top-level `out` port is
driven by them. So `connect()` pairs a top-level port with an instance port of the same
direction, or an `inout` one, and drives a top-level `out` port only once. A top-level
`in` port can drive several instance ports, one `connect()` call each. Two top-level ports
cannot be connected together.

### `add_bus(name, bus_type, mode, *, group=None, connect=None) -> BusInterfaceRef`

Declares one top-level bus interface: a bus interface of the component containing the
design, which the design reaches with a `hierInterface`.

- `name` follows the same naming rule, and must be unique among the top-level bus
  interfaces.
- `bus_type` is the VLNV of a bus definition, resolved against `library` right away, like
  `add_ip`'s `vlnv`.
- `mode` is the interface mode, as IP-XACT writes it: `"initiator"`, `"target"`,
  `"mirroredInitiator"`, `"mirroredTarget"`, `"system"` or `"mirroredSystem"`. A
  `"monitor"` has nothing to watch at the top level, and raises `ValueError`.
- `group` is the system group: needed by a `system` or `mirroredSystem` mode, refused by
  the others.
- `connect`, if given, is connected to the new interface right away, exactly as
  `connect_bus(interface, connect)` would.

Returns a `BusInterfaceRef` with no instance, which `connect_bus()` takes like an
instance's `.bus(name)`. It pairs it with an instance interface of the same bus type and
the **same** mode, as the standard requires of a hierarchical connection (SCR 2.4 to
2.10): a top-level target is an internal target, seen from outside. The instance side can
still be picked automatically. Two top-level interfaces cannot be connected together, and
`base_addr`/`size` raise `ValueError`, since the address map of a top-level interface
belongs to the connection outside the component. The `Interconnection` has the instance
side as its `activeInterface` and the top-level interface as a `hierInterface`, and is
named from the top-level interface name and the instance name, e.g. `ext_if_to_apb_bus`.
A top-level interface is connected only once.

A top-level interface takes its ports from the internal interface it connects to: the
abstraction types and port maps are copied, and each mapped port becomes a top-level port
named `<interface>_<logical port>`, a copy of the internal one (same direction, same
width). `ext_if` connected to an interface mapping the logical ports `CLK` and `DATA`
gives the top-level ports `ext_if_CLK` and `ext_if_DATA`. A name already taken by another
top-level port raises `ValueError`.

### `build() -> ipxact.Design`

Returns the finished `ipxact.Design`: every `add_ip` as a `ComponentInstance` (in call
order), every `connect_bus` as an `Interconnection` (or a `MonitorInterconnection` for a
monitor), every `connect` as an `AdHocConnection`. It does not check that every bus interface got connected: an
unconnected optional interface (e.g. an unused debug port) is a legitimate, common
design, not an error. Completeness is `SCR`'s concern, if and when a rule for it exists,
not this importer's.

### `build_component() -> ipxact.Component`

Returns the `ipxact.Component` containing the design: every top-level port, those of
`add_port` and those `add_bus` created, every `add_bus` interface, and one view,
`hierarchical`. The view's `designInstantiation` (`hierarchical_design`) refers to the
design, and its `componentInstantiation` (`hierarchical_rtl`) names the module,
`module_name`. `xactflow.link()` finds the component through that view, and checks the
`hierInterface` and `externalPortReference` elements against it. A script with no
top-level port or bus interface still gets this component, with the view and its two
instantiations only.

Scripts don't call `build()` or `build_component()` themselves. The importer does, after
running the script, see below.

## How the importer runs a script

```python
class ArchiDescImporter(xactflow.Importer):
    def import_(self, source: Path, library: xactflow.Library, **options) -> None: ...
```

It executes `source` as a plain Python file (`runpy.run_path`), then looks for a
module-level variable named `design`, of type `Design`. Missing or wrong-typed `design`
raises `ValueError` naming what was expected. It then calls `.build_component()` and
`.build()` on it and adds both results to `library` as targets
(`library.add(document, source, replace=True, target=True)`), which is what an exporter
writes. They are regenerated from the script on every run, so they replace the documents
with the same VLNVs already in `library`, e.g. its previous output scanned from a `--lib`
folder. The script still builds its own `Design` on its own `Library`: `library` is where
the results go, and where an exporter run after the importer looks up the components the
design instantiates.

## Worked example

```python
from xactflow import Library, link
from xactflow_archidesc import Design

library = Library.scan("path/to/ip/library")
design = Design("cern.ch:soc:example:0.1", library)

cpu   = design.add_ip("cpu",   "openhwgroup:core:ibex:1.0", transforms=["tmrg"])
uart  = design.add_ip("uart",  "cern.ch:ip:uart:1.0", parameters={"BAUDRATE": 115200})
timer = design.add_ip("timer", "pulp:ip:timer:1.0")
debug = design.add_ip("debug", "cern.ch:ip:debug_module:1.0")
bus   = design.add_ip("apb_bus", "cern.socgen:ip:apb_rt:0.1.0")

design.connect_bus(cpu, bus.bus("s0"))
design.connect_bus(bus.bus("m0"), uart,  base_addr=0x4001_0000, size=0x1000)
design.connect_bus(bus.bus("m1"), timer, base_addr=0x4002_0000, size=0x1000)

design.connect(debug.debug_req_o, cpu.debug_req_i)
design.connect(cpu.debug_resp_o, debug.debug_resp_i)

design.add_port("inout", "jtag_io", connect=debug.jtag_io)
design.add_port("out", "irq_o", connect=uart.irq_o)

library.add(design.build_component())
linked = link(design.build(), library)
```

The interconnect has several identical ports on each side, so each `connect_bus` call
names the one to use with `.bus(name)`.

`design.build()` produces the `ipxact.Design` `cern.ch:soc:example.design:0.1`: 5
`ComponentInstance`s, 3 `Interconnection`s (`cpu_to_apb_bus`, `apb_bus_to_uart`,
`apb_bus_to_timer`, one per `connect_bus` call), and 4 `AdHocConnection`s, 2 of them to
the top-level ports. `design.build_component()` produces the `ipxact.Component`
`cern.ch:soc:example.component:0.1` containing it, with the top-level ports `jtag_io` and
`irq_o`. With that component added to the same `library`, `link()` resolves and checks the
design exactly as it would a hand-written or hand-exported one, top-level ports included,
all in this one script, with no separate importer step needed. Running the same script
through `ArchiDescImporter` instead (letting the CLI or another tool drive it) does the
same `build_component()` and `build()` calls automatically and adds both results to the
library it is given; linking is then up to whatever exporter reads that library, not shown
here.

## Requirement on imported components

`add_ip`'s `parameters` argument only works for a component whose parameters carry a
`parameterId`; that's what `config_element_values` keys by. Any component supplied to
this format, whether produced by `XactFlow-sv`, another importer, or hand-written
IP-XACT, needs `parameterId` and `resolve="user"` set on each parameter meant to be
overridable this way. One with no `parameterId` cannot be referenced for override at all,
and one with `resolve="immediate"` cannot be modified through configuration, per the
standard. This is a general requirement on the components this format consumes, not
something specific to one importer.

## Open questions, deferred rather than silently resolved

Things this first version leaves open, on purpose:

- **Hierarchical design description.** A sub-design instantiated as a component inside a
  bigger one is not addressed here; this format describes one level: a design, and the
  top-level ports and bus interfaces of the component containing it. `XactFlow`'s own
  linker doesn't recurse into nested designs yet either
  (see its README's "One level of hierarchy" limitation), so there is nothing
  downstream to hand a hierarchical result to yet regardless.
- **Address mapping**, as above: a real system, not a vendor-extension placeholder, once
  `XactFlow` applies `DesignConfiguration`/`addressSpace`.
- **Top-level ports copied from part of a port**: a top-level bus interface copies whole
  internal ports. A port map onto part of an internal port (a `partSelect` or sub-port)
  would need a top-level port of just that part, and is refused until that is designed.
  A copied port also keeps its width as written, which can refer to a parameter of the
  internal component that the component containing the design does not have; that needs
  the width evaluated first.
- **Protocol bridges** (e.g. an APB-to-OBI adapter between two incompatible bus
  segments): not addressed. `connect_bus` either finds a matching pair or fails; nothing
  here automatically adds a bridge component. A bridge, if one exists as a real
  component, is just another `add_ip` the script author wires in explicitly on both
  sides, sourced and placed by the user, not made automatically by the tool.
- **System/mirrored/monitor interface modes**: real IP-XACT concepts, deliberately not
  auto-matched by `connect_bus` in this first version (see above).
- **Bus type parameter values**: `connect_bus` compares the parameter values set on each
  side's bus type (a data width, for example), not just the bus VLNV. These values are
  raw expressions (`"32"` and `"0x20"` are equal, a value can refer to an instance
  parameter), so they have to be evaluated before they can be compared. This first
  version only compares the bus VLNV, until `XactFlow` evaluates parameter values well
  enough to do it.
