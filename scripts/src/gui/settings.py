"""
Local GUI preferences: window geometry, panel splits, column widths and the
directories the file dialogs last used.

Stored outside the repository (``$XDG_CONFIG_HOME/kicad_customlib/gui.json``,
falling back to ``~/.config``) because none of it should be committed or synced
between machines — a window size from a 4K desktop is actively wrong on a
laptop.

**Failure policy, deliberately the opposite of provenance.json.** A damaged
preferences file must never stop the GUI from opening: it falls back to
defaults and is overwritten on the next save. Preferences are disposable and
reconstructible by using the app; library metadata is neither, which is why
``provenance.load()`` raises loudly on corruption and this does not. Do not
"fix" this asymmetry.

Geometry is keyed per screen configuration, so plugging in a second monitor
yields a separate profile instead of restoring a size that no longer fits.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA_VERSION = 1
APP_DIR_NAME = "kicad_customlib"
FILE_NAME = "gui.json"

# Dialog-size keys used by the windows that remember theirs.
WINDOW_MAIN = "main"
WINDOW_IMPORT = "import"
WINDOW_AUDIT = "audit"
WINDOW_SYNC = "sync"

# Purposes for remembered directories. Separate keys because these live in
# genuinely different places: downloads, project trees, export targets.
DIR_IMPORT_SOURCE = "import_source"
DIR_PACKAGE_PROJECT = "package_project"
DIR_PACKAGE_OUT = "package_out"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / APP_DIR_NAME


def config_path() -> Path:
    return config_dir() / FILE_NAME


def _blank() -> Dict[str, Any]:
    return {"version": SCHEMA_VERSION, "dirs": {}, "geometry": {}}


# Keys the unversioned first release could contain, worth preserving.
_LEGACY_KEYS = ("last_category",)


def _migrate_unversioned(data: Dict[str, Any]) -> Dict[str, Any]:
    """Bring a pre-schema config forward, keeping whatever it did hold."""
    out = _blank()
    for key in _LEGACY_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    return out


class Settings:
    """Preferences, with every read and write tolerant of a broken file."""

    def __init__(self, data: Optional[Dict[str, Any]] = None, path: Optional[Path] = None):
        self.path = path or config_path()
        self.data = data if isinstance(data, dict) else _blank()
        # Normalise shape so callers never have to check.
        self.data.setdefault("version", SCHEMA_VERSION)
        for key in ("dirs", "geometry"):
            if not isinstance(self.data.get(key), dict):
                self.data[key] = {}

    # -- lifecycle --------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        """
        Read the preferences. Never raises.

        A missing, empty, unreadable or malformed file, or one written by a
        newer schema, all yield defaults.
        """
        target = path or config_path()
        try:
            raw = target.read_text(encoding="utf-8-sig")
        except (OSError, ValueError):
            return cls(path=target)
        try:
            data = json.loads(raw)
        except ValueError:
            return cls(path=target)
        if not isinstance(data, dict):
            return cls(path=target)
        if "version" not in data:
            # The first release stored a bare {"last_category": ...} with no
            # version field. Carry the known keys over rather than silently
            # dropping the user's remembered category.
            return cls(_migrate_unversioned(data), path=target)
        if data.get("version") != SCHEMA_VERSION:
            return cls(path=target)
        return cls(data, path=target)

    def save(self) -> bool:
        """
        Write atomically. Never raises; returns whether it succeeded.

        A preference failing to persist is not worth interrupting the user
        over, so the caller is free to ignore the result.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(self.data, indent=2, sort_keys=True) + "\n"
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except (OSError, ValueError, TypeError):
            return False

    # -- plain values -----------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    # -- remembered directories -------------------------------------------
    def dir_for(self, purpose: str, default: Path) -> Path:
        """
        The directory a dialog should open in.

        A remembered path that has since been deleted or moved falls back to
        the default rather than dropping the user at a dead end.
        """
        remembered = self.data.get("dirs", {}).get(purpose)
        if isinstance(remembered, str) and remembered:
            candidate = Path(remembered)
            if candidate.is_dir():
                return candidate
        return default

    def remember_dir(self, purpose: str, path: Path) -> None:
        """Store the directory containing `path` (or `path` itself if it is one)."""
        target = Path(path)
        directory = target if target.is_dir() else target.parent
        self.data.setdefault("dirs", {})[purpose] = str(directory)

    # -- geometry ---------------------------------------------------------
    @staticmethod
    def profile_key(widget: Any) -> str:
        """
        A signature for the current screen configuration, e.g. ``3840x1080@96``.

        Under X11 the reported screen size is normally the bounding box of all
        monitors, so attaching a display changes the key and the old profile is
        left alone. DPI is included so a HiDPI change at the same pixel size
        also gets its own profile.
        """
        try:
            width = int(widget.winfo_screenwidth())
            height = int(widget.winfo_screenheight())
        except Exception:  # noqa: BLE001 -- no display, or a stub in tests
            return "unknown"
        try:
            dpi = int(round(float(widget.winfo_fpixels("1i"))))
        except Exception:  # noqa: BLE001
            dpi = 96
        return f"{width}x{height}@{dpi}"

    def profile(self, key: str, *, create: bool = False) -> Dict[str, Any]:
        """
        The geometry profile for `key`.

        Reading never creates one: otherwise merely opening the app on a new
        screen would grow the file, and reset_layout() would be undone by the
        first read that followed it. Writers pass create=True.
        """
        geometry = self.data.setdefault("geometry", {})
        entry = geometry.get(key)
        if not isinstance(entry, dict):
            if not create:
                return {"windows": {}, "sash": {}, "columns": {}, "browser": {}}
            entry = {}
            geometry[key] = entry
        for section in ("windows", "sash", "columns", "browser"):
            if not isinstance(entry.get(section), dict):
                if not create:
                    entry = dict(entry)
                    entry[section] = {}
                else:
                    entry[section] = {}
        return entry

    def reset_layout(self) -> None:
        """
        Forget every geometry profile, keeping directories and other
        preferences.

        The escape hatch for a saved geometry that has become unusable — for
        instance a window sized for a monitor that is no longer attached.
        """
        self.data["geometry"] = {}

    # -- geometry accessors ------------------------------------------------
    def window_geometry(self, key: str, window: str) -> Optional[str]:
        value = self.profile(key)["windows"].get(window)
        return value if isinstance(value, str) and value else None

    def set_window_geometry(self, key: str, window: str, value: str) -> None:
        self.profile(key, create=True)["windows"][window] = value

    def sash(self, key: str, name: str) -> Optional[int]:
        value = self.profile(key)["sash"].get(name)
        return int(value) if isinstance(value, (int, float)) and value > 0 else None

    def set_sash(self, key: str, name: str, position: int) -> None:
        if position > 0:
            self.profile(key, create=True)["sash"][name] = int(position)

    def columns(self, key: str, kind: str) -> Dict[str, int]:
        raw = self.profile(key)["columns"].get(kind)
        if not isinstance(raw, dict):
            return {}
        return {
            str(name): int(width)
            for name, width in raw.items()
            if isinstance(width, (int, float)) and width > 0
        }

    def set_columns(self, key: str, kind: str, widths: Dict[str, int]) -> None:
        self.profile(key, create=True)["columns"][kind] = {
            str(name): int(width) for name, width in widths.items() if width > 0
        }

    def browser_state(self, key: str) -> Dict[str, Any]:
        return dict(self.profile(key)["browser"])

    def set_browser_state(
        self,
        key: str,
        *,
        kind: Optional[str] = None,
        category: Optional[str] = None,
        sort_column: Optional[str] = None,
        sort_reverse: Optional[bool] = None,
    ) -> None:
        """
        Remember which view the browser was showing.

        The search box is deliberately *not* remembered: a filter silently
        applied at launch looks like a library that has lost most of its parts.
        """
        state = self.profile(key, create=True)["browser"]
        if kind is not None:
            state["kind"] = kind
        # `category` None means "[All categories]", which is a real choice, so
        # it is stored rather than skipped.
        state["category"] = category
        if sort_column is not None:
            state["sort"] = [sort_column, bool(sort_reverse)]


# --------------------------------------------------------------------------
# Geometry helpers (pure, so they are testable without a display)
# --------------------------------------------------------------------------

_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)(?:([+-]\d+)([+-]\d+))?$")


def parse_geometry(value: str) -> Optional[Dict[str, int]]:
    """
    Parse Tk's ``WxH`` or ``WxH+X+Y`` form; offsets may be negative.

    Returns None for anything unparseable, so a hand-edited config cannot
    crash startup.
    """
    if not isinstance(value, str):
        return None
    match = _GEOMETRY_RE.match(value.strip())
    if match is None:
        return None
    width, height, x, y = match.groups()
    if int(width) <= 0 or int(height) <= 0:
        return None
    out = {"width": int(width), "height": int(height)}
    if x is not None and y is not None:
        out["x"] = int(x)
        out["y"] = int(y)
    return out


def clamp_geometry(
    parsed: Dict[str, int],
    screen_width: int,
    screen_height: int,
    *,
    min_width: int = 0,
    min_height: int = 0,
    margin: int = 60,
) -> str:
    """
    Fit a saved geometry to the current screen.

    Size is capped to the screen and floored at the window's minimum. A
    position that would put the title bar off-screen is dropped entirely
    rather than nudged, because a window that opens somewhere surprising is
    worse than one that opens where the window manager chooses.

    Note that restoring a *position* is best-effort regardless: Tk runs under
    XWayland on a Wayland session, which may ignore it, and native Wayland
    always does. Only the size can be relied on.
    """
    width = max(min_width, min(parsed["width"], screen_width))
    height = max(min_height, min(parsed["height"], screen_height))

    if "x" not in parsed or "y" not in parsed:
        return f"{width}x{height}"

    x, y = parsed["x"], parsed["y"]
    off_screen = (
        x + margin > screen_width
        or y + margin > screen_height
        or x + width < margin
        or y < 0
    )
    if off_screen:
        return f"{width}x{height}"
    return f"{width}x{height}+{x}+{y}"


# --------------------------------------------------------------------------
# Per-profile adapter
# --------------------------------------------------------------------------

class LayoutStore:
    """
    A `Settings` object bound to one screen profile.

    Widgets ask for "my column widths" without knowing that profiles exist,
    and every write marks the settings dirty so the owner can save on a
    debounce rather than on every drag event.
    """

    def __init__(
        self,
        settings: Settings,
        profile_key: str,
        *,
        on_dirty: Optional[Any] = None,
    ):
        self.settings = settings
        self.key = profile_key
        self._on_dirty = on_dirty

    def _dirty(self) -> None:
        if self._on_dirty is not None:
            self._on_dirty()

    # -- windows ----------------------------------------------------------
    def window(self, name: str) -> Optional[str]:
        return self.settings.window_geometry(self.key, name)

    def remember_window(self, name: str, geometry: str) -> None:
        if self.settings.window_geometry(self.key, name) != geometry:
            self.settings.set_window_geometry(self.key, name, geometry)
            self._dirty()

    # -- paned window -----------------------------------------------------
    def sash(self, name: str = "browser") -> Optional[int]:
        return self.settings.sash(self.key, name)

    def remember_sash(self, position: int, name: str = "browser") -> None:
        if position and self.settings.sash(self.key, name) != position:
            self.settings.set_sash(self.key, name, position)
            self._dirty()

    # -- columns ----------------------------------------------------------
    def columns(self, kind: str) -> Dict[str, int]:
        return self.settings.columns(self.key, kind)

    def remember_columns(self, kind: str, widths: Dict[str, int]) -> None:
        if widths and self.settings.columns(self.key, kind) != widths:
            self.settings.set_columns(self.key, kind, widths)
            self._dirty()

    # -- browser view -----------------------------------------------------
    def sort(self) -> tuple:
        raw = self.settings.browser_state(self.key).get("sort")
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            return str(raw[0]), bool(raw[1])
        return None, False

    def remember_sort(self, column: str, reverse: bool) -> None:
        if self.sort() != (column, reverse):
            self.settings.set_browser_state(
                self.key, sort_column=column, sort_reverse=reverse
            )
            self._dirty()

    def view(self) -> tuple:
        state = self.settings.browser_state(self.key)
        return state.get("kind") or "symbol", state.get("category")

    def remember_view(self, kind: str, category: Optional[str]) -> None:
        if self.view() != (kind, category):
            self.settings.set_browser_state(self.key, kind=kind, category=category)
            self._dirty()
