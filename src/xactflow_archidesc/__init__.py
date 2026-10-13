__version__ = "0.1.0"

import runpy
import sys
from pathlib import Path

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
        sys.path.insert(0, str(source_path.parent))
        try:
            namespace = runpy.run_path(str(source_path))
        finally:
            sys.path.remove(str(source_path.parent))
        if "design" not in namespace:
            raise ValueError(f"{source_path} must define a module-level variable 'design'")
        design = namespace["design"]
        if not isinstance(design, Design):
            kind = type(design)
            raise ValueError(
                f"{source_path}: 'design' must be a xactflow_archidesc.Design, got {kind.__module__}.{kind.__qualname__}"
            )
        return design.build()


__all__ = ["__version__", "ArchiDescImporter", "BusInterfaceRef", "Design", "IPInstance", "PortRef"]
