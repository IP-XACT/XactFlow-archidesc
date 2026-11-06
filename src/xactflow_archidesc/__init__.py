__version__ = "0.2.0"

import runpy
import sys
from pathlib import Path
from typing import Set

import ipxact
from xactflow import Importer

from .design import BusInterfaceRef, Design, IPInstance, PortRef


class ArchiDescImporter(Importer):
    """Runs a Python design description script and returns the ipxact.Design it describes.

    The script must define a module-level variable named `design`, holding a Design. It runs
    like `python script.py` would: its own folder is importable while it runs.
    """

    name = "archidesc"

    def import_(self, source_path: Path, **options: object) -> ipxact.Design:
        if options:
            raise ValueError(f"the archidesc importer takes no options, got: {', '.join(options)}")
        source_path = Path(source_path)
        if not source_path.is_file():
            raise ValueError(f"{source_path} is not a file")
        modules_before = set(sys.modules)
        sys.path.insert(0, str(source_path.parent))
        try:
            namespace = runpy.run_path(str(source_path))
        finally:
            sys.path.remove(str(source_path.parent))
            _forget_modules_from(source_path.parent, modules_before)
        if "design" not in namespace:
            raise ValueError(f"{source_path} must define a module-level variable 'design'")
        design = namespace["design"]
        if not isinstance(design, Design):
            kind = type(design)
            raise ValueError(
                f"{source_path}: 'design' must be a xactflow_archidesc.Design, got {kind.__module__}.{kind.__qualname__}"
            )
        return design.build()


def _forget_modules_from(folder: Path, modules_before: Set[str]) -> None:
    """Remove from sys.modules the modules the script imported from its own folder.

    Otherwise a later script in another folder importing a module of the same name would get
    this one.
    """
    folder = folder.resolve()
    for name in set(sys.modules) - modules_before:
        file = getattr(sys.modules[name], "__file__", None)
        if file is not None and Path(file).resolve().is_relative_to(folder):
            del sys.modules[name]


__all__ = ["__version__", "ArchiDescImporter", "BusInterfaceRef", "Design", "IPInstance", "PortRef"]
