"""design.py: the Design object a design description script builds."""

from __future__ import annotations

import inspect
import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional
from xml.sax.saxutils import quoteattr

import ipxact
from xactflow import Library

# Namespace of the vendor extensions this importer writes.
XACTFLOW_NAMESPACE = "https://github.com/IP-XACT/XactFlow"

# Instance names end up as HDL instance names, so they must be plain identifiers.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class IPInstance:
    """A handle on one component instance"""

    def __init__(self, name: str, component: ipxact.Component) -> None:
        self.name = name
        self.component = component

    def __repr__(self) -> str:
        return f"IPInstance({self.name!r}, {self.component.vlnv})"


@dataclass
class _InstanceRecord:
    component_instance: ipxact.ComponentInstance
    call_site: str


def _caller_site() -> str:
    """Return "file:line" of the code that called the public Design method."""
    caller = inspect.currentframe().f_back.f_back  # skip _caller_site itself, then the Design method
    return f"{caller.f_code.co_filename}:{caller.f_lineno}"


def _check_type(value: object, expected: type, what: str) -> None:
    """Raise TypeError if value is not of the expected type."""
    if not isinstance(value, expected):
        raise TypeError(f"{what} must be a {expected.__name__}, got {type(value).__name__}: {value!r}")


def _parse_vlnv(value: object, what: str) -> ipxact.VLNV:
    _check_type(value, str, what)
    fields = value.split(":")
    if len(fields) != 4 or not all(fields):
        raise ValueError(f"{what} must be a 'vendor:library:name:version' string, got: {value!r}")
    return ipxact.VLNV(*fields)


def _expression(value: object, what: str) -> str:
    """Write a Python value as an IP-XACT expression string."""
    if isinstance(value, bool):  # before int, since bool is an int
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    raise TypeError(f"{what} must be an int, float, bool or str, got {type(value).__name__}: {value!r}")


def _config_element_values(
    where: str, component: ipxact.Component, parameters: Optional[Dict[str, object]]
) -> Dict[str, str]:
    """Turn add_ip()'s parameter overrides into configurableElementValues, keyed by parameterId."""
    if parameters is None:
        return {}
    _check_type(parameters, dict, f"{where}: parameters")
    by_name = {parameter.name: parameter for parameter in component.parameters}
    values = {}
    for name, value in parameters.items():
        parameter = by_name.get(name)
        if parameter is None:
            raise ValueError(
                f"{where}: {component.vlnv} has no parameter {name!r}. "
                f"Available parameters: {', '.join(by_name) or 'none'}"
            )
        if parameter.parameter_id is None:
            raise ValueError(
                f"{where}: parameter {name!r} of {component.vlnv} has no parameterId, "
                f"so it cannot be overridden. This needs fixing in the component file"
            )
        values[parameter.parameter_id] = _expression(value, f"{where}: parameter {name!r}")
    return values


def _transforms_extensions(where: str, transforms: Optional[List[str]]) -> List[str]:
    """Turn add_ip()'s transforms into vendor extensions on the component instance.

    The transformations are only recorded here, a separate transformer applies them later.
    """
    transforms = [] if transforms is None else transforms
    if not isinstance(transforms, (list, tuple)) or not all(isinstance(t, str) and t for t in transforms):
        raise TypeError(f"{where}: transforms must be a list of non-empty strings, got: {transforms!r}")
    if not transforms:
        return []
    items = "".join(f"<xactflow:transform name={quoteattr(name)}/>" for name in transforms)
    return [f'<xactflow:transforms xmlns:xactflow="{XACTFLOW_NAMESPACE}">{items}</xactflow:transforms>']


class Design:
    """Describes one flat design: its component instances and how they are connected.

    build() turns it into an ipxact.Design.
    """

    def __init__(self, vlnv: str, library: Library) -> None:
        self.vlnv = _parse_vlnv(vlnv, "Design(): vlnv")
        self.library = library
        self._instances: Dict[str, _InstanceRecord] = {}

    def add_ip(
        self,
        instance_name: str,
        vlnv: str,
        parameters: Optional[Dict[str, object]] = None,
        transforms: Optional[List[str]] = None,
    ) -> IPInstance:
        """Add one component instance, resolving its VLNV against the library right away."""
        call_site = _caller_site()
        self._check_new_instance_name(instance_name, call_site)
        where = f"add_ip('{instance_name}')"
        component_vlnv = _parse_vlnv(vlnv, f"{where}: vlnv")
        component = self._resolve_component(where, component_vlnv)

        component_instance = ipxact.ComponentInstance(
            instance_name=instance_name,
            component_ref=ipxact.VLNVRef(
                **asdict(component_vlnv),
                config_element_values=_config_element_values(where, component, parameters),
            ),
            vendor_extensions=_transforms_extensions(where, transforms),
        )
        self._instances[instance_name] = _InstanceRecord(component_instance, call_site)
        return IPInstance(instance_name, component)

    def build(self) -> ipxact.Design:
        """Return the ipxact.Design described so far."""
        return ipxact.Design(
            vlnv=self.vlnv,
            component_instances=[record.component_instance for record in self._instances.values()],
        )

    def _check_new_instance_name(self, instance_name: object, call_site: str) -> None:
        _check_type(instance_name, str, "add_ip(): instance name")
        if not _IDENTIFIER.fullmatch(instance_name):
            raise ValueError(
                f"add_ip(): instance name must start with a letter or '_' and contain only "
                f"letters, digits and '_', got: {instance_name!r}"
            )
        earlier = self._instances.get(instance_name)
        if earlier is not None:
            raise ValueError(
                f"add_ip() at {call_site}: instance name '{instance_name}' is already used "
                f"by add_ip() at {earlier.call_site}"
            )

    def _resolve_component(self, where: str, vlnv: ipxact.VLNV) -> ipxact.Component:
        document = self.library.get(vlnv)
        if document is None:
            raise ValueError(f"{where}: no document with VLNV {vlnv} in the library")
        if not isinstance(document, ipxact.Component):
            raise ValueError(f"{where}: VLNV {vlnv} is a {type(document).__name__}, not a Component")
        return document
