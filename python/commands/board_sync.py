"""Update a board from its schematic the way KiCad's F8 ("Update PCB from Schematic") does.

Connectivity comes from kicad-cli's netlist (``sch export netlist --format
kicadxml``), which is KiCad's own connectivity engine: bus members, hierarchical
names, driver priority and global labels are all resolved exactly as eeschema
does. Re-deriving it from label positions disagreed with KiCad on harness buses
and label priority.

``sync_board`` matches footprints to symbols, then reports and (unless
``dry_run``) applies every change F8 makes:

* match by symbol UUID path, and optionally by reference for footprints whose
  path matches no symbol (KiCad's "Re-link footprints to schematic symbols based
  on their reference designators"); the symbol path, sheet name and sheet file are
  written into every matched footprint so the next sync matches by UUID;
* reference and value updates;
* footprints whose library link changed: replaced from the library (keeping
  position, orientation, side, lock and field placement), relinked without
  touching their geometry, or kept;
* new footprints for symbols missing from the board, placed in a row beside the
  board outline;
* footprints whose symbol is gone, reported and optionally deleted (never locked
  ones, never ones with a blank reference such as logos);
* pad nets from the netlist, with tracks, vias and zones following a net that
  was renamed (every pad of the old net moved to the same new net).
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from utils.board_items import delete_board_item

logger = logging.getLogger("kicad_interface")

FOOTPRINT_CHANGE_MODES = ("replace", "relink", "keep")


class NetlistError(RuntimeError):
    """kicad-cli could not produce or we could not read the netlist."""


@dataclass
class NetlistComponent:
    ref: str
    value: str
    footprint: str
    sheet_tstamps: str
    unit_uuids: List[str]
    sheetname: str = ""
    sheetfile: str = ""
    on_board: bool = True

    @property
    def paths(self) -> List[str]:
        """Board footprint paths that belong to this symbol, one per unit."""
        prefix = self.sheet_tstamps if self.sheet_tstamps.endswith("/") else self.sheet_tstamps + "/"
        return [prefix + u for u in self.unit_uuids]


@dataclass
class Netlist:
    components: Dict[str, NetlistComponent]
    #: (reference, pin number) -> net name, for every pin KiCad netlists
    pin_net: Dict[Tuple[str, str], str]
    net_names: List[str]


@dataclass
class SyncOptions:
    match_by_reference: bool = True
    footprint_changes: str = "replace"
    update_values: bool = True
    delete_extra_footprints: bool = False
    rename_track_nets: bool = True


def export_netlist(kicad_cli: str, schematic_path: str, timeout: int = 180) -> str:
    """The kicadxml netlist of *schematic_path* as text."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "netlist.xml"
        cmd = [kicad_cli, "sch", "export", "netlist", "--format", "kicadxml", "--output", str(out), schematic_path]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise NetlistError(f"kicad-cli netlist export could not run: {e}") from e
        if result.returncode != 0 or not out.is_file():
            raise NetlistError(
                f"kicad-cli netlist export failed (exit {result.returncode}): "
                f"{(result.stderr or result.stdout).strip()}"
            )
        return out.read_text(encoding="utf-8")


def parse_netlist(xml_text: str) -> Netlist:
    """Components and pin-to-net assignments from a kicadxml netlist."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise NetlistError(f"netlist is not valid XML: {e}") from e

    components: Dict[str, NetlistComponent] = {}
    for comp in root.findall("./components/comp"):
        ref = comp.get("ref", "")
        props = {p.get("name", ""): p.get("value", "") for p in comp.findall("property")}
        sheetpath = comp.find("sheetpath")
        tstamps = (comp.findtext("tstamps") or "").replace(",", " ").split()
        components[ref] = NetlistComponent(
            ref=ref,
            value=comp.findtext("value") or "",
            footprint=(comp.findtext("footprint") or "").strip(),
            sheet_tstamps=sheetpath.get("tstamps", "/") if sheetpath is not None else "/",
            unit_uuids=tstamps,
            sheetname=props.get("Sheetname", ""),
            sheetfile=props.get("Sheetfile", ""),
            on_board="exclude_from_board" not in props,
        )

    pin_net: Dict[Tuple[str, str], str] = {}
    net_names: List[str] = []
    for net in root.findall("./nets/net"):
        name = net.get("name", "")
        net_names.append(name)
        for node in net.findall("node"):
            pin_net[(node.get("ref", ""), node.get("pin", ""))] = name
    return Netlist(components=components, pin_net=pin_net, net_names=net_names)


def _path_of(fp: Any) -> str:
    try:
        path = fp.GetPath().AsString()
    except Exception:
        return ""
    return path if isinstance(path, str) else ""


def _pad_signature(fp: Any) -> List[Tuple[str, float, float, float, float]]:
    """Pads in footprint-local coordinates, to tell whether two footprints are the same land pattern."""
    import pcbnew

    # A footprint on the back is stored mirrored in Y; undo that so a placed
    # footprint compares equal to its library original.
    y_sign = -1 if fp.IsFlipped() else 1
    sig = []
    for pad in fp.Pads():
        pos = pad.GetFPRelativePosition()
        pos = pcbnew.VECTOR2I(pos.x, y_sign * pos.y)
        try:
            size = pad.GetSize(pcbnew.F_Cu)
        except TypeError:
            size = pad.GetSize()
        sx, sy = size.x, size.y
        sig.append(
            (
                pad.GetNumber(),
                round(pcbnew.ToMM(pos.x), 3),
                round(pcbnew.ToMM(pos.y), 3),
                round(pcbnew.ToMM(sx), 3),
                round(pcbnew.ToMM(sy), 3),
            )
        )
    return sorted(sig)


def _copy_field(src: Any, dst: Any) -> None:
    """Copy a text field's placement and look (not its text) from *src* to *dst*."""
    for getter, setter in (
        ("GetPosition", "SetPosition"),
        ("GetTextAngle", "SetTextAngle"),
        ("GetLayer", "SetLayer"),
        ("IsVisible", "SetVisible"),
        ("GetTextSize", "SetTextSize"),
        ("GetTextThickness", "SetTextThickness"),
        ("IsMirrored", "SetMirrored"),
    ):
        try:
            getattr(dst, setter)(getattr(src, getter)())
        except Exception:
            pass


def _replace_footprint(board: Any, old: Any, new: Any) -> None:
    """Put *new* where *old* is, the way F8's footprint exchange does, and remove *old*."""
    import pcbnew

    # On the board first: FOOTPRINT::Flip needs the board's layer setup and
    # segfaults on a footprint that has no parent board.
    board.Add(new)
    new.SetPosition(old.GetPosition())
    if old.IsFlipped() != new.IsFlipped():
        new.Flip(new.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
    new.SetOrientation(old.GetOrientation())
    new.SetPosition(old.GetPosition())
    new.SetLocked(old.IsLocked())
    new.SetReference(old.GetReference())
    new.SetValue(old.GetValue())
    _copy_field(old.Reference(), new.Reference())
    _copy_field(old.Value(), new.Value())
    new.SetPath(old.GetPath())
    for name in ("Sheetname", "Sheetfile"):
        try:
            getattr(new, f"Set{name}")(getattr(old, f"Get{name}")())
        except Exception:
            pass
    delete_board_item(board, old)


def sync_board(
    board: Any,
    netlist: Netlist,
    load_footprint: Callable[[str], Any],
    options: Optional[SyncOptions] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Bring *board* in line with *netlist*. Returns a report of every change.

    *load_footprint* takes "Library:Footprint" and returns a fresh FOOTPRINT
    (raising or returning None when it cannot). With *dry_run* the board is not
    modified.
    """
    import pcbnew

    opts = options or SyncOptions()
    if opts.footprint_changes not in FOOTPRINT_CHANGE_MODES:
        raise ValueError(f"footprint_changes must be one of {FOOTPRINT_CHANGE_MODES}")

    wanted = {
        ref: c for ref, c in netlist.components.items() if c.on_board and ref and not ref.startswith("#")
    }
    footprints = list(board.GetFootprints())

    # ---- matching ---------------------------------------------------------
    path_to_ref: Dict[str, str] = {}
    for c in wanted.values():
        for p in c.paths:
            path_to_ref.setdefault(p, c.ref)
    matched: Dict[str, Any] = {}  # ref -> footprint
    how: Dict[str, str] = {}
    used: Set[int] = set()
    for fp in footprints:
        ref = path_to_ref.get(_path_of(fp))
        if ref and ref not in matched:
            matched[ref] = fp
            how[ref] = "uuid"
            used.add(id(fp))
    if opts.match_by_reference:
        by_ref: Dict[str, Any] = {}
        for fp in footprints:
            if id(fp) not in used and fp.GetReference():
                by_ref.setdefault(fp.GetReference(), fp)
        for ref in wanted:
            if ref not in matched and ref in by_ref:
                matched[ref] = by_ref[ref]
                how[ref] = "reference"
                used.add(id(by_ref[ref]))

    report: Dict[str, Any] = {
        "dry_run": dry_run,
        "matched_by_uuid": sorted(r for r, h in how.items() if h == "uuid"),
        "matched_by_reference": sorted(r for r, h in how.items() if h == "reference"),
        "paths_updated": [],
        "references_changed": [],
        "values_changed": [],
        "footprints_replaced": [],
        "footprints_relinked": [],
        "footprint_changes_kept": [],
        "footprint_change_errors": [],
        "footprints_added": [],
        "footprints_not_added": [],
        "footprints_deleted": [],
        "extra_footprints": [],
        "nets_added": [],
        "copper_nets_renamed": [],
        "nets_split": [],
        "pads_net_changed": 0,
        "pads_without_pin": [],
    }

    # Pad nets as they are now, recorded before any footprint is replaced: a
    # replacement's pads start without nets, and renamed-net detection needs
    # to know what each pad was on.
    old_pad_net: Dict[Tuple[str, str], str] = {
        (ref, pad.GetNumber()): pad.GetNetname() for ref, fp in matched.items() for pad in fp.Pads()
    }

    # ---- per-footprint updates -------------------------------------------
    final_fp: Dict[str, Any] = {}  # ref -> footprint that carries it after the sync
    for ref, fp in sorted(matched.items()):
        comp = wanted[ref]
        final_fp[ref] = fp
        new_path = comp.paths[0] if comp.paths else ""
        if new_path and _path_of(fp) != new_path:
            report["paths_updated"].append(ref)
            if not dry_run:
                fp.SetPath(pcbnew.KIID_PATH(new_path))
        if not dry_run:
            if comp.sheetname:
                fp.SetSheetname(comp.sheetname)
            if comp.sheetfile:
                fp.SetSheetfile(comp.sheetfile)
        if fp.GetReference() != ref:
            report["references_changed"].append({"from": fp.GetReference(), "to": ref})
            if not dry_run:
                fp.SetReference(ref)
        if opts.update_values and comp.value and fp.GetValue() != comp.value:
            report["values_changed"].append({"reference": ref, "from": fp.GetValue(), "to": comp.value})
            if not dry_run:
                fp.SetValue(comp.value)

        current = fp.GetFPIDAsString()
        if comp.footprint and comp.footprint != current:
            entry: Dict[str, Any] = {"reference": ref, "from": current, "to": comp.footprint}
            new_fp = None
            try:
                new_fp = load_footprint(comp.footprint)
            except Exception as e:
                entry["error"] = str(e)
            if new_fp is None and "error" not in entry:
                entry["error"] = f"footprint {comp.footprint} could not be loaded"
            if new_fp is not None:
                entry["same_pads"] = _pad_signature(new_fp) == _pad_signature(fp)
            if opts.footprint_changes == "keep":
                report["footprint_changes_kept"].append(entry)
            elif opts.footprint_changes == "relink":
                report["footprints_relinked"].append(entry)
                if not dry_run:
                    lib, name = comp.footprint.split(":", 1)
                    fp.SetFPID(pcbnew.LIB_ID(lib, name))
            elif new_fp is None:
                report["footprint_change_errors"].append(entry)
            else:
                report["footprints_replaced"].append(entry)
                if not dry_run:
                    # A loaded footprint knows only its name; give it the full link,
                    # or the next sync sees a changed footprint again.
                    lib, name = comp.footprint.split(":", 1)
                    new_fp.SetFPID(pcbnew.LIB_ID(lib, name))
                    _replace_footprint(board, fp, new_fp)
                    final_fp[ref] = new_fp

    # ---- footprints to add -----------------------------------------------
    missing = sorted(ref for ref in wanted if ref not in matched)
    origin_x = origin_y = 0
    try:
        bbox = board.GetBoardEdgesBoundingBox()
        if bbox.GetWidth() > 0:
            origin_x, origin_y = bbox.GetRight() + pcbnew.FromMM(5), bbox.GetTop()
    except Exception:
        pass
    slot = 0
    for ref in missing:
        comp = wanted[ref]
        if not comp.footprint or ":" not in comp.footprint:
            report["footprints_not_added"].append({"reference": ref, "reason": "symbol has no footprint"})
            continue
        try:
            new_fp = load_footprint(comp.footprint)
        except Exception as e:
            new_fp, reason = None, str(e)
        else:
            reason = f"footprint {comp.footprint} could not be loaded"
        if new_fp is None:
            report["footprints_not_added"].append({"reference": ref, "footprint": comp.footprint, "reason": reason})
            continue
        report["footprints_added"].append({"reference": ref, "footprint": comp.footprint})
        if dry_run:
            continue
        lib, name = comp.footprint.split(":", 1)
        new_fp.SetFPID(pcbnew.LIB_ID(lib, name))
        new_fp.SetReference(ref)
        new_fp.SetValue(comp.value)
        if comp.paths:
            new_fp.SetPath(pcbnew.KIID_PATH(comp.paths[0]))
        if comp.sheetname:
            new_fp.SetSheetname(comp.sheetname)
        if comp.sheetfile:
            new_fp.SetSheetfile(comp.sheetfile)
        step = pcbnew.FromMM(5)
        new_fp.SetPosition(pcbnew.VECTOR2I(origin_x + (slot % 10) * step, origin_y + (slot // 10) * step))
        slot += 1
        board.Add(new_fp)
        final_fp[ref] = new_fp

    # ---- footprints whose symbol is gone ---------------------------------
    for fp in footprints:
        if id(fp) in used:
            continue
        ref = fp.GetReference()
        entry = {"reference": ref, "footprint": fp.GetFPIDAsString(), "locked": bool(fp.IsLocked())}
        if not ref or fp.IsLocked() or not opts.delete_extra_footprints:
            entry["kept"] = "blank reference" if not ref else ("locked" if fp.IsLocked() else "deletion not requested")
            report["extra_footprints"].append(entry)
            continue
        report["footprints_deleted"].append(entry)
        if not dry_run:
            delete_board_item(board, fp)

    # ---- nets --------------------------------------------------------------
    nets_by_name = board.GetNetInfo().NetsByName()
    for name in netlist.net_names:
        if name and not nets_by_name.has_key(name):
            report["nets_added"].append(name)
            if not dry_run:
                board.Add(pcbnew.NETINFO_ITEM(board, name))
    if not dry_run:
        nets_by_name = board.GetNetInfo().NetsByName()

    moves: Dict[str, Set[str]] = {}
    pad_updates: List[Tuple[Any, str]] = []
    for ref, fp in final_fp.items():
        for pad in fp.Pads():
            number = pad.GetNumber()
            if not number:
                continue
            new_net = netlist.pin_net.get((ref, number), "")
            if (ref, number) not in netlist.pin_net:
                report["pads_without_pin"].append(f"{ref}/{number}")
            old_net = old_pad_net.get((ref, number), pad.GetNetname())
            if pad.GetNetname() != new_net:
                pad_updates.append((pad, new_net))
            if old_net:
                moves.setdefault(old_net, set()).add(new_net)
    # a net all of whose pads moved to one new net was renamed: its copper follows
    renames = {old: next(iter(new)) for old, new in moves.items() if len(new) == 1 and old not in new and next(iter(new))}
    report["nets_split"] = sorted(old for old, new in moves.items() if len(new) > 1)
    report["pads_net_changed"] = len(pad_updates)

    if opts.rename_track_nets:
        copper = list(board.GetTracks()) + list(board.Zones())
        for old, new in sorted(renames.items()):
            count = sum(1 for item in copper if item.GetNetname() == old)
            if count:
                report["copper_nets_renamed"].append({"from": old, "to": new, "items": count})
                if not dry_run:
                    for item in copper:
                        if item.GetNetname() == old:
                            item.SetNet(nets_by_name[new])

    if not dry_run:
        for pad, net in pad_updates:
            pad.SetNet(nets_by_name[net] if net else board.GetNetInfo().GetNetItem(0))

    report["summary"] = (
        f"{len(report['matched_by_uuid'])} matched by UUID, {len(report['matched_by_reference'])} by reference; "
        f"{len(report['footprints_added'])} added, {len(report['footprints_not_added'])} could not be added; "
        f"{len(report['footprints_replaced'])} replaced, {len(report['footprints_relinked'])} relinked, "
        f"{len(report['footprint_change_errors'])} replacement errors; "
        f"{len(report['footprints_deleted'])} deleted, {len(report['extra_footprints'])} extra kept; "
        f"{len(report['values_changed'])} values changed; {report['pads_net_changed']} pad nets changed, "
        f"{len(report['copper_nets_renamed'])} copper nets renamed, {len(report['nets_split'])} nets split"
    )
    return report
