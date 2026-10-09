"""Library lookup through nested tables and kicad_common.json path variables.

The layout mirrors a company setup on KiCad 10: the global sym-lib-table and
fp-lib-table each hold a single (type "Table") row pointing into a shared
library checkout, the URIs there use a path variable defined only in
kicad_common.json, and one row is a database library.

Regression: DynamicSymbolLoader read neither the nested table nor the path
variable, so add/replace_schematic_component could not place any symbol from
such a library; and the library managers read the tables once at startup, so a
library registered later stayed "not found" until the server restarted.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.dynamic_symbol_loader import DynamicSymbolLoader  # noqa: E402
from commands.library import LibraryCommands, LibraryManager  # noqa: E402
from commands.library_resolution import find_library, iter_libraries  # noqa: E402
from commands.library_symbol import SymbolLibraryManager  # noqa: E402

_SYMBOL_LIB = """\
(kicad_symbol_lib (version 20241209) (generator "test")
  (symbol "{name}" (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "{name}" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "{name}_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    )
  )
)
"""

_FOOTPRINT = """\
(footprint "{name}" (version 20241229) (generator "test") (layer "F.Cu")
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
)
"""


def _table(root: str, rows: list) -> str:
    body = "\n".join(
        f'  (lib (name "{n}")(type "{t}")(uri "{u}")(options "")(descr ""))' for n, t, u in rows
    )
    return f"({root}\n  (version 7)\n{body}\n)\n"


def _touch_later(path: Path) -> None:
    """Move *path*'s mtime forward so a same-tick rewrite still reads as an edit."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


@pytest.fixture
def kicad(tmp_path: Path, monkeypatch: Any) -> Dict[str, Path]:
    """A KiCad 10 user config with one nested company table behind ${VM_LIBS}."""
    for var in ("APPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("VM_LIBS", raising=False)
    config_root = tmp_path / "config"
    config = config_root / "10.0"
    config.mkdir(parents=True)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(config_root))

    libs = tmp_path / "company-libs"
    (libs / "symbols").mkdir(parents=True)
    (libs / "footprints" / "Company-ICs.pretty").mkdir(parents=True)
    (libs / "symbols" / "Resistors.kicad_sym").write_text(
        _SYMBOL_LIB.format(name="R"), encoding="utf-8"
    )
    (libs / "footprints" / "Company-ICs.pretty" / "TQFP64.kicad_mod").write_text(
        _FOOTPRINT.format(name="TQFP64"), encoding="utf-8"
    )
    (libs / "Parts.kicad_dbl").write_text("{}", encoding="utf-8")

    (libs / "sym-lib-table").write_text(
        _table(
            "sym_lib_table",
            [
                ("Co-Resistors", "KiCad", "${VM_LIBS}/symbols/Resistors.kicad_sym"),
                ("Co-Parts", "Database", "${VM_LIBS}/Parts.kicad_dbl"),
            ],
        ),
        encoding="utf-8",
    )
    (libs / "fp-lib-table").write_text(
        _table("fp_lib_table", [("Co-ICs", "KiCad", "${VM_LIBS}/footprints/Company-ICs.pretty")]),
        encoding="utf-8",
    )
    (config / "sym-lib-table").write_text(
        _table("sym_lib_table", [("Company", "Table", "${VM_LIBS}/sym-lib-table")]),
        encoding="utf-8",
    )
    (config / "fp-lib-table").write_text(
        _table("fp_lib_table", [("Company", "Table", "${VM_LIBS}/fp-lib-table")]),
        encoding="utf-8",
    )
    common = config / "kicad_common.json"
    common.write_text(json.dumps({"environment": {"vars": {"VM_LIBS": str(libs)}}}))

    DynamicSymbolLoader.clear_library_caches()
    yield {"config": config, "libs": libs, "common": common}
    DynamicSymbolLoader.clear_library_caches()


@pytest.mark.unit
class TestResolver:
    def test_follows_table_row_and_expands_kicad_common_variable(self, kicad: Dict) -> None:
        row = find_library("Co-Resistors", "symbol")
        assert row is not None
        assert row.exists
        assert row.path == (kicad["libs"] / "symbols" / "Resistors.kicad_sym").resolve()
        assert row.table.resolve() == (kicad["libs"] / "sym-lib-table").resolve()

    def test_database_row_is_reported_as_database(self, kicad: Dict) -> None:
        row = find_library("Co-Parts", "symbol")
        assert row is not None and row.is_database

    def test_project_table_wins_over_global(self, kicad: Dict, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        other = tmp_path / "other.kicad_sym"
        other.write_text(_SYMBOL_LIB.format(name="R"), encoding="utf-8")
        (project / "sym-lib-table").write_text(
            _table("sym_lib_table", [("Co-Resistors", "KiCad", str(other))]), encoding="utf-8"
        )
        row = find_library("Co-Resistors", "symbol", project)
        assert row is not None and row.path == other.resolve()

    def test_first_row_with_a_name_wins(self, kicad: Dict, tmp_path: Path) -> None:
        second = tmp_path / "second.kicad_sym"
        second.write_text(_SYMBOL_LIB.format(name="R"), encoding="utf-8")
        table = kicad["libs"] / "sym-lib-table"
        table.write_text(
            _table(
                "sym_lib_table",
                [
                    ("Co-Resistors", "KiCad", "${VM_LIBS}/symbols/Resistors.kicad_sym"),
                    ("Co-Resistors", "KiCad", str(second)),
                ],
            ),
            encoding="utf-8",
        )
        row = find_library("Co-Resistors", "symbol")
        assert row is not None and row.path.name == "Resistors.kicad_sym"

    def test_table_that_includes_itself_terminates(self, kicad: Dict) -> None:
        table = kicad["libs"] / "sym-lib-table"
        table.write_text(
            _table(
                "sym_lib_table",
                [
                    ("Loop", "Table", "${VM_LIBS}/sym-lib-table"),
                    ("Co-Resistors", "KiCad", "${VM_LIBS}/symbols/Resistors.kicad_sym"),
                ],
            ),
            encoding="utf-8",
        )
        names = [lib.name for lib in iter_libraries("symbol")]
        assert names == ["Co-Resistors"]

    def test_undefined_variable_is_named(self, kicad: Dict) -> None:
        kicad["common"].write_text(json.dumps({"environment": {"vars": None}}))
        # The global table's own Table row needs ${VM_LIBS}; without it nothing
        # behind it is reachable, and the loader says why it found nothing.
        loader = DynamicSymbolLoader()
        assert loader.find_library_file("Co-Resistors") is None
        assert "Co-Resistors" in loader.describe_symbol_miss("Co-Resistors", "R")


@pytest.mark.unit
class TestLoader:
    def test_loader_resolves_nested_library(self, kicad: Dict) -> None:
        loader = DynamicSymbolLoader()
        assert loader.find_library_file("Co-Resistors") is not None
        block = loader.extract_symbol_from_library("Co-Resistors", "R")
        assert block is not None and '(symbol "Co-Resistors:R"' in block

    def test_missing_variable_in_nested_row_is_explained(self, kicad: Dict) -> None:
        table = kicad["libs"] / "sym-lib-table"
        table.write_text(
            _table("sym_lib_table", [("Co-Caps", "KiCad", "${NOT_DEFINED}/Caps.kicad_sym")]),
            encoding="utf-8",
        )
        message = DynamicSymbolLoader().describe_symbol_miss("Co-Caps", "C")
        assert "${NOT_DEFINED}" in message
        assert "not defined" in message


@pytest.mark.unit
class TestManagers:
    def test_symbol_manager_finds_nested_symbol(self, kicad: Dict) -> None:
        manager = SymbolLibraryManager()
        assert manager.find_symbol("Co-Resistors:R") is not None

    def test_footprint_info_uses_library_name(self, kicad: Dict) -> None:
        result = LibraryCommands(LibraryManager()).get_footprint_info(
            {"library_name": "Co-ICs", "footprint_name": "TQFP64"}
        )
        assert result["success"] is True, result
        assert result["info"]["library"] == "Co-ICs"

    def test_footprint_info_not_found_is_reported_not_raised(self, kicad: Dict) -> None:
        result = LibraryCommands(LibraryManager()).get_footprint_info(
            {"library_name": "Co-ICs", "footprint_name": "NoSuchFootprint"}
        )
        assert result == {
            "success": False,
            "message": "Footprint not found: Co-ICs:NoSuchFootprint",
        }

    def test_footprint_info_unknown_library(self, kicad: Dict) -> None:
        result = LibraryCommands(LibraryManager()).get_footprint_info(
            {"library_name": "NoSuchLib", "footprint_name": "TQFP64"}
        )
        assert result["success"] is False
        assert "NoSuchLib" in result["message"] and "fp-lib-table" in result["message"]

    def test_library_added_to_nested_table_after_startup(self, kicad: Dict) -> None:
        manager = SymbolLibraryManager()
        assert manager.find_symbol("Co-Caps:C") is None

        caps = kicad["libs"] / "symbols" / "Caps.kicad_sym"
        caps.write_text(_SYMBOL_LIB.format(name="C"), encoding="utf-8")
        table = kicad["libs"] / "sym-lib-table"
        table.write_text(
            table.read_text(encoding="utf-8").replace(
                "\n)\n",
                '\n  (lib (name "Co-Caps")(type "KiCad")'
                '(uri "${VM_LIBS}/symbols/Caps.kicad_sym")(options "")(descr ""))\n)\n',
            ),
            encoding="utf-8",
        )
        _touch_later(table)
        assert manager.find_symbol("Co-Caps:C") is not None

    def test_path_variable_defined_after_startup(self, kicad: Dict) -> None:
        defined = kicad["common"].read_text()
        kicad["common"].write_text(json.dumps({"environment": {"vars": None}}))
        manager = LibraryManager()
        assert manager.find_footprint("Co-ICs:TQFP64") is None

        kicad["common"].write_text(defined)
        _touch_later(kicad["common"])
        assert manager.find_footprint("Co-ICs:TQFP64") is not None
