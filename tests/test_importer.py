import sys
from pathlib import Path

import ipxact
import pytest
from xactflow import discover_importers

from xactflow_archidesc import ArchiDescImporter

LIBRARY = Path(__file__).parent / "fixtures" / "library"

SCRIPT = f"""
from xactflow import Library
from xactflow_archidesc import Design

library = Library.scan({str(LIBRARY)!r})
design = Design("example.org:soc:top:1.0", library)
cpu = design.add_ip("cpu", "example.org:ip:cpu:1.0")
ic = design.add_ip("ic", "example.org:ip:interconnect:1.0")
debug = design.add_ip("debug", "example.org:ip:debug_module:1.0")
design.connect_bus(cpu, ic)
design.connect(debug.debug_req_o, cpu.debug_req_i)
"""


def test_importer_is_registered():
    assert discover_importers()["archidesc"] is ArchiDescImporter


def test_import_runs_the_script_and_returns_the_built_design(tmp_path):
    script = tmp_path / "top.py"
    script.write_text(SCRIPT)

    design = ArchiDescImporter().import_(script)

    assert isinstance(design, ipxact.Design)
    assert design.vlnv == ipxact.VLNV("example.org", "soc", "top", "1.0")
    assert [i.instance_name for i in design.component_instances] == ["cpu", "ic", "debug"]
    assert [i.name for i in design.interconnections] == ["cpu_to_ic"]
    assert [c.name for c in design.ad_hoc_connections] == ["debug_debug_req_o_to_cpu_debug_req_i"]


@pytest.mark.parametrize(
    "ending, message",
    [
        ("del design", "must define a module-level variable 'design'"),
        ("design = design.build()", "'design' must be a xactflow_archidesc.Design, got ipxact.schema.design.Design"),
    ],
)
def test_import_rejects_a_script_without_a_design(tmp_path, ending, message):
    script = tmp_path / "top.py"
    script.write_text(SCRIPT + ending + "\n")

    with pytest.raises(ValueError, match=message):
        ArchiDescImporter().import_(script)


def test_script_can_import_a_module_next_to_it(tmp_path):
    (tmp_path / "helpers.py").write_text("TOP = 'example.org:soc:top:1.0'\n")
    script = tmp_path / "top.py"
    script.write_text("from helpers import TOP\n" + SCRIPT.replace('"example.org:soc:top:1.0"', "TOP"))

    assert ArchiDescImporter().import_(script).vlnv.name == "top"


def test_scripts_in_two_folders_each_get_their_own_helper_module(tmp_path):
    scripts = []
    for name in ("soc_a", "soc_b"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "helpers.py").write_text(f"TOP = 'example.org:soc:{name}:1.0'\n")
        scripts.append(folder / "top.py")
        scripts[-1].write_text("from helpers import TOP\n" + SCRIPT.replace('"example.org:soc:top:1.0"', "TOP"))

    assert [ArchiDescImporter().import_(script).vlnv.name for script in scripts] == ["soc_a", "soc_b"]


def test_a_package_installed_below_the_script_folder_stays_imported(tmp_path, monkeypatch):
    # A .venv next to the script: its packages are found through sys.path, not the script folder.
    site = tmp_path / ".venv" / "site"
    (site / "archidesc_test_pkg" / "sub").mkdir(parents=True)
    (site / "archidesc_test_pkg" / "__init__.py").write_text("")
    (site / "archidesc_test_pkg" / "sub" / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(site))
    for name in ("archidesc_test_pkg", "archidesc_test_pkg.sub"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    script = tmp_path / "top.py"
    script.write_text("import archidesc_test_pkg.sub\n" + SCRIPT)

    ArchiDescImporter().import_(script)

    assert "archidesc_test_pkg.sub" in sys.modules


def test_the_script_error_is_raised_even_if_the_script_left_sys_path(tmp_path):
    script = tmp_path / "top.py"
    script.write_text("import os, sys\nsys.path.remove(os.path.dirname(__file__))\nraise RuntimeError('boom')\n")

    with pytest.raises(RuntimeError, match="boom"):
        ArchiDescImporter().import_(script)


def test_import_rejects_a_path_that_is_not_a_file(tmp_path):
    with pytest.raises(ValueError, match="is not a file"):
        ArchiDescImporter().import_(tmp_path)


def test_import_rejects_options(tmp_path):
    script = tmp_path / "top.py"
    script.write_text(SCRIPT)

    with pytest.raises(ValueError, match="takes no options, got: verbose"):
        ArchiDescImporter().import_(script, verbose="1")
