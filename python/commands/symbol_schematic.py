"""
Replace instance lib_ids in KiCad schematics — library migration helper.

Handles the mechanical work of swapping lib_id references in schematic
instances, including mirror-variant angle correction (__m0/__m90/__m180/__m270
suffixes produced by the Eagle importer's mirror cache). Which symbol maps to
which — by type, value, footprint — is the caller's decision; this tool only
applies a given mapping.
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils.file_io import read_text_preserve_newline, write_text_atomic
from utils.sexpr_format import QUOTED_VALUE, escape_sexpr_string, match_paren, unescape_sexpr_string

logger = logging.getLogger("kicad_interface")

MIRROR_TO_ANGLE = {"__m0": 0, "__m90": 90, "__m180": 180, "__m270": 270}

_PIN_RE = re.compile(
    r"\(pin\s+\S+\s+\S+\s*\(at\s+([-\d.]+)\s+([-\d.]+)[^)]*\).*?" r"\(number\s+" + QUOTED_VALUE,
    re.DOTALL,
)


def _lib_symbols_span(content: str) -> Tuple[int, int]:
    """(start, end) of the lib_symbols section, end inclusive; (-1, -1) if absent."""
    start = content.find("(lib_symbols")
    if start < 0:
        return -1, -1
    return start, match_paren(content, start)


def _placed_blocks(content: str, reference: str) -> List[Tuple[int, int]]:
    """(start, end) of every placed (symbol ...) block -- every unit -- of *reference*."""
    lib_start, lib_end = _lib_symbols_span(content)
    ref_re = re.compile(r'\(property\s+"Reference"\s+' + QUOTED_VALUE)
    spans = []
    for m in re.finditer(r"\(symbol\s+\(", content):
        pos = m.start()
        if lib_start <= pos <= lib_end:
            continue
        end = match_paren(content, pos)
        if end < 0:
            continue
        ref = ref_re.search(content, pos, end)
        if ref and unescape_sexpr_string(ref.group(1)) == reference:
            spans.append((pos, end))
    return spans


def _lib_symbol_block(content: str, name: str) -> Optional[str]:
    """The lib_symbols entry named *name*, or None."""
    lib_start, lib_end = _lib_symbols_span(content)
    if lib_start < 0:
        return None
    pos = content.find(f'(symbol "{escape_sexpr_string(name)}"', lib_start, lib_end)
    if pos < 0:
        return None
    return content[pos : match_paren(content, pos) + 1]


def _unit_pins(lib_block: str, unit: int) -> Dict[str, Tuple[float, float]]:
    """Pin number -> connection point of *unit* (plus the shared unit 0), body style 1."""
    pins: Dict[str, Tuple[float, float]] = {}
    for m in re.finditer(r'\(symbol\s+"((?:[^"\\]|\\.)*)"', lib_block):
        um = re.search(r"_(\d+)_(\d+)$", m.group(1))
        if not um or int(um.group(1)) not in (0, unit) or int(um.group(2)) not in (0, 1):
            continue
        sub = lib_block[m.start() : match_paren(lib_block, m.start()) + 1]
        for pm in _PIN_RE.finditer(sub):
            pins[unescape_sexpr_string(pm.group(3))] = (
                round(float(pm.group(1)), 3),
                round(float(pm.group(2)), 3),
            )
    return pins


def _set_instance_property(block: str, name: str, value: str) -> str:
    """*block* with field *name* set to *value*; a new field is hidden at the symbol origin."""
    m = re.search(
        r'\(property\s+"' + re.escape(escape_sexpr_string(name)) + r'"\s+' + QUOTED_VALUE, block
    )
    if m:
        return block[: m.start(1)] + escape_sexpr_string(value) + block[m.end(1) :]
    at = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)", block)
    x, y = (at.group(1), at.group(2)) if at else ("0", "0")
    prop = (
        f'(property "{escape_sexpr_string(name)}" "{escape_sexpr_string(value)}"'
        f" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))"
    )
    anchor = re.search(r'\(pin\s+"|\(instances\b', block)
    pos = anchor.start() if anchor else block.rstrip().rfind(")")
    line_start = block.rfind("\n", 0, pos) + 1
    indent = block[line_start:pos]
    if indent.strip():
        return block[:pos] + prop + " " + block[pos:]
    return block[:line_start] + indent + prop + "\n" + block[line_start:]


class SymbolSchematicCommands:
    """Commands for schematic symbol instance manipulation."""

    def replace_instance_lib_ids(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Replace lib_id references in schematic instances.

        Params
        ------
        schematicPath : path to .kicad_sch file (required)
        mapping       : dict {old_full_lib_id: new_full_lib_id} (required),
                        e.g. {"eagle_import:1-RCL-EIGEN_C": "project_lib:C_100nF_0402"}.
                        Values are used verbatim, so one migration may target
                        several libraries. Mirror-variant instances
                        (…__m90 etc.) need their own entries — the variant
                        chooses both the target symbol and the angle offset.
        sourceLibrary : library prefix whose instances are candidates
                        (default "eagle_import", the Eagle importer's output)

        Only instances are rewritten; the lib_symbols section is preserved
        (use update_symbol_from_library to refresh definitions).
        """
        sch_path = params.get("schematicPath")
        mapping: Dict[str, str] = params.get("mapping", {})
        by_reference: Dict[str, str] = params.get("byReference", {})
        source_lib = params.get("sourceLibrary", "eagle_import")

        if not sch_path:
            return {"success": False, "error": "schematicPath is required"}
        if mapping and by_reference:
            return {"success": False, "error": "Pass mapping or byReference, not both"}
        if not mapping and not by_reference:
            return {"success": False, "error": "mapping or byReference dict is required"}
        if not Path(sch_path).exists():
            return {"success": False, "error": f"File not found: {sch_path}"}
        if by_reference:
            return self._relink_by_reference(
                Path(sch_path), by_reference, bool(params.get("allowPinChange", False))
            )

        try:
            content = Path(sch_path).read_text(encoding="utf-8")

            # ── find lib_symbols / instance boundary ──
            lib_start = content.find("(lib_symbols")
            if lib_start < 0:
                return {"success": False, "error": "lib_symbols section not found"}

            depth = 0
            lib_end = None
            for i in range(lib_start, len(content)):
                if content[i] == "(":
                    depth += 1
                elif content[i] == ")":
                    depth -= 1
                    if depth == 0:
                        lib_end = i
                        break
            if lib_end is None:
                return {"success": False, "error": "Cannot find end of lib_symbols"}

            lib_part = content[: lib_end + 1]
            inst_part = content[lib_end + 1 :]

            replaced = 0

            # ── per-instance replacer ──
            def _replace(m: "re.Match[str]") -> str:
                nonlocal replaced
                full = m.group(0)
                lib_id = m.group(1)
                x, y = m.group(2), m.group(3)
                old_angle = int(m.group(4))

                full_key = f"{source_lib}:{lib_id}"
                if full_key not in mapping:
                    return full

                # A mirror-variant suffix contributes an angle offset; the
                # mapping entry (keyed on the full suffixed id) supplies the
                # target lib_id.
                angle_offset = 0
                for suffix, offset in MIRROR_TO_ANGLE.items():
                    if lib_id.endswith(suffix):
                        angle_offset = offset
                        break

                # The mapping value is the complete replacement lib_id, used
                # verbatim — different entries may target different libraries.
                new_full_id = mapping[full_key]
                new_angle = (old_angle + angle_offset) % 360

                result = full.replace(full_key, new_full_id, 1)
                if angle_offset != 0:
                    result = result.replace(
                        f"(at {x} {y} {old_angle})",
                        f"(at {x} {y} {new_angle})",
                        1,
                    )
                replaced += 1
                return result

            # Both header layouts KiCad-family writers produce:
            #   (symbol\n  (lib_id "…")\n  (at x y a)   — KiCad GUI / Eagle import
            #   (symbol (lib_id "…") (at x y a)         — this repo's dynamic loader
            pattern = re.compile(
                r'\(symbol\s+\(lib_id "'
                + re.escape(source_lib)
                + r':([^"]+)"\)\s+\(at ([\d.-]+) ([\d.-]+) (\d+)\)',
            )

            inst_part = pattern.sub(_replace, inst_part)

            new_content = lib_part + inst_part
            with Path(sch_path).open("w", encoding="utf-8", newline="\n") as f:
                f.write(new_content)

            remaining = inst_part.count(f'(lib_id "{source_lib}:')

            return {
                "success": True,
                "replaced": replaced,
                "remaining": remaining,
                "message": (
                    f"Replaced {replaced} instance lib_ids, "
                    f"{remaining} {source_lib}: instance(s) remaining"
                ),
            }

        except Exception as e:
            logger.exception("replace_instance_lib_ids failed")
            return {"success": False, "error": f"Replacement error: {e}"}

    def _relink_by_reference(
        self, sch_path: Path, by_reference: Dict[str, str], allow_pin_change: bool
    ) -> Dict[str, Any]:
        """Point each listed reference at a new lib_id, in place.

        Every unit of the reference keeps its position, field positions and
        pin uuids, so no wire moves; only the lib_id changes and the new
        symbol is loaded into lib_symbols. A database part's row also sets
        Value, Footprint, Description and its mapped fields. A new symbol
        whose pins sit elsewhere than the old one's is refused unless
        *allow_pin_change*, since its wires would no longer meet the pins.
        Written once: any failure leaves the file unchanged.
        """
        from commands.dynamic_symbol_loader import DynamicSymbolLoader
        from commands.schematic_text_utils import _find_project_root

        try:
            original, newline = read_text_preserve_newline(sch_path)
            loader = DynamicSymbolLoader(project_path=_find_project_root(sch_path.parent))
            content = original
            for new_id in sorted(set(by_reference.values())):
                lib, sep, sym = new_id.partition(":")
                if not sep or not lib or not sym:
                    return {
                        "success": False,
                        "error": f"'{new_id}' is not a 'Library:Symbol' lib_id; file unchanged",
                    }
                try:
                    content = loader.inject_symbol_text(content, lib, sym)
                except ValueError as e:
                    return {"success": False, "error": f"Cannot load {new_id}: {e}; file unchanged"}

            relinked: Dict[str, Dict[str, Any]] = {}
            problems: List[str] = []
            for reference, new_id in by_reference.items():
                spans = _placed_blocks(content, reference)
                if not spans:
                    problems.append(f"{reference}: not found in this sheet")
                    continue
                lib, _, sym = new_id.partition(":")
                new_lib_block = _lib_symbol_block(content, new_id)
                db_part = None
                if loader.is_database_library(lib):
                    from commands.database_library import DatabaseLibrary
                    from utils.platform_helper import PlatformHelper

                    db_part = DatabaseLibrary(
                        loader.find_library_file(lib), PlatformHelper.load_kicad_env_vars()
                    ).part(sym)
                old_ids = set()
                for start, end in reversed(spans):
                    block = content[start : end + 1]
                    head = re.match(
                        r'\(symbol\s+(?:\(lib_name\s+"((?:[^"\\]|\\.)*)"\)\s*)?'
                        r'\(lib_id\s+"((?:[^"\\]|\\.)*)"\)',
                        block,
                    )
                    if not head:
                        problems.append(f"{reference}: unrecognised symbol header")
                        break
                    old_id = unescape_sexpr_string(head.group(2))
                    old_ids.add(old_id)
                    old_name = unescape_sexpr_string(head.group(1) or head.group(2))
                    unit_m = re.search(r"\(unit\s+(\d+)\)", block)
                    unit = int(unit_m.group(1)) if unit_m else 1
                    old_lib_block = _lib_symbol_block(content, old_name)
                    if not allow_pin_change:
                        old_pins = _unit_pins(old_lib_block, unit) if old_lib_block else None
                        new_pins = _unit_pins(new_lib_block or "", unit)
                        if old_pins != new_pins:
                            problems.append(
                                f"{reference} unit {unit}: pins of {new_id} differ from "
                                f"{old_name}'s (old {old_pins}, new {new_pins}); "
                                "pass allowPinChange to relink anyway"
                            )
                            break
                    new_block = (
                        f'(symbol (lib_id "{escape_sexpr_string(new_id)}")' + block[head.end() :]
                    )
                    if db_part is not None:
                        if db_part.footprints:
                            new_block = _set_instance_property(
                                new_block, "Footprint", db_part.footprints[0]
                            )
                        if db_part.description:
                            new_block = _set_instance_property(
                                new_block, "Description", db_part.description
                            )
                        for name, value, _visible, _show in db_part.fields:
                            if name != "Reference":
                                new_block = _set_instance_property(new_block, name, value)
                    content = content[:start] + new_block + content[end + 1 :]
                else:
                    relinked[reference] = {
                        "from": sorted(old_ids),
                        "to": new_id,
                        "units": len(spans),
                    }
            if problems:
                return {
                    "success": False,
                    "error": "Nothing was changed: " + "; ".join(problems),
                    "problems": problems,
                }
            write_text_atomic(sch_path, content, newline)
            return {
                "success": True,
                "relinked": relinked,
                "replaced": sum(r["units"] for r in relinked.values()),
                "message": f"Relinked {len(relinked)} reference(s) in {sch_path.name}",
            }
        except Exception as e:
            logger.exception("replace_instance_lib_ids byReference failed")
            return {"success": False, "error": f"Relink error: {e}; file unchanged"}
