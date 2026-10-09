"""Tests for the netlist-driven sync_schematic_to_board (commands/board_sync.py).

The parser and the kicad-skip collection helper run everywhere. The sync itself
builds real boards and footprints, so it is gated on KICAD_USE_REAL_PCBNEW=1
(same convention as test_real_pcbnew_matrix.py).
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.board_sync import NetlistError, SyncOptions, parse_netlist  # noqa: E402

SHEET = "/11111111-1111-1111-1111-111111111111/"
R1_UUID = "aaaaaaaa-0000-0000-0000-000000000001"
R3_UUID = "aaaaaaaa-0000-0000-0000-000000000003"

NETLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <components>
    <comp ref="R1">
      <value>10k</value>
      <footprint>Lib:R_0603</footprint>
      <property name="Sheetname" value="Power"/>
      <property name="Sheetfile" value="power.kicad_sch"/>
      <sheetpath names="/Power/" tstamps="{SHEET}"/>
      <tstamps>{R1_UUID}</tstamps>
    </comp>
    <comp ref="R3">
      <value>1k</value>
      <footprint>Lib:R_0603</footprint>
      <sheetpath names="/Power/" tstamps="{SHEET}"/>
      <tstamps>{R3_UUID}</tstamps>
    </comp>
    <comp ref="LOGO1">
      <value>logo</value>
      <footprint>Lib:Logo</footprint>
      <property name="exclude_from_board" value=""/>
      <sheetpath names="/" tstamps="/"/>
      <tstamps>bbbbbbbb-0000-0000-0000-000000000001</tstamps>
    </comp>
  </components>
  <nets>
    <net code="1" name="/Power/VIN"><node ref="R1" pin="1"/><node ref="R3" pin="1"/></net>
    <net code="2" name="GND"><node ref="R1" pin="2"/></net>
    <net code="3" name="unconnected-(R3-Pad2)"><node ref="R3" pin="2"/></net>
  </nets>
</export>
"""


class TestParseNetlist:
    def test_components_paths_and_sheet(self):
        nl = parse_netlist(NETLIST)
        r1 = nl.components["R1"]
        assert (r1.value, r1.footprint) == ("10k", "Lib:R_0603")
        assert r1.paths == [SHEET + R1_UUID]
        assert (r1.sheetname, r1.sheetfile) == ("Power", "power.kicad_sch")

    def test_exclude_from_board(self):
        nl = parse_netlist(NETLIST)
        assert nl.components["LOGO1"].on_board is False
        assert nl.components["R1"].on_board is True

    def test_pin_nets_include_unconnected_pins(self):
        nl = parse_netlist(NETLIST)
        assert nl.pin_net[("R1", "1")] == "/Power/VIN"
        assert nl.pin_net[("R3", "1")] == "/Power/VIN"
        assert nl.pin_net[("R3", "2")] == "unconnected-(R3-Pad2)"
        assert "GND" in nl.net_names

    def test_multi_unit_symbol_gives_one_path_per_unit(self):
        text = NETLIST.replace(f"<tstamps>{R1_UUID}</tstamps>", f"<tstamps>{R1_UUID} {R3_UUID}</tstamps>", 1)
        assert parse_netlist(text).components["R1"].paths == [SHEET + R1_UUID, SHEET + R3_UUID]

    def test_invalid_xml(self):
        with pytest.raises(NetlistError):
            parse_netlist("<export>")


class TestKicadSkipItems:
    """kicad-skip returns the bare element when a sheet has exactly one of a kind."""

    SCH = """(kicad_sch (version 20250114) (generator "eeschema") (uuid "1b5f1d4e-0000-0000-0000-000000000000")
  (paper "A4")
  (hierarchical_label "ONLY" (shape input) (at 10 10 0) (effects (font (size 1.27 1.27)) (justify left))
    (uuid "1b5f1d4e-0000-0000-0000-000000000001"))
  (label "A" (at 20 20 0) (effects (font (size 1.27 1.27)) (justify left bottom)) (uuid "1b5f1d4e-0000-0000-0000-000000000002"))
  (label "B" (at 30 30 0) (effects (font (size 1.27 1.27)) (justify left bottom)) (uuid "1b5f1d4e-0000-0000-0000-000000000003"))
)
"""

    def _sch(self, tmp_path):
        skip = pytest.importorskip("skip")
        path = tmp_path / "one.kicad_sch"
        path.write_text(self.SCH)
        return skip.Schematic(str(path))

    def test_single_element_is_a_one_item_list(self, tmp_path):
        from commands.schematic_handlers import _items

        labels = _items(self._sch(tmp_path), "hierarchical_label")
        assert [label.value for label in labels] == ["ONLY"]

    def test_collection_and_absent_kind(self, tmp_path):
        from commands.schematic_handlers import _items

        sch = self._sch(tmp_path)
        assert sorted(label.value for label in _items(sch, "label")) == ["A", "B"]
        assert _items(sch, "global_label") == []


@pytest.mark.real_pcbnew
class TestSyncBoardRealPcbnew:
    @pytest.fixture(autouse=True)
    def require_real_pcbnew(self):
        if os.environ.get("KICAD_USE_REAL_PCBNEW") != "1":
            pytest.skip("requires real pcbnew (set KICAD_USE_REAL_PCBNEW=1)")

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _footprint(board, ref, value, lib_id, pad_dx_mm=0.8):
        import pcbnew

        fp = pcbnew.FOOTPRINT(board)
        lib, name = lib_id.split(":", 1)
        # FootprintLoad returns a footprint whose LIB_ID has no library nickname
        fp.SetFPID(pcbnew.LIB_ID(lib if board is not None else "", name))
        fp.SetReference(ref)
        fp.SetValue(value)
        for number, x in (("1", -pad_dx_mm), ("2", pad_dx_mm)):
            pad = pcbnew.PAD(fp)
            pad.SetNumber(number)
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.8), pcbnew.FromMM(0.9)))
            pad.SetLayerSet(pcbnew.PAD.SMDMask())
            pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), 0))
            fp.Add(pad)
        return fp

    def _board(self):
        """R1 (old library link, old value, on net OLDVIN with a track) and R2 (no longer in the schematic)."""
        import pcbnew

        board = pcbnew.BOARD()
        oldvin = pcbnew.NETINFO_ITEM(board, "OLDVIN")
        board.Add(oldvin)
        r1 = self._footprint(board, "R1", "4k7", "OldLib:R_0603")
        r1.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(20)))
        r1.SetOrientationDegrees(90)
        board.Add(r1)
        r1.Flip(r1.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)  # on the back
        r1.SetOrientationDegrees(90)
        for pad in r1.Pads():
            if pad.GetNumber() == "1":
                pad.SetNet(oldvin)
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(0, 0))
        track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(5), 0))
        track.SetNet(oldvin)
        board.Add(track)
        r2 = self._footprint(board, "R2", "1M", "OldLib:R_0603")
        board.Add(r2)
        return board

    def _loader(self, board, pad_dx_mm=0.8):
        # Like pcbnew.FootprintLoad: a footprint that belongs to no board.
        return lambda lib_id: self._footprint(None, "REF**", "", lib_id, pad_dx_mm)

    def _refs(self, board):
        return {fp.GetReference(): fp for fp in board.GetFootprints()}

    # -- tests -----------------------------------------------------------------
    def test_dry_run_reports_but_does_not_modify(self):
        from commands.board_sync import sync_board

        board = self._board()
        report = sync_board(board, parse_netlist(NETLIST), self._loader(board), dry_run=True)
        assert report["matched_by_reference"] == ["R1"]
        assert [a["reference"] for a in report["footprints_added"]] == ["R3"]
        assert report["values_changed"] == [{"reference": "R1", "from": "4k7", "to": "10k"}]
        assert set(self._refs(board)) == {"R1", "R2"}
        assert self._refs(board)["R1"].GetValue() == "4k7"

    def test_apply_matches_updates_adds_and_assigns_nets(self):
        import pcbnew

        from commands.board_sync import sync_board

        board = self._board()
        report = sync_board(board, parse_netlist(NETLIST), self._loader(board))
        refs = self._refs(board)
        r1 = refs["R1"]
        # matched by reference, path written, value updated, placement kept
        assert r1.GetPath().AsString() == SHEET + R1_UUID
        assert r1.GetSheetname() == "Power"
        assert r1.GetValue() == "10k"
        assert pcbnew.ToMM(r1.GetPosition().x) == pytest.approx(10)
        assert r1.GetOrientationDegrees() == pytest.approx(90)
        assert r1.IsFlipped()  # the replacement went to the back, like the original
        # same pads: replaced from the library, new link
        assert report["footprints_replaced"][0]["same_pads"] is True
        assert r1.GetFPIDAsString() == "Lib:R_0603"
        # new symbol added, extra footprint kept (deletion not requested)
        assert "R3" in refs and refs["R3"].GetPath().AsString() == SHEET + R3_UUID
        assert "R2" in refs and report["extra_footprints"][0]["kept"] == "deletion not requested"
        # pad nets come from the netlist, including the unconnected pin
        nets = {(ref, p.GetNumber()): p.GetNetname() for ref, fp in refs.items() if ref != "R2" for p in fp.Pads()}
        assert nets == {
            ("R1", "1"): "/Power/VIN",
            ("R1", "2"): "GND",
            ("R3", "1"): "/Power/VIN",
            ("R3", "2"): "unconnected-(R3-Pad2)",
        }
        # the track on the renamed net follows it
        assert report["copper_nets_renamed"] == [{"from": "OLDVIN", "to": "/Power/VIN", "items": 1}]
        assert [t.GetNetname() for t in board.GetTracks()] == ["/Power/VIN"]

    def test_second_sync_matches_by_uuid_and_changes_nothing(self):
        from commands.board_sync import sync_board

        board = self._board()
        sync_board(board, parse_netlist(NETLIST), self._loader(board))
        again = sync_board(board, parse_netlist(NETLIST), self._loader(board), dry_run=True)
        assert again["matched_by_uuid"] == ["R1", "R3"]
        assert again["footprints_replaced"] == []
        assert again["pads_net_changed"] == 0
        assert not again["footprints_added"] and not again["footprints_replaced"]

    def test_relink_keeps_geometry_and_reports_different_pads(self):
        import pcbnew

        from commands.board_sync import sync_board

        board = self._board()
        report = sync_board(
            board,
            parse_netlist(NETLIST),
            self._loader(board, pad_dx_mm=1.0),  # library version has wider pad spacing
            SyncOptions(footprint_changes="relink"),
        )
        r1 = self._refs(board)["R1"]
        assert report["footprints_relinked"][0]["same_pads"] is False
        assert r1.GetFPIDAsString() == "Lib:R_0603"
        xs = sorted(round(pcbnew.ToMM(p.GetFPRelativePosition().x), 3) for p in r1.Pads())
        assert xs == [-0.8, 0.8]  # board geometry untouched

    def test_delete_extra_footprints_skips_locked_and_blank(self):
        from commands.board_sync import sync_board

        board = self._board()
        blank = self._footprint(board, "", "", "Lib:Logo")
        board.Add(blank)
        report = sync_board(
            board, parse_netlist(NETLIST), self._loader(board), SyncOptions(delete_extra_footprints=True)
        )
        assert [d["reference"] for d in report["footprints_deleted"]] == ["R2"]
        assert "R2" not in self._refs(board)
        assert any(e["kept"] == "blank reference" for e in report["extra_footprints"])

    def test_unloadable_footprint_is_reported_not_added(self):
        from commands.board_sync import sync_board

        board = self._board()

        def loader(lib_id):
            raise LookupError("library 'Lib' is not in any fp-lib-table")

        report = sync_board(board, parse_netlist(NETLIST), loader, SyncOptions(footprint_changes="replace"))
        assert report["footprints_not_added"][0]["reference"] == "R3"
        assert "not in any fp-lib-table" in report["footprints_not_added"][0]["reason"]
        assert report["footprint_change_errors"][0]["reference"] == "R1"
        assert "R3" not in self._refs(board)
