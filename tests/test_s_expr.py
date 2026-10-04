"""Phase 1: s_expr.py -- parsing, safe mutation, byte-exact round trips."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core import s_expr as sx
from src.core.s_expr import SExprError
from tests import kicad_fixtures as kf


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------

def test_find_matching_paren_ignores_parens_inside_strings():
    t = '(footprint "Mini-Fit(MX4.2)_HX-5569" (pad "1"))'
    assert sx.find_matching_paren(t, 0) == len(t) - 1


def test_find_matching_paren_ignores_escaped_quote_in_string():
    t = '(property "Desc" "a \\" b (c" (at 0 0))'
    assert sx.find_matching_paren(t, 0) == len(t) - 1


def test_find_matching_paren_returns_minus_one_when_unbalanced():
    assert sx.find_matching_paren('(symbol "X"', 0) == -1


def test_quote_round_trips_backslashes_and_quotes():
    for value in ['plain', 'a"b', 'C:\\Users\\x', 'back\\\\slash', '${KICAD_CUSTOM_LIB}/a.step']:
        assert sx.unquote(sx.quote(value)) == value


def test_children_skips_nested_and_returns_direct_descendants_only():
    t = '(footprint "F" (pad "1" (at 0 0)) (model "m.step" (offset (xyz 1 2 3))))'
    heads = [h for h, _o, _c in sx.children(t, 0)]
    assert heads == ["pad", "model"]


def test_string_tokens_does_not_descend_into_child_nodes():
    t = '(property "Footprint" "Lib:FP" (at 0 0 0) (effects (font (size 1.27 1.27))))'
    assert [v for v, _s, _e in sx.string_tokens(t, 0)] == ["Footprint", "Lib:FP"]


def test_apply_edits_rejects_overlapping_spans():
    with pytest.raises(SExprError, match="overlapping"):
        sx.apply_edits("abcdef", [(0, 3, "X"), (2, 5, "Y")])


def test_apply_edits_applies_back_to_front():
    assert sx.apply_edits("0123456789", [(0, 1, "a"), (8, 10, "z")]) == "a1234567z"


def test_apply_edits_does_not_reinterpret_backslashes_as_group_refs():
    """The exact failure mode of the old re.sub-with-replacement-string code."""
    out = sx.apply_edits('(model "old")', [(7, 12, sx.quote(r"C:\1\2\g<0>"))])
    assert sx.unquote(out[7:-1]) == r"C:\1\2\g<0>"


# --------------------------------------------------------------------------
# File I/O
# --------------------------------------------------------------------------

def test_read_text_strips_bom_and_normalises_crlf(tmp_path):
    p = tmp_path / "x.kicad_mod"
    p.write_bytes(b'\xef\xbb\xbf(footprint "X"\r\n\t(layer "F.Cu")\r\n)\r\n')
    text = sx.read_text(p)
    assert not text.startswith("\ufeff")
    assert "\r" not in text
    assert text.endswith(')\n')


def test_write_text_always_emits_lf(tmp_path):
    p = tmp_path / "out.kicad_mod"
    sx.write_text(p, '(footprint "X"\n)\n')
    assert b"\r" not in p.read_bytes()


def test_crlf_input_becomes_lf_output(tmp_path):
    src = tmp_path / "in.kicad_mod"
    src.write_bytes(kf.footprint_text("FP").replace("\n", "\r\n").encode())
    sx.write_text(src, sx.read_text(src))
    assert b"\r\n" not in src.read_bytes()


def test_read_write_round_trip_is_byte_identical(tmp_path):
    """A no-op edit must not perturb a single byte."""
    for name, builder in (
        ("s.kicad_sym", lambda p: kf.write_symbol(p, "TPA3255DDV", "Amp:FP")),
        ("f.kicad_mod", lambda p: kf.write_footprint(p, "FP", model="${KICAD_CUSTOM_LIB}/a.step")),
    ):
        p = tmp_path / name
        builder(p)
        before = p.read_bytes()
        sx.write_text(p, sx.read_text(p))
        assert p.read_bytes() == before, name


def test_write_text_is_atomic_and_leaves_no_temp_files(tmp_path):
    p = tmp_path / "x.kicad_mod"
    sx.write_text(p, "(footprint \"X\"\n)\n")
    assert sorted(q.name for q in tmp_path.iterdir()) == ["x.kicad_mod"]


# --------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------

def test_top_level_symbols_finds_all_of_them_not_just_the_first():
    names = ["CONN-5MM-4P", "Inductor_2*10uH_Leaded_7W15", "TPA3255DDV"]
    text = kf.symbol_lib([kf.symbol_block(n) for n in names])
    assert [n for n, _o, _c in sx.top_level_symbols(text)] == names


def test_top_level_symbols_does_not_return_unit_subsymbols():
    text = kf.symbol_lib([kf.symbol_block("LM358", units=("0_1", "1_1", "2_1"))])
    assert [n for n, _o, _c in sx.top_level_symbols(text)] == ["LM358"]


def test_get_property_is_scoped_to_one_symbol_block():
    """
    The old code regex-matched the first Footprint in the file, so in a
    multi-symbol bundle every symbol reported the same footprint.
    """
    text = kf.symbol_lib([
        kf.symbol_block("A", "Cat:FP_A"),
        kf.symbol_block("B", "Cat:FP_B"),
    ])
    spans = {n: o for n, o, _c in sx.top_level_symbols(text)}
    assert sx.get_property(text, spans["A"], "Footprint") == "Cat:FP_A"
    assert sx.get_property(text, spans["B"], "Footprint") == "Cat:FP_B"


def test_set_property_changes_only_the_targeted_symbol():
    text = kf.symbol_lib([kf.symbol_block("A", "Old:FP"), kf.symbol_block("B", "Old:FP")])
    spans = {n: o for n, o, _c in sx.top_level_symbols(text)}
    out = sx.set_property(text, spans["B"], "Footprint", "New:FP")
    spans2 = {n: o for n, o, _c in sx.top_level_symbols(out)}
    assert sx.get_property(out, spans2["A"], "Footprint") == "Old:FP"
    assert sx.get_property(out, spans2["B"], "Footprint") == "New:FP"


def test_set_property_preserves_everything_else_on_the_line():
    text = kf.symbol_lib([kf.symbol_block("A", "Old:FP")])
    out = sx.set_symbol_footprint(text, "A", "New:FP")
    # Only the value token changed; the (at ...) and (hide yes) survive.
    assert out.count("(hide yes)") == text.count("(hide yes)")
    assert "(at 0 5.08 0)" in out
    assert len(out.splitlines()) == len(text.splitlines())


def test_set_property_accepts_a_value_containing_a_backslash():
    text = kf.symbol_lib([kf.symbol_block("A", "Old:FP")])
    out = sx.set_symbol_footprint(text, "A", r"Cat:FP\with\backslash")
    span = sx.find_symbol(out, "A")
    assert sx.get_property(out, span[0], "Footprint") == r"Cat:FP\with\backslash"


def test_extends_target_reads_the_parent_name():
    text = kf.symbol_lib([kf.symbol_block("B250R", extends="B40R")])
    span = sx.find_symbol(text, "B250R")
    assert sx.extends_target(text, span[0])[0] == "B40R"


# --------------------------------------------------------------------------
# Renaming symbols
# --------------------------------------------------------------------------

def test_rename_symbol_rewrites_name_units_and_value():
    text = kf.symbol_lib([kf.symbol_block("OLD", "Cat:FP", units=("0_1", "1_1"))])
    out = sx.rename_symbol(text, "OLD", "NEW")
    assert [n for n, _o, _c in sx.top_level_symbols(out)] == ["NEW"]
    span = sx.find_symbol(out, "NEW")
    assert [n for n, _o, _c in sx.unit_subsymbols(out, span[0])] == ["NEW_0_1", "NEW_1_1"]
    assert sx.get_property(out, span[0], "Value") == "NEW"
    assert "OLD" not in out


def test_rename_symbol_leaves_a_customised_value_alone():
    """Value is only rewritten when it still mirrors the symbol name."""
    text = kf.symbol_lib([kf.symbol_block("OLD")])
    text = sx.set_symbol_footprint(text, "OLD", "Cat:FP")
    span = sx.find_symbol(text, "OLD")
    text = sx.set_property(text, span[0], "Value", "Custom Label")
    out = sx.rename_symbol(text, "OLD", "NEW")
    assert sx.get_property(out, sx.find_symbol(out, "NEW")[0], "Value") == "Custom Label"


def test_rename_symbol_touches_only_the_named_symbol():
    text = kf.symbol_lib([kf.symbol_block("KEEP", "Cat:FP"), kf.symbol_block("OLD", "Cat:FP")])
    out = sx.rename_symbol(text, "OLD", "NEW")
    assert [n for n, _o, _c in sx.top_level_symbols(out)] == ["KEEP", "NEW"]


def test_rename_symbol_does_not_corrupt_a_name_that_is_a_prefix_of_another():
    """'KF2EDG-5.08_4P' and 'KF2EDG-5.08_4P_1' both exist in the real bundle."""
    text = kf.symbol_lib([
        kf.symbol_block("KF2EDG-5.08_4P"),
        kf.symbol_block("KF2EDG-5.08_4P_1"),
    ])
    out = sx.rename_symbol(text, "KF2EDG-5.08_4P", "KF2EDG_5P08_4P")
    assert [n for n, _o, _c in sx.top_level_symbols(out)] == [
        "KF2EDG_5P08_4P", "KF2EDG-5.08_4P_1",
    ]


def test_rename_symbol_raises_for_an_unknown_name():
    with pytest.raises(SExprError, match="not found"):
        sx.rename_symbol(kf.symbol_lib([kf.symbol_block("A")]), "MISSING", "X")


def test_retarget_extends_rewrites_sibling_references_and_counts_them():
    """The cross-file case from AGENTS.md 6.2."""
    sibling = kf.symbol_lib([kf.symbol_block("B250R", extends="B40R")])
    out, n = sx.retarget_extends(sibling, "B40R", "B40R_V2")
    assert n == 1
    assert sx.extends_target(out, sx.find_symbol(out, "B250R")[0])[0] == "B40R_V2"


def test_retarget_extends_is_a_no_op_for_unrelated_files():
    text = kf.symbol_lib([kf.symbol_block("X", extends="SOMETHING_ELSE")])
    out, n = sx.retarget_extends(text, "B40R", "B40R_V2")
    assert (out, n) == (text, 0)


# --------------------------------------------------------------------------
# Footprints and models
# --------------------------------------------------------------------------

def test_rename_footprint_rewrites_the_declaration():
    text = kf.footprint_text("OLD_FP")
    out = sx.rename_footprint(text, "OLD_FP", "NEW_FP")
    assert sx.footprint_span(out)[0] == "NEW_FP"


def test_rename_footprint_refuses_a_wrong_old_name():
    with pytest.raises(SExprError, match="is named"):
        sx.rename_footprint(kf.footprint_text("A"), "B", "C")


def test_all_models_returns_every_block_not_just_the_first():
    text = kf.footprint_text("FP", models=["a.step", "b.step", "c.wrl"])
    assert [m.path for m in sx.all_models(text)] == ["a.step", "b.step", "c.wrl"]


def test_all_models_is_empty_for_a_footprint_without_a_model():
    """The real SOP63P810X120-44N.kicad_mod has no model block at all."""
    assert sx.all_models(kf.footprint_text("FP")) == []


def test_all_models_reads_offset_scale_rotate():
    text = kf.footprint_text("FP", model="a.step", offset=(1.5, -2.0, 0.25), rotate=(0, 0, 90))
    m = sx.all_models(text)[0]
    assert m.offset == (1.5, -2.0, 0.25)
    assert m.scale == (1.0, 1.0, 1.0)
    assert m.rotate == (0.0, 0.0, 90.0)


def test_model_filename_is_separator_agnostic():
    text = kf.footprint_text("FP", model="${KICAD_CUSTOM_LIB}/3dmodels/C.3dshapes/M.step")
    assert sx.all_models(text)[0].filename == "M.step"
    text2 = kf.footprint_text("FP", model=r"C:\models\M.step")
    assert sx.all_models(text2)[0].filename == "M.step"


def test_set_model_path_preserves_a_hand_tuned_alignment():
    """
    The reason offsets live in the .kicad_mod: a category move must not
    discard alignment the owner adjusted in the footprint editor.
    """
    text = kf.footprint_text("FP", model="old.step", offset=(0.1, 0.2, 0.3), rotate=(0, 0, 90))
    out = sx.set_model_path(text, 0, "${KICAD_CUSTOM_LIB}/3dmodels/New.3dshapes/new.step")
    m = sx.all_models(out)[0]
    assert m.path == "${KICAD_CUSTOM_LIB}/3dmodels/New.3dshapes/new.step"
    assert (m.offset, m.rotate) == ((0.1, 0.2, 0.3), (0.0, 0.0, 90.0))


def test_set_model_path_targets_one_of_several_models():
    text = kf.footprint_text("FP", models=["a.step", "b.step"])
    out = sx.set_model_path(text, 1, "z.step")
    assert [m.path for m in sx.all_models(out)] == ["a.step", "z.step"]


def test_set_model_path_rejects_an_out_of_range_index():
    with pytest.raises(SExprError, match="out of range"):
        sx.set_model_path(kf.footprint_text("FP"), 0, "x.step")


def test_add_model_creates_a_block_and_keeps_the_file_parseable():
    text = kf.footprint_text("FP")
    out = sx.add_model(text, "${KICAD_CUSTOM_LIB}/3dmodels/C.3dshapes/M.step", rotate=(0, 0, 90))
    models = sx.all_models(out)
    assert len(models) == 1
    assert models[0].rotate == (0.0, 0.0, 90.0)
    assert sx.find_matching_paren(out, sx.root_node(out)[0]) == sx.root_node(out)[1]
    assert out.endswith(")\n")


def test_set_or_add_model_handles_both_the_present_and_absent_cases():
    with_model = kf.footprint_text("FP", model="old.step")
    assert sx.all_models(sx.set_or_add_model(with_model, "new.step"))[0].path == "new.step"
    without = kf.footprint_text("FP")
    assert sx.all_models(sx.set_or_add_model(without, "new.step"))[0].path == "new.step"


# --------------------------------------------------------------------------
# Deprecated shims still behave
# --------------------------------------------------------------------------

def test_deprecated_patch_symbol_footprint_now_patches_every_symbol(tmp_path):
    """
    Regression for plan problem #9: the old implementation patched only the
    first Footprint property in the file.
    """
    p = tmp_path / "multi.kicad_sym"
    kf.write_multi_symbol(p, ["A", "B", "C"], footprint_prefix="Old")
    assert sx.patch_symbol_footprint(p, "New:FP") is True
    text = sx.read_text(p)
    values = [
        sx.get_property(text, o, "Footprint") for _n, o, _c in sx.top_level_symbols(text)
    ]
    assert values == ["New:FP"] * 3
