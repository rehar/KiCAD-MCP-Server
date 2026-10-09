"""replace_schematic_component must change the file completely or not at all.

Regression: the handler removed the old symbol block and wrote the file before
it loaded the new symbol, so a library or symbol that could not be loaded left
the part deleted and its wires dangling. These tests run the real loader against
a project-local symbol library; only the file contents are inspected.
"""

import hashlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.dynamic_symbol_loader import DynamicSymbolLoader  # noqa: E402
from commands.schematic_batch import SchematicBatchCommands  # noqa: E402

_LIB = """\
(kicad_symbol_lib (version 20241209) (generator "test")
  (symbol "C" (pin_numbers (hide yes)) (pin_names (offset 0.254)) (in_bom yes) (on_board yes)
    (property "Reference" "C" (at 0.635 2.54 0) (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "C" (at 0.635 -2.54 0) (effects (font (size 1.27 1.27)) (justify left)))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Datasheet" "~" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Description" "Unpolarized capacitor" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (symbol "C_0_1"
      (polyline (pts (xy -2.032 0.762) (xy 2.032 0.762)) (stroke (width 0.508) (type default)) (fill (type none)))
      (polyline (pts (xy -2.032 -0.762) (xy 2.032 -0.762)) (stroke (width 0.508) (type default)) (fill (type none)))
    )
    (symbol "C_1_1"
      (pin passive line (at 0 3.81 270) (length 2.794) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 2.794) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
    )
  )
)
"""

_SYM_LIB_TABLE = """\
(sym_lib_table
  (version 7)
  (lib (name "TestLib")(type "KiCad")(uri "${KIPRJMOD}/TestLib.kicad_sym")(options "")(descr ""))
)
"""

_SCH = """\
(kicad_sch (version 20250114) (generator "eeschema")
  (uuid "aaaaaaaa-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Old:C"
      (property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Description" "LIBRARY TEXT" (at 0 0 0) (effects (font (size 1.27 1.27))))
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
    (property "Footprint" "Capacitor_SMD:C_0402_1005Metric" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Description" "OLD DESC" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "MPN" "GRM155R71C104KA88D" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (pin "1" (uuid "cccccccc-0000-0000-0000-0000000000a1"))
    (pin "2" (uuid "cccccccc-0000-0000-0000-0000000000a2"))
    (instances (project "proj" (path "/aaaaaaaa-0000-0000-0000-000000000001" (reference "C22") (unit 1))))
  )
  (sheet_instances (path "/" (page "1")))
)
"""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    DynamicSymbolLoader.clear_library_caches()
    (tmp_path / "proj.kicad_pro").write_text("{}\n", encoding="utf-8")
    (tmp_path / "sym-lib-table").write_text(_SYM_LIB_TABLE, encoding="utf-8")
    (tmp_path / "TestLib.kicad_sym").write_text(_LIB, encoding="utf-8")
    sch = tmp_path / "proj.kicad_sch"
    sch.write_text(_SCH, encoding="utf-8")
    yield sch
    DynamicSymbolLoader.clear_library_caches()


def _commands() -> SchematicBatchCommands:
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    iface._update_command_handlers = MagicMock()
    return SchematicBatchCommands(iface)


def _replace(sch: Path, new_symbol: str) -> Any:
    return _commands().replace_schematic_component(
        {"schematicPath": str(sch), "reference": "C22", "newSymbol": new_symbol}
    )


@pytest.mark.unit
class TestReplaceFailureLeavesFileUnchanged:
    def test_unknown_library(self, project: Path) -> None:
        before = _digest(project)
        result = _replace(project, "NoSuchLib:C")
        assert result["success"] is False
        assert "C22 was not changed" in result["message"]
        assert _digest(project) == before

    def test_unknown_symbol_in_known_library(self, project: Path) -> None:
        before = _digest(project)
        result = _replace(project, "TestLib:NoSuchSymbol")
        assert result["success"] is False
        assert _digest(project) == before

    def test_failure_after_old_block_removed(self, project: Path, monkeypatch: Any) -> None:
        def boom(*_a: Any, **_kw: Any) -> str:
            raise RuntimeError("instance build failed")

        monkeypatch.setattr(DynamicSymbolLoader, "component_instance_text", boom)
        before = _digest(project)
        result = _replace(project, "TestLib:C")
        assert result["success"] is False
        assert _digest(project) == before

    def test_a_second_attempt_still_finds_the_part(self, project: Path) -> None:
        _replace(project, "NoSuchLib:C")
        result = _replace(project, "TestLib:C")
        assert result["success"] is True, result


@pytest.mark.unit
class TestReplaceSuccess:
    def test_relinks_and_keeps_position_and_fields(self, project: Path) -> None:
        result = _replace(project, "TestLib:C")
        assert result["success"] is True, result

        text = project.read_text(encoding="utf-8")
        assert '(lib_id "Old:C")' not in text
        assert text.count('(property "Reference" "C22"') == 1
        assert '(symbol (lib_id "TestLib:C") (at 101.6 76.2 90)' in text
        assert '(symbol "TestLib:C"' in text
        assert '(property "Value" "100nF"' in text
        assert '(property "Footprint" "Capacitor_SMD:C_0402_1005Metric"' in text
        assert '(property "MPN" "GRM155R71C104KA88D"' in text
        assert '(property "Description" "OLD DESC"' in text

    def test_restore_does_not_touch_lib_symbols(self, project: Path) -> None:
        """Field restore is scoped to the new instance block.

        A whole-file substitution hit the first (property "Description" ...),
        which lives inside lib_symbols, and corrupted that library definition.
        """
        assert _replace(project, "TestLib:C")["success"] is True
        assert '"LIBRARY TEXT"' in project.read_text(encoding="utf-8")

    def test_crlf_file_keeps_its_line_endings(self, project: Path) -> None:
        project.write_bytes(_SCH.replace("\n", "\r\n").encode("utf-8"))
        assert _replace(project, "TestLib:C")["success"] is True
        raw = project.read_bytes()
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")
