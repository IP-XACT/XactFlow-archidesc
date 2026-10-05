# Python design description format

This document specifies a new `XactFlow` `Importer`: a way to describe a design's
architecture (which IPs it instantiates, how they're wired together over buses, and any
ad hoc signal connections) as a plain Python script, instead of hand-written IP-XACT XML.
Running the script produces an `ipxact.Design` object, which the rest of `XactFlow`
already knows what to do with: `elaborate()` resolves and checks it against a `Library`,
and `XactFlow-design` can serialize it back out to a real IP-XACT `design` XML document.

This builds on the design description concept from *IP-XACT-Based SoC Generation: Tools
and SoCMake Integration* (M. Travaillard, B. Denkinger, August 2026), specifically its
Section 4.3 and the `design2ipxact` transformation of Section 5.2. That transformation
scoped a full SoCMake-integrated pipeline (`sv2ipxact`, `design2ipxact`, `ipxact2sv`, a
shared bus library, radiation-hardening hooks, CMake integration); this document only
covers the piece of it that becomes a standalone `XactFlow` `Importer`, with no SoCMake
or CMake dependency, consistent with every other `XactFlow` plugin.

## Scope

What this importer does:

1. Execute a Python script that declares IP instances, bus connections, and ad hoc
   connections using the API below.
2. Resolve each IP reference to a real `ipxact.Component` via a `Library` the script
   itself provides.
3. Build the corresponding `ipxact.Design` object: one `ComponentInstance` per IP
   instance, one `Interconnection` per bus connection, one `AdHocConnection` per ad hoc
   connection.

What it deliberately does not do, because `XactFlow` already does it elsewhere:

- **Bus protocol compatibility checking.** `connect_bus()` only matches interfaces by bus
  definition VLNV and interface mode (see below); it does not check signal-level
  compatibility, that's `xactflow.elaborate()`'s job, via its interconnection-compatibility
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
    def __init__(self, vlnv: str, library: xactflow.Library) -> None: ...
    def add_ip(self, instance_name: str, vlnv: str, parameters: dict[str, object] | None = None, transforms: list[str] | None = None) -> IPInstance: ...
    def connect_bus(self, a: IPInstance | BusInterfaceRef, b: IPInstance | BusInterfaceRef, *, base_addr: int | None = None, size: int | None = None) -> None: ...
    def connect(self, source: PortRef, target: PortRef) -> None: ...
    def build(self) -> ipxact.Design: ...
```

### `Design(vlnv, library)`

- `vlnv`: a colon-separated `"vendor:library:name:version"` string identifying the design
  itself. Parsed eagerly; a malformed string (not exactly 4 fields) raises `ValueError` at
  construction.
- `library`: an already-scanned `xactflow.Library`. The script builds this itself
  (`Library.scan(...)`) and passes it in, `Design` never constructs or owns a `Library`
  implicitly. This keeps the script fully self-contained.

### `add_ip(instance_name, vlnv, parameters=None, transforms=None) -> IPInstance`

Declares one component instance and resolves it immediately, not deferred to `build()`:

- `instance_name` must be unique within the design. A duplicate raises `ValueError`
  immediately, naming both the new call and the earlier one that already used it.
- `vlnv` is resolved against `library` right away. An unresolvable VLNV raises
  `ValueError` at the `add_ip()` call site, not at the end of the script, so a mistake is
  reported on the line that made it.
- `parameters` maps a parameter's **name** (as declared on the resolved component) to an
  override value. This is IP-XACT's own `configurableElementValues` feature. For each
  entry, `add_ip` looks up the matching `Parameter` by name on the resolved component:
  - Unknown name: `ValueError`, naming the instance, the bad key, and the parameter names
    that *are* available on that component.
  - Name matches a parameter with no `parameterId`: `ValueError`. Such a parameter cannot
    be referenced for override at all, per the standard; this is a defect in the
    referenced *component* file, not something this importer can paper over (see
    [Requirement on imported components](#requirement-on-imported-components)).
  - Otherwise, the override is recorded against that parameter's `parameterId`.
- `transforms` names zero or more downstream transformations to apply to this instance
  before elaboration, radiation hardening being one example. This importer does nothing
  with the names itself, it only records them as a vendor extension on the resulting
  component instance, for some future tool to act on.

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
- Attribute access (`instance.some_port`) is shorthand for `instance.port("some_port")`,
  for the common case of wiring up ad hoc connections without the extra call. It raises
  the same `ValueError` as `.port()` for an unknown name. Note that `name`, `component`,
  `port`, and `bus` are real attributes/methods of `IPInstance` itself, so a port that
  happens to share one of those names cannot be reached this way; the explicit
  `.port(...)` form always works instead.

### `connect_bus(a, b, *, base_addr=None, size=None)`

Connects one bus interface on `a` to one bus interface on `b`. Each argument is either a
plain `IPInstance` (pick one of its bus interfaces automatically) or an
`IPInstance.bus(name)` (use that exact one). Here is how it picks:

1. For each side, the candidates are: the exact interface given, if a `bus(name)` was
   passed; otherwise every bus interface on that instance's component that an earlier
   `connect_bus()` call has not already used.
2. A pair `(interface_a, interface_b)` matches if they share the same bus type, and
   their `mode`s are opposite. The same bus type means the same bus VLNV
   (`interface_a.bus_type.vlnv == interface_b.bus_type.vlnv`) and the same parameter
   values set on it (`bus_type.config_element_values`), compared after evaluation. This
   first version only matches `INITIATOR` with `TARGET`; `SYSTEM`, `MIRRORED_*`, and
   `MONITOR` modes are real IP-XACT concepts but are not matched automatically here,
   connecting through one needs an exact, named call instead. There is no need to say
   which side is the initiator and which is the target: each side's mode is already set
   on its own component, so this importer reads it instead of asking the script to
   repeat it.
3. If exactly one matching pair exists, it is used.
4. If more than one exists, `connect_bus` raises `ValueError` listing every candidate
   interface name on both sides. The caller must then say exactly which one to use, by
   passing `a.bus("...")`/`b.bus("...")` for at least one side. A multi-port interconnect
   (several identical target interfaces, one per attached peripheral) will usually hit
   this case; it is expected, not an edge case.
5. If none matches, `ValueError` explains why (no shared bus definition, no opposite mode
   available, or every candidate already used) and lists what bus interfaces are still
   free on each side.

On success, one `ipxact.Interconnection` is added to the design, with `active_interface`
set from `a`'s side and `other_active_interfaces=[<b's side>]`. Its name always follows
the same pattern, `f"{a.name}_to_{b.name}"` (a number is added at the end if that name is
already taken, e.g. a second link between the same two instances). Both interfaces are
now marked used, so a later `connect_bus()` call will not reuse them.

#### Address mapping

`base_addr`/`size` are a convenience for the common case: a target sitting at some offset
on an addressable bus. IP-XACT actually handles this through `addressSpace`/`segment` on
the initiator side, resolved through a `designConfiguration`, but `XactFlow` doesn't
elaborate that yet (see the main `XactFlow`
[README](https://github.com/IP-XACT/XactFlow#known-limitations)). Doing this the real way
is future work, once that support exists. Until then,
when either is given, they're recorded as a vendor extension on the *target* side of the
connection, the same way IP-XACT represents anything the standard doesn't cover and
isn't modeled here yet. Concretely, something to the effect of:

```xml
<xactflow:addressMapping baseAddr="0x40010000" size="0x1000"/>
```

is appended to that interface's `vendor_extensions`. This is a placeholder, not a
standards-compliant addressing mechanism; anything reading it back needs to know to look
for it specifically. It should be revisited (and likely replaced) once `XactFlow`
elaborates `DesignConfiguration`/`addressSpace` for real.

### `connect(source, target)`

An ad hoc, signal-level connection between exactly two ports, each a `PortRef` (from
`.port(name)` or the attribute shorthand). Unlike `connect_bus`, this validates immediately
rather than deferring to `elaborate()`, since the check is cheap and both ports are
already in hand:

- Both ports must exist (already guaranteed, `PortRef` can only be constructed from a
  real port lookup).
- Directions must be compatible: one `out` and one `in`, or either side `inout`. Two
  `out`s or two `in`s raise `ValueError` naming both ports and their directions.

On success, appends one `ipxact.AdHocConnection` with a single `InternalPortReference`
pair (`component_instance_ref`/`port_ref` for each side), named
`f"{source.instance.name}_{source.name}_to_{target.instance.name}_{target.name}"`.
Bit-slice or struct-sub-port ad hoc connections (`SubPortReference`/`PartSelect` in
`ipxact-compiler`'s model) are out of scope for this first version; `connect()` only
wires whole ports to whole ports.

### `build() -> ipxact.Design`

Returns the finished `ipxact.Design`: every `add_ip` as a `ComponentInstance` (in call
order), every `connect_bus` as an `Interconnection`, every `connect` as an
`AdHocConnection`. It does not check that every bus interface got connected: an
unconnected optional interface (e.g. an unused debug port) is a legitimate, common
design, not an error. Completeness is `SCR`'s concern, if and when a rule for it exists,
not this importer's.

Scripts don't call `build()` themselves. The importer does, after running the script, see
below.

## How the importer runs a script

```python
class ArchiDescImporter(xactflow.Importer):
    def import_(self, source: Path, **options) -> ipxact.Design: ...
```

It executes `source` as a plain Python file (`runpy.run_path`), then looks for a
module-level variable named `design`, of type `Design`. Missing or wrong-typed `design`
raises `ValueError` naming what was expected. It then calls `.build()` on it and returns
the result. Nothing about `Library` setup is the importer's business, that already
happened inside the script when it constructed its own `Design`.

## Worked example

```python
from xactflow import Library, elaborate
from xactflow_archidesc import Design

library = Library.scan("path/to/ip/library")
design = Design("cern.ch:soc:example:0.1", library)

cpu   = design.add_ip("cpu",   "openhwgroup:core:ibex:1.0", transforms=["tmrg"])
uart  = design.add_ip("uart",  "cern.ch:ip:uart:1.0", parameters={"BAUDRATE": 115200})
timer = design.add_ip("timer", "pulp:ip:timer:1.0")
debug = design.add_ip("debug", "cern.ch:ip:debug_module:1.0")
bus   = design.add_ip("apb_bus", "cern.socgen:apb_rt:0.1.0")

design.connect_bus(cpu, bus)
design.connect_bus(bus, uart,  base_addr=0x4001_0000, size=0x1000)
design.connect_bus(bus, timer, base_addr=0x4002_0000, size=0x1000)

design.connect(debug.debug_req_o, cpu.debug_req_i)
design.connect(cpu.debug_resp_o, debug.debug_resp_i)

elaborated = elaborate(design.build(), library)
```

`design.build()` produces the `ipxact.Design`: 5 `ComponentInstance`s, 3
`Interconnection`s (`cpu_to_apb_bus`, `apb_bus_to_uart`, `apb_bus_to_timer`, one per
`connect_bus` call), and 2 `AdHocConnection`s. Passing it straight to `elaborate()` with
the same `library` resolves and checks it exactly as it would a hand-written or
hand-exported design, all in this one script, with no separate importer step needed.
Running the same script through `ArchiDescImporter` instead (letting the CLI or another
tool drive it) does that same `build()` call automatically; elaboration is then up to
whatever calls the importer, not shown here.

## Requirement on imported components

`add_ip`'s `parameters` argument only works for a component whose parameters carry a
`parameterId`; that's what `config_element_values` keys by. Any component supplied to
this format, whether produced by `XactFlow-sv`, another importer, or hand-written
IP-XACT, needs `parameterId` set on each parameter meant to be overridable this way. One
with no `parameterId` cannot be referenced for override at all, per the standard. This is
a general requirement on the components this format consumes, not something specific to
one importer.

## Open questions, deferred rather than silently resolved

Things this first version leaves open, on purpose:

- **Hierarchical design description.** A sub-design instantiated as a component inside a
  bigger one is not addressed here; this format currently describes exactly one flat
  design. `XactFlow`'s own elaborator doesn't recurse into nested designs yet either
  (see its README's "No multi-level design hierarchy" limitation), so there is nothing
  downstream to hand a hierarchical result to yet regardless.
- **Address mapping**, as above: a real system, not a vendor-extension placeholder, once
  `XactFlow` elaborates `DesignConfiguration`/`addressSpace`.
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
