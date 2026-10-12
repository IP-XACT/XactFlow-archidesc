__version__ = "0.1.0"

from pathlib import Path

from xactflow import Importer

from .design import BusInterfaceRef, Design, IPInstance, PortRef


class ArchiDescImporter(Importer):
    name = "archidesc"

    def import_(self, source_path: Path, **options: object) -> object:
        raise NotImplementedError


__all__ = ["__version__", "ArchiDescImporter", "BusInterfaceRef", "Design", "IPInstance", "PortRef"]
