"""Phase 1: naming.py -- what is a usable category / item name."""

from __future__ import annotations

import pytest

from src.core import naming
from src.core.naming import NameError_


@pytest.mark.parametrize("name", [
    "TPA3255DDV", "SOP63P810X120-44N", "TI-TPAxxx_AUDIO-AMP",
    "Passives_Inductors_Sagami", "7W15-SAGAMI", "R_0603_1608Metric",
    "tusb564", "C_0.1uF", "V+", "3255",
])
def test_accepts_names_kicad_itself_uses(name):
    assert naming.is_valid(name), name


@pytest.mark.parametrize("name,fragment", [
    ("", "empty"),
    ("Cat:Part", "lib_id"),
    ("a/b", "path separator"),
    ("a\\b", "path separator"),
    ("Inductor_2*10uH", "not allowed"),
    ("Mini-Fit(MX4.2)", "not allowed"),
    ('quote"name', "not allowed"),
    (".hidden", "'.'"),
    ("trailing.", "'.'"),
    ("CON", "reserved"),
    ("nul.step", "reserved"),
    ("x" * 101, "longer than"),
])
def test_rejects_unusable_names(name, fragment):
    with pytest.raises(NameError_, match=fragment):
        naming.validate(name)


def test_the_official_case_only_oddity_is_accepted():
    """tusb564.kicad_sym holds (symbol "TUSB564") -- both spellings are valid."""
    assert naming.is_valid("tusb564") and naming.is_valid("TUSB564")


# --------------------------------------------------------------------------
# Sanitising
# --------------------------------------------------------------------------

def test_sanitize_reproduces_the_name_the_vendor_already_used():
    """
    The staged bundle declares (symbol "Inductor_2*10uH_Leaded_7W15") but its
    own Footprint property says Inductor_2_10uH_Leaded_7W15 -- so collapsing
    illegal characters to '_' lands on the vendor's own spelling.
    """
    assert naming.sanitize("Inductor_2*10uH_Leaded_7W15") == "Inductor_2_10uH_Leaded_7W15"


def test_sanitize_handles_parentheses_and_collapses_runs():
    assert naming.sanitize("Mini-Fit(MX4.2)_HX-5569-2x2A") == "Mini-Fit_MX4.2_HX-5569-2x2A"


@pytest.mark.parametrize("raw", [
    "Inductor_2*10uH_Leaded_7W15", "Mini-Fit(MX4.2)_HX-5569-2x2A",
    "a/b\\c", "  spaced  ", "***", "CON", "...",
])
def test_sanitize_always_produces_a_valid_name(raw):
    out = naming.sanitize(raw)
    assert naming.is_valid(out), f"{raw!r} -> {out!r}"


def test_sanitize_falls_back_when_nothing_survives():
    assert naming.sanitize("///", fallback="unnamed") == "unnamed"


# --------------------------------------------------------------------------
# Case-insensitive duplicates (Windows / macOS fold case)
# --------------------------------------------------------------------------

def test_find_case_collisions_groups_names_differing_only_by_case():
    groups = naming.find_case_collisions(["TPA3255", "tpa3255", "Other"])
    assert groups == [("TPA3255", "tpa3255")]


def test_find_case_collisions_ignores_exact_duplicates():
    assert naming.find_case_collisions(["A", "A", "B"]) == []


def test_collides_case_insensitively_reports_the_existing_name():
    assert naming.collides_case_insensitively("tpa3255", ["TPA3255"]) == "TPA3255"
    assert naming.collides_case_insensitively("TPA3255", ["TPA3255"]) is None


# --------------------------------------------------------------------------
# Official nickname collisions
# --------------------------------------------------------------------------

def test_official_collision_detected_against_an_explicit_pool():
    assert naming.official_collision("MCU_RaspberryPi", {"MCU_RaspberryPi"}) == "MCU_RaspberryPi"
    assert naming.official_collision("mcu_raspberrypi", {"MCU_RaspberryPi"}) == "MCU_RaspberryPi"
    assert naming.official_collision("TI-TPAxxx_AUDIO-AMP", {"MCU_RaspberryPi"}) is None


def test_official_nicknames_never_returns_an_empty_set():
    """Falls back to the bundled list when KiCad is not installed."""
    assert len(naming.official_nicknames()) > 20


def test_check_category_warns_but_does_not_raise_on_an_official_collision():
    warnings = naming.check_category("MCU_RaspberryPi", official={"MCU_RaspberryPi"})
    assert any("official KiCad library" in w for w in warnings)
    assert any("AX_MCU_RaspberryPi" in w for w in warnings)


def test_check_category_warns_on_a_case_clash_with_an_existing_category():
    warnings = naming.check_category("amp_test", existing=["Amp_Test"], official=set())
    assert any("only by case" in w for w in warnings)


def test_check_category_warns_on_an_all_digit_name():
    """'3255' is the stale category this repo actually accumulated."""
    assert any("only digits" in w for w in naming.check_category("3255", official=set()))


def test_check_category_is_silent_for_a_good_name():
    assert naming.check_category("TI-TPAxxx_AUDIO-AMP", existing=["Conn_XT"], official=set()) == []


def test_check_category_still_raises_for_an_invalid_name():
    with pytest.raises(NameError_):
        naming.check_category("Bad:Name", official=set())
