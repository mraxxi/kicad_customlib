"""
Validation and sanitising of category, symbol, footprint and model names.

The allowed character set is a deliberate superset of what the official KiCad
libraries use (alphanumerics plus '+', '-', '.', '_' -- see AGENTS.md 6.1), so
no legitimate KiCad name is ever rejected, while names that break lib_ids,
Windows file systems or KiCad's own parser are.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

ALLOWED_EXTRA = "+-._"
_ALLOWED_RE = re.compile(r"^[A-Za-z0-9+\-._]+$")

MAX_NAME_LEN = 100

# Reserved device names on Windows, with or without an extension.
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Fallback list for machines without KiCad installed. Not exhaustive -- it only
# needs to catch the nicknames people actually reach for. The real check reads
# the installed libraries.
_COMMON_OFFICIAL = {
    "4xxx", "74xx", "Amplifier_Audio", "Amplifier_Operational", "Analog",
    "Analog_ADC", "Analog_DAC", "Audio", "Battery_Management", "Capacitor",
    "Capacitor_SMD", "Capacitor_THT", "Connector", "Connector_Audio",
    "Connector_Generic", "Device", "Diode", "Diode_Bridge", "Diode_SMD",
    "Diode_THT", "Driver_FET", "Driver_Motor", "Fuse", "Inductor_SMD",
    "Interface_USB", "Jumper", "LED", "LED_SMD", "LED_THT", "Logic_74xx",
    "MCU_Microchip_ATmega", "MCU_Module", "MCU_NXP_LPC", "MCU_RaspberryPi",
    "MCU_ST_STM32F0", "MCU_ST_STM32F1", "MCU_ST_STM32F4", "Mechanical",
    "Motor", "Package_DIP", "Package_QFP", "Package_SO", "Package_TO_SOT_SMD",
    "Package_TO_SOT_THT", "Potentiometer", "Power_Management",
    "Power_Protection", "Power_Supervisor", "Regulator_Linear",
    "Regulator_Switching", "Relay", "Resistor_SMD", "Resistor_THT", "RF",
    "RF_Module", "Sensor", "Sensor_Motion", "Sensor_Temperature", "Switch",
    "Transistor_BJT", "Transistor_FET", "Transformer", "Triac_Thyristor",
    "Valve", "Video",
}


class NameError_(ValueError):
    """A name that cannot be used at all."""


def _reason_invalid(name: str, kind: str) -> Optional[str]:
    if not name:
        return f"{kind} name is empty"
    if len(name) > MAX_NAME_LEN:
        return f"{kind} name is longer than {MAX_NAME_LEN} characters"
    if ":" in name:
        return (
            f"{kind} name contains ':', which separates library from item in a "
            f"KiCad lib_id"
        )
    if "/" in name or "\\" in name:
        return f"{kind} name contains a path separator"
    if not _ALLOWED_RE.match(name):
        bad = sorted({c for c in name if not (c.isalnum() or c in ALLOWED_EXTRA)})
        shown = ", ".join(repr(c) for c in bad)
        return (
            f"{kind} name contains characters that are not allowed ({shown}); "
            f"allowed: letters, digits and {' '.join(ALLOWED_EXTRA)}"
        )
    if name != name.strip():
        return f"{kind} name has leading or trailing whitespace"
    if name.startswith(".") or name.endswith("."):
        return f"{kind} name starts or ends with '.'"
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        return f"{kind} name '{name}' is a reserved device name on Windows"
    return None


def validate(name: str, kind: str = "item") -> None:
    """Raise NameError_ when `name` cannot be used. Returns None on success."""
    reason = _reason_invalid(name, kind)
    if reason:
        raise NameError_(reason)


def is_valid(name: str, kind: str = "item") -> bool:
    return _reason_invalid(name, kind) is None


def sanitize(name: str, *, fallback: str = "unnamed") -> str:
    """
    Best-effort conversion of an arbitrary vendor name into a usable one.

    Illegal characters collapse to '_', so 'Inductor_2*10uH_Leaded_7W15'
    becomes 'Inductor_2_10uH_Leaded_7W15' -- which happens to be exactly the
    name the vendor's own Footprint property already used.
    """
    out = "".join(c if (c.isalnum() or c in ALLOWED_EXTRA) else "_" for c in name)
    out = re.sub(r"_{2,}", "_", out).strip("._ ")
    if not out:
        return fallback
    if out.split(".")[0].upper() in _WINDOWS_RESERVED:
        out = f"{out}_"
    return out[:MAX_NAME_LEN]


# --------------------------------------------------------------------------
# Duplicate detection (case-insensitive: Windows and macOS fold case)
# --------------------------------------------------------------------------

def find_case_collisions(names: Iterable[str]) -> List[Tuple[str, ...]]:
    """Groups of names that differ only by case, as sorted tuples."""
    buckets: dict[str, List[str]] = {}
    for n in names:
        buckets.setdefault(n.lower(), []).append(n)
    return [tuple(sorted(v)) for v in buckets.values() if len(set(v)) > 1]


def collides_case_insensitively(name: str, existing: Iterable[str]) -> Optional[str]:
    """The existing name that `name` would collide with on Windows/macOS."""
    low = name.lower()
    for e in existing:
        if e.lower() == low and e != name:
            return e
    return None


# --------------------------------------------------------------------------
# Official KiCad nickname collisions
# --------------------------------------------------------------------------

def _dirs_from_env(var: str, default: str) -> Optional[Path]:
    p = Path(os.environ.get(var) or default)
    return p if p.is_dir() else None


def official_nicknames() -> Set[str]:
    """
    Nicknames of the installed official libraries, read from
    $KICAD10_SYMBOL_DIR / $KICAD10_FOOTPRINT_DIR when set, else from the
    standard system paths. Falls back to a bundled list when KiCad is absent.
    """
    found: Set[str] = set()

    sym_dir = _dirs_from_env("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols")
    if sym_dir:
        for p in sym_dir.iterdir():
            if p.is_dir() and p.name.endswith(".kicad_symdir"):
                found.add(p.name[: -len(".kicad_symdir")])
            elif p.is_file() and p.suffix == ".kicad_sym":
                found.add(p.stem)

    fp_dir = _dirs_from_env("KICAD10_FOOTPRINT_DIR", "/usr/share/kicad/footprints")
    if fp_dir:
        for p in fp_dir.iterdir():
            if p.is_dir() and p.name.endswith(".pretty"):
                found.add(p.name[: -len(".pretty")])

    return found or set(_COMMON_OFFICIAL)


def official_collision(name: str, official: Optional[Set[str]] = None) -> Optional[str]:
    """
    The official nickname `name` collides with, compared case-insensitively.
    A collision is a warning, not an error: it makes lib_id resolution depend
    on global-table ordering, which differs between machines.
    """
    pool = official if official is not None else official_nicknames()
    low = name.lower()
    for n in pool:
        if n.lower() == low:
            return n
    return None


def suggest_prefixed(name: str, prefix: str = "AX") -> str:
    """A non-colliding alternative for a category that shadows an official one."""
    return f"{prefix}_{name}"


def check_category(
    name: str,
    *,
    existing: Sequence[str] = (),
    official: Optional[Set[str]] = None,
) -> List[str]:
    """
    Validate a category name. Raises NameError_ when unusable; otherwise
    returns a list of human-readable warnings (possibly empty).
    """
    validate(name, "category")
    warnings: List[str] = []

    clash = collides_case_insensitively(name, existing)
    if clash:
        warnings.append(
            f"category '{name}' differs from existing '{clash}' only by case; "
            f"on Windows and macOS these are the same directory"
        )

    off = official_collision(name, official)
    if off:
        warnings.append(
            f"category '{name}' collides with the official KiCad library "
            f"'{off}'; lib_id resolution would depend on global table order. "
            f"Consider '{suggest_prefixed(name)}'"
        )

    if name.isdigit():
        warnings.append(
            f"category '{name}' is only digits, which makes a poor library "
            f"nickname and reads as a number in some UI toolkits"
        )

    return warnings
