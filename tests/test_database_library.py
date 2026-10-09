"""KiCad database libraries (.kicad_dbl) in the symbol loader and tools.

A database part is named ``<library>/<key>``; its row names the library symbol
that draws it, its footprint, and the columns that become fields. The fixture
is a project-local sym-lib-table with an ordinary library and a database
library backed by a SQLite file, opened through Python's sqlite3 (pyodbc is
hidden), so no ODBC driver is needed. One test exercises pyodbc when it and
its SQLite ODBC driver are installed.
"""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.database_library import DatabaseLibrary, DatabaseLibraryError  # noqa: E402
from commands.dynamic_symbol_loader import DynamicSymbolLoader  # noqa: E402
from commands.library_symbol import SymbolLibraryManager  # noqa: E402
from commands.schematic_batch import SchematicBatchCommands  # noqa: E402

# Tab-indented like a KiCad-written library: the extracted block then starts
# with indentation, which once put added fields outside the symbol.
_LIB = """\
(kicad_symbol_lib
\t(version 20241209)
\t(generator "test")
\t(symbol "C"
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(property "Reference" "C" (at 0.635 2.54 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "C" (at 0.635 -2.54 0) (effects (font (size 1.27 1.27))))
\t\t(property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(property "Description" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
\t\t(symbol "C_1_1"
\t\t\t(pin passive line (at 0 3.81 270) (length 2.794) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
\t\t\t(pin passive line (at 0 -3.81 90) (length 2.794) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
\t\t)
\t)
)
"""

_DBL = {
    "meta": {"version": 0},
    "name": "Parts",
    "description": "test parts",
    "source": {
        "type": "odbc",
        "dsn": "",
        "username": "",
        "password": "",
        "timeout_seconds": 2,
        "connection_string": "Driver={SQLITE3};Database=${CWD}/parts.db",
    },
    "libraries": [
        {
            "name": "Passive Caps",
            "table": "Capacitors",
            "key": "Part_Number",
            "symbols": "Symbols",
            "footprints": "Footprints",
            "fields": [
                {"column": "Part_Number", "name": "Part Number", "visible_on_add": False},
                {"column": "MPN", "name": "MPN", "visible_on_add": False},
                {"column": "Value", "name": "Value", "visible_on_add": True},
            ],
            "properties": {"description": "Description"},
        }
    ],
}

_SCH = """\
(kicad_sch (version 20250114) (generator "eeschema")
  (uuid "aaaaaaaa-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Old:C"
      (property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "C_1_1"
        (pin passive line (at 0 3.81 270) (length 2.794) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 2.794) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "Old:C") (at 101.6 76.2 90) (unit 1)
    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
    (uuid "cccccccc-0000-0000-0000-000000000022")
    (property "Reference" "C22" (at 101.6 72.39 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 101.6 80.01 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Old:C0402" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "MPN" "OLD-MPN" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Assembly Note" "hand solder" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "cccccccc-0000-0000-0000-0000000000a1"))
    (pin "2" (uuid "cccccccc-0000-0000-0000-0000000000a2"))
    (instances (project "proj" (path "/aaaaaaaa-0000-0000-0000-000000000001" (reference "C22") (unit 1))))
  )
  (sheet_instances (path "/" (page "1")))
)
"""

PART = "Parts:Passive Caps/120-00449"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def no_pyodbc(monkeypatch: Any) -> None:
    """Make ``import pyodbc`` fail, so the sqlite3 path is what runs."""
    monkeypatch.setitem(sys.modules, "pyodbc", None)


@pytest.fixture
def project(tmp_path: Path, no_pyodbc: None) -> Path:
    DynamicSymbolLoader.clear_library_caches()
    (tmp_path / "proj.kicad_pro").write_text("{}\n", encoding="utf-8")
    (tmp_path / "TestLib.kicad_sym").write_text(_LIB, encoding="utf-8")
    (tmp_path / "parts.kicad_dbl").write_text(json.dumps(_DBL), encoding="utf-8")
    db = sqlite3.connect(tmp_path / "parts.db")
    db.execute(
        "CREATE TABLE Capacitors (Part_Number TEXT, Symbols TEXT, Footprints TEXT,"
        " Description TEXT, MPN TEXT, Value TEXT)"
    )
    db.executemany(
        "INSERT INTO Capacitors VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("120-00449", "TestLib:C", "Cap:C0603;Cap:C0805", "100 nF 25 V X7R", "GRM-104", "100n"),
            ("120-00558", "TestLib:C", "Cap:C1206", "2.2 uF 25 V X7R", None, "2u2"),
            ("A/B 1", "TestLib:C", "Cap:C0402", "slash key", "SL-1", "1n"),
            ("120-99999", "NoSuchLib:C", "Cap:C0402", "", "", "1p"),
        ],
    )
    db.commit()
    db.close()
    (tmp_path / "sym-lib-table").write_text(
        "(sym_lib_table\n  (version 7)\n"
        '  (lib (name "TestLib")(type "KiCad")(uri "${KIPRJMOD}/TestLib.kicad_sym")(options "")(descr ""))\n'
        '  (lib (name "Parts")(type "Database")(uri "${KIPRJMOD}/parts.kicad_dbl")(options "")(descr ""))\n'
        ")\n",
        encoding="utf-8",
    )
    sch = tmp_path / "proj.kicad_sch"
    sch.write_text(_SCH, encoding="utf-8")
    yield sch
    DynamicSymbolLoader.clear_library_caches()


def _replace(sch: Path, new_symbol: str, **extra: Any) -> Any:
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface._update_command_handlers = MagicMock()
    return SchematicBatchCommands(iface).replace_schematic_component(
        {"schematicPath": str(sch), "reference": "C22", "newSymbol": new_symbol, **extra}
    )


def _instance(text: str) -> str:
    start = text.index("(symbol (lib_id")
    return text[start : text.index("(sheet_instances", start)]


@pytest.mark.unit
class TestDatabaseLibrary:
    def test_part_from_sqlite(self, project: Path) -> None:
        part = DatabaseLibrary(project.parent / "parts.kicad_dbl").part("Passive Caps/120-00449")
        assert part.symbol == "TestLib:C"
        assert part.footprints == ["Cap:C0603", "Cap:C0805"]
        assert part.description == "100 nF 25 V X7R"
        assert ("Value", "100n", True, False) in part.fields

    def test_key_containing_a_slash(self, project: Path) -> None:
        part = DatabaseLibrary(project.parent / "parts.kicad_dbl").part("Passive Caps/A/B 1")
        assert part.name == "Passive Caps/A/B 1"

    def test_null_column_gives_no_field(self, project: Path) -> None:
        part = DatabaseLibrary(project.parent / "parts.kicad_dbl").part("Passive Caps/120-00558")
        assert "MPN" not in [name for name, *_ in part.fields]

    def test_missing_row(self, project: Path) -> None:
        with pytest.raises(DatabaseLibraryError, match="No row with Part_Number = 'nope'"):
            DatabaseLibrary(project.parent / "parts.kicad_dbl").part("Passive Caps/nope")

    def test_unknown_library_name(self, project: Path) -> None:
        with pytest.raises(DatabaseLibraryError, match="'Passive Caps'"):
            DatabaseLibrary(project.parent / "parts.kicad_dbl").part("Resistors/1")

    def test_non_sqlite_source_needs_pyodbc(self, project: Path) -> None:
        dbl = project.parent / "parts.kicad_dbl"
        config = dict(_DBL, source=dict(_DBL["source"], connection_string="Driver={PSQL};Server=x"))
        dbl.write_text(json.dumps(config), encoding="utf-8")
        with pytest.raises(DatabaseLibraryError, match="pyodbc"):
            DatabaseLibrary(dbl).part("Passive Caps/120-00449")

    def test_unsupported_source_type(self, project: Path) -> None:
        dbl = project.parent / "parts.kicad_dbl"
        dbl.write_text(json.dumps(dict(_DBL, source={"type": "rest"})), encoding="utf-8")
        with pytest.raises(DatabaseLibraryError, match="not supported"):
            DatabaseLibrary(dbl).part("Passive Caps/120-00449")


@pytest.mark.unit
def test_pyodbc_sqlite_driver(tmp_path: Path) -> None:
    pyodbc = pytest.importorskip("pyodbc")
    if not any("sqlite" in d.lower() for d in pyodbc.drivers()):
        pytest.skip("no SQLite ODBC driver installed")
    driver = next(d for d in pyodbc.drivers() if "sqlite" in d.lower())
    dbl = tmp_path / "parts.kicad_dbl"
    config = dict(
        _DBL,
        source=dict(
            _DBL["source"], connection_string=f"Driver={{{driver}}};Database=${{CWD}}/parts.db"
        ),
    )
    dbl.write_text(json.dumps(config), encoding="utf-8")
    db = sqlite3.connect(tmp_path / "parts.db")
    db.execute("CREATE TABLE Capacitors (Part_Number TEXT, Symbols TEXT, Footprints TEXT)")
    db.execute("INSERT INTO Capacitors VALUES ('1', 'TestLib:C', 'Cap:C0603')")
    db.commit()
    db.close()
    library = DatabaseLibrary(dbl)
    conn, _quote = library._connect()
    assert type(conn).__module__ == "pyodbc"
    conn.close()
    assert library.part("Passive Caps/1").symbol == "TestLib:C"


@pytest.mark.unit
class TestLoader:
    def test_symbol_block_is_renamed_and_carries_the_row(self, project: Path) -> None:
        loader = DynamicSymbolLoader(project_path=project.parent)
        block = loader.extract_symbol_from_library("Parts", "Passive Caps/120-00449")
        assert block is not None
        assert block.lstrip().startswith(f'(symbol "{PART}"')
        assert '(symbol "Passive Caps/120-00449_1_1"' in block
        assert '(property "Footprint" "Cap:C0603"' in block
        assert '(property "Value" "100n"' in block
        assert '(property "Description" "100 nF 25 V X7R"' in block
        # Added fields sit inside the symbol, before its unit sub-symbol.
        mpn = block.index('(property "MPN" "GRM-104"')
        assert block.index(f'(symbol "{PART}"') < mpn < block.index('(symbol "Passive Caps/')

    def test_add_component_places_the_part_with_its_fields(self, project: Path) -> None:
        loader = DynamicSymbolLoader(project_path=project.parent)
        loader.add_component(
            project, "Parts", "Passive Caps/120-00449", reference="C1", x=50.8, y=50.8
        )
        inst = project.read_text(encoding="utf-8")
        inst = inst[inst.index(f'(symbol (lib_id "{PART}")') :]
        assert '(property "Value" "100n"' in inst
        assert '(property "Footprint" "Cap:C0603"' in inst
        assert '(property "MPN" "GRM-104"' in inst
        assert '(property "Part Number" "120-00449"' in inst

    def test_base_symbol_missing_is_explained(self, project: Path) -> None:
        loader = DynamicSymbolLoader(project_path=project.parent)
        assert loader.extract_symbol_from_library("Parts", "Passive Caps/120-99999") is None
        message = loader.describe_symbol_miss("Parts", "Passive Caps/120-99999")
        assert "NoSuchLib:C" in message

    def test_symbol_manager_lists_and_finds_parts(self, project: Path) -> None:
        manager = SymbolLibraryManager(project_path=project.parent)
        info = manager.find_symbol(PART)
        assert info is not None
        assert info.value == "100n" and info.mpn == "GRM-104"
        assert info.footprint == "Cap:C0603"
        assert len(manager.list_symbols("Parts")) == 4


@pytest.mark.unit
class TestReplaceWithDatabasePart:
    def test_row_values_win_and_unshared_fields_survive(self, project: Path) -> None:
        result = _replace(project, PART)
        assert result["success"] is True, result
        inst = _instance(project.read_text(encoding="utf-8"))
        assert f'(lib_id "{PART}") (at 101.6 76.2 90)' in inst
        assert '(property "Value" "100n"' in inst
        assert '(property "Footprint" "Cap:C0603"' in inst
        assert '(property "MPN" "GRM-104"' in inst
        assert "OLD-MPN" not in inst
        assert '(property "Assembly Note" "hand solder"' in inst

    def test_old_field_fills_a_null_column(self, project: Path) -> None:
        assert _replace(project, "Parts:Passive Caps/120-00558")["success"] is True
        assert '(property "MPN" "OLD-MPN"' in _instance(project.read_text(encoding="utf-8"))

    def test_remove_old_fields(self, project: Path) -> None:
        assert _replace(project, PART, removeOldFields=True)["success"] is True
        inst = _instance(project.read_text(encoding="utf-8"))
        assert "Assembly Note" not in inst
        assert '(property "MPN" "GRM-104"' in inst

    def test_missing_row_leaves_file_unchanged(self, project: Path) -> None:
        before = _digest(project)
        result = _replace(project, "Parts:Passive Caps/nope")
        assert result["success"] is False
        assert "No row with Part_Number = 'nope'" in result["message"]
        assert _digest(project) == before
