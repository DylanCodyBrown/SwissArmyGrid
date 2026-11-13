"""The Sandbox's themes: twenty built in (high contrast and colourblind-safe first), and the user's own,
imported as a small JSON file (by the user, or proposed by the agent).

A theme is a set of colour tokens, the same names the page's own themes use (grid.css): the Sandbox sets
them on its own element, so its cards, charts, Page view, PDF and markdown pictures take the theme while
the grid keeps the app's. Every theme here is readable by construction: `accessible()` moves each colour
just far enough from the background to meet its contrast (WCAG: text 7:1, quieter text 4.5:1, chart
colours and strong borders 3:1, the text on the accent 4.5:1), keeping its hue.

A theme file:  {"name": "Harbour", "dark": false, "tokens": {"bg": "#ffffff", "fg": "#14202b",
                "accent": "#0b6e99", "cat": ["#0b6e99", "#c2410c", ...]}}
Missing tokens come from the light or dark base; only plain hex colours are accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# The tokens a Sandbox theme sets (grid.css names, without the leading --).
TOKENS: tuple[str, ...] = (
    "bg",
    "surface",
    "surface-2",
    "raised",
    "fg",
    "muted",
    "border",
    "border-strong",
    "accent",
    "accent-fg",
    "accent-soft",
    "focus",
    "ok",
    "warn",
    "error",
)
# Worked out from the ones above unless a theme gives them: row stripes, the selection, highlights and
# the soft backgrounds behind messages, and the agent's own colour.
DERIVED: tuple[str, ...] = (
    "row-hover",
    "selected",
    "highlight",
    "changed",
    "warn-soft",
    "error-soft",
    "agent",
    "agent-soft",
    "ok-fg",
)
CATEGORIES: int = 8
HEX = re.compile(r"^#(?:[0-9a-fA-F]{6})$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.,'()-]{0,39}$")
MAX_CUSTOM: int = 40


class ThemeError(ValueError):
    """A theme that can't be used (the message says why)."""


# ===== colour arithmetic (sRGB, WCAG 2 relative luminance) =====


def _rgb(color: str) -> tuple[float, float, float]:
    return tuple(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb)


def luminance(color: str) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _mix(color: str, toward: str, share: float) -> str:
    a, b = _rgb(color), _rgb(toward)
    return _hex((a[0] + (b[0] - a[0]) * share, a[1] + (b[1] - a[1]) * share, a[2] + (b[2] - a[2]) * share))


def readable(color: str, background: str, ratio: float) -> str:
    """`color`, moved toward black (on a light background) or white (on a dark one) just far enough to
    stand `ratio` apart from `background`. Its hue stays."""
    if contrast(color, background) >= ratio:
        return color
    target = "#000000" if luminance(background) > 0.18 else "#ffffff"
    low, high = 0.0, 1.0
    for _ in range(30):
        middle = (low + high) / 2
        if contrast(_mix(color, target, middle), background) >= ratio:
            high = middle
        else:
            low = middle
    return _mix(color, target, high)


# ===== a theme =====


@dataclass(frozen=True)
class Theme:
    id: str
    label: str
    dark: bool
    tokens: dict[str, str]
    cat: tuple[str, ...]
    group: str = "Themes"
    note: str = ""
    custom: bool = False
    by_agent: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "dark": self.dark,
            "group": self.group,
            "note": self.note,
            "custom": self.custom,
            "by_agent": self.by_agent,
            "tokens": dict(self.tokens),
            "cat": list(self.cat),
        }

    def file(self) -> dict[str, Any]:
        """The theme as a file to share (and import elsewhere)."""
        return {"name": self.label, "dark": self.dark, "tokens": {**self.tokens, "cat": list(self.cat)}}


LIGHT_BASE: dict[str, str] = {
    "bg": "#ffffff",
    "surface": "#eef1f4",
    "surface-2": "#e1e6eb",
    "raised": "#ffffff",
    "fg": "#1c2228",
    "muted": "#525c66",
    "border": "#c8d0d8",
    "border-strong": "#7d8893",
    "accent": "#c8102e",
    "accent-fg": "#ffffff",
    "accent-soft": "#fbe6e9",
    "focus": "#38434e",
    "ok": "#17722f",
    "warn": "#855800",
    "error": "#a3141f",
}
DARK_BASE: dict[str, str] = {
    "bg": "#121518",
    "surface": "#191d22",
    "surface-2": "#21272d",
    "raised": "#21272d",
    "fg": "#e8ecef",
    "muted": "#a2acb6",
    "border": "#343c45",
    "border-strong": "#707b86",
    "accent": "#ff6b73",
    "accent-fg": "#1c0a0c",
    "accent-soft": "#3d1c21",
    "focus": "#c4ccd4",
    "ok": "#3fb950",
    "warn": "#d29922",
    "error": "#ff8a80",
}


def accessible(id: str, label: str, dark: bool, tokens: dict[str, str], cat: list[str], **more: Any) -> Theme:
    """A theme whose every colour meets its contrast against the background (see the module's note)."""
    t = {**(DARK_BASE if dark else LIGHT_BASE), **tokens}
    bg = t["bg"]
    t["fg"] = readable(t["fg"], bg, 7.0)
    t["muted"] = readable(t["muted"], bg, 4.5)
    t["border-strong"] = readable(t["border-strong"], bg, 3.0)
    t["focus"] = readable(t["focus"], bg, 3.0)
    for name in ("accent", "ok", "warn", "error"):
        t[name] = readable(t[name], bg, 3.0)
    t["accent-fg"] = readable(t["accent-fg"], t["accent"], 4.5)
    given = {k: v for k, v in tokens.items() if k in DERIVED}
    t.setdefault("agent", "#b392f0" if dark else "#6639ba")
    t["agent"] = readable(t["agent"], bg, 3.0)
    derived = {
        "row-hover": _mix(bg, t["fg"], 0.05),
        "selected": _mix(bg, t["accent"], 0.18),
        "highlight": _mix(bg, t["warn"], 0.28),
        "changed": _mix(bg, t["ok"], 0.2),
        "warn-soft": _mix(bg, t["warn"], 0.14),
        "error-soft": _mix(bg, t["error"], 0.14),
        "agent-soft": _mix(bg, t["agent"], 0.16),
        "ok-fg": "#000000" if contrast("#000000", t["ok"]) > contrast("#ffffff", t["ok"]) else "#ffffff",
    }
    t.update({k: given.get(k, v) for k, v in derived.items()})
    # Text on the soft backgrounds and the selection stays as readable as on the background.
    for soft in ("row-hover", "selected", "highlight", "changed", "warn-soft", "error-soft", "agent-soft"):
        if contrast(t["fg"], t[soft]) < 7.0:
            t[soft] = _mix(t[soft], bg, 0.5)
    colors = tuple(readable(c, bg, 3.0) for c in (cat + list(cat))[:CATEGORIES])
    return Theme(id, label, dark, t, colors, **more)


def _theme(
    id: str, label: str, dark: bool, cat: list[str], group: str = "Themes", note: str = "", **tokens: str
) -> Theme:
    return accessible(
        id, label, dark, {k.replace("_", "-"): v for k, v in tokens.items()}, cat, group=group, note=note
    )


# Okabe and Ito's eight colours: told apart by people with each common kind of colour blindness.
OKABE_ITO: list[str] = [
    "#0072b2",
    "#e69f00",
    "#009e73",
    "#cc79a7",
    "#56b4e9",
    "#d55e00",
    "#f0e442",
    "#000000",
]

BUILT_IN: tuple[Theme, ...] = (
    _theme(
        "hc-light",
        "High contrast light",
        False,
        ["#0000c8", "#a8001c", "#005c1a", "#7a00a3", "#8a4b00", "#005f73", "#3d3d3d", "#b0006e"],
        group="Accessible",
        note="Black on white, every line and label at least 7:1.",
        surface="#f4f4f4",
        surface_2="#e8e8e8",
        fg="#000000",
        muted="#2b2b2b",
        border="#5c5c5c",
        border_strong="#000000",
        accent="#a8001c",
        focus="#000000",
    ),
    _theme(
        "hc-dark",
        "High contrast dark",
        True,
        ["#7ec8ff", "#ff9e9e", "#7dff9a", "#f0a6ff", "#ffd479", "#5ef0e6", "#ffffff", "#ffb3d9"],
        group="Accessible",
        note="White on black, every line and label at least 7:1.",
        bg="#000000",
        surface="#0d0d0d",
        surface_2="#1a1a1a",
        raised="#141414",
        fg="#ffffff",
        muted="#e0e0e0",
        border="#a0a0a0",
        border_strong="#ffffff",
        accent="#ffd479",
        accent_fg="#000000",
        accent_soft="#332a10",
        focus="#ffffff",
    ),
    _theme(
        "colorblind-light",
        "Colourblind-safe light",
        False,
        OKABE_ITO,
        group="Accessible",
        note="Okabe and Ito's eight colours, darkened where white needs it.",
        accent="#0072b2",
        accent_soft="#e3f0f8",
    ),
    _theme(
        "colorblind-dark",
        "Colourblind-safe dark",
        True,
        ["#56b4e9", "#e69f00", "#009e73", "#cc79a7", "#0072b2", "#d55e00", "#f0e442", "#ffffff"],
        group="Accessible",
        note="Okabe and Ito's eight colours on a dark ground.",
        accent="#56b4e9",
        accent_fg="#06121a",
        accent_soft="#13293a",
    ),
    _theme(
        "light",
        "Steel light",
        False,
        ["#c8102e", "#3b6e96", "#17722f", "#a3248f", "#6a5c00", "#0a7c86", "#5b4bb7", "#57606a"],
        note="The app's own light theme.",
    ),
    _theme(
        "dark",
        "Steel dark",
        True,
        ["#ff6b73", "#7fb2dc", "#56d364", "#f778ba", "#e3b341", "#39c5cf", "#a99cf0", "#b1bac4"],
        note="The app's own dark theme.",
    ),
    _theme(
        "paper",
        "Paper",
        False,
        ["#1f3a5f", "#9c4221", "#2f6b3a", "#6b3f7a", "#8a6d1f", "#2c6e6e", "#7a2e3a", "#4a4a4a"],
        note="Warm paper and ink: made to print.",
        bg="#fbf8f1",
        surface="#f3eee2",
        surface_2="#e9e2d0",
        raised="#fffdf8",
        fg="#22201c",
        muted="#5e584c",
        border="#d8cfbb",
        accent="#9c4221",
        accent_soft="#f5e3d8",
    ),
    _theme(
        "ink",
        "Ink",
        False,
        ["#1d3557", "#e63946", "#2a9d8f", "#6d597a", "#e76f51", "#457b9d", "#8d6e00", "#264653"],
        note="Navy on white, one red.",
        fg="#14213d",
        accent="#1d3557",
        accent_soft="#e3e8f0",
    ),
    _theme(
        "arctic",
        "Arctic",
        False,
        ["#0b6e99", "#5b8fb9", "#2f8f83", "#7b6cc4", "#b5651d", "#3d5a80", "#9a4d7a", "#5c6b73"],
        note="Cool blues and greys.",
        bg="#f7fafc",
        surface="#edf3f7",
        surface_2="#dfe9f0",
        fg="#102a3a",
        accent="#0b6e99",
        accent_soft="#dcedf5",
    ),
    _theme(
        "sand",
        "Sand",
        False,
        ["#8c5a2b", "#2f6f73", "#a8432e", "#5b6f2b", "#6e4f8c", "#b07d1c", "#3f5b7a", "#7a5c4f"],
        note="Warm, low glare.",
        bg="#fbf6ec",
        surface="#f2eadb",
        surface_2="#e7dcc6",
        fg="#2e2416",
        accent="#8c5a2b",
        accent_soft="#f1e2cf",
    ),
    _theme(
        "forest-light",
        "Meadow",
        False,
        ["#2d6a4f", "#9c6644", "#1b4965", "#bc4749", "#6a994e", "#7b2cbf", "#a68a00", "#495057"],
        note="Greens with an earthy second colour.",
        bg="#f8fbf7",
        surface="#edf4ec",
        surface_2="#dfebdd",
        fg="#16261c",
        accent="#2d6a4f",
        accent_soft="#dcefe3",
    ),
    _theme(
        "rose",
        "Rose",
        False,
        ["#b5179e", "#3a0ca3", "#f72585", "#4361ee", "#7209b7", "#e85d04", "#4895ef", "#6c757d"],
        note="Bright pinks and violets.",
        bg="#fffafc",
        surface="#fbeef4",
        surface_2="#f5dfe9",
        fg="#2b1020",
        accent="#b5179e",
        accent_soft="#fadcef",
    ),
    _theme(
        "mint",
        "Mint",
        False,
        ["#0f766e", "#7c3aed", "#c2410c", "#0369a1", "#a16207", "#be123c", "#4d7c0f", "#475569"],
        note="Fresh teal with clear second colours.",
        bg="#f6fdfb",
        surface="#e9f8f4",
        surface_2="#d6f0e9",
        fg="#0f2a26",
        accent="#0f766e",
        accent_soft="#d3f2ec",
    ),
    _theme(
        "sunrise",
        "Sunrise",
        False,
        ["#c2410c", "#7c2d12", "#b45309", "#9d174d", "#1e40af", "#065f46", "#6d28d9", "#57534e"],
        note="Oranges and reds, a blue to tell apart.",
        bg="#fffaf5",
        surface="#fdf0e4",
        surface_2="#f9e1cc",
        fg="#2b1608",
        accent="#c2410c",
        accent_soft="#fde3d0",
    ),
    _theme(
        "graphite",
        "Graphite",
        True,
        ["#9ecbff", "#ffb86b", "#7ee0a1", "#ff8fb1", "#d6b4ff", "#ffe08a", "#7fdbda", "#c9ced6"],
        note="Mid grey, easy on the eyes.",
        bg="#2b2f33",
        surface="#33383d",
        surface_2="#3c4248",
        raised="#363b41",
        fg="#eef1f4",
        muted="#c0c7ce",
        border="#4c535a",
        accent="#9ecbff",
        accent_fg="#0b1a2a",
        accent_soft="#33465c",
    ),
    _theme(
        "midnight",
        "Midnight",
        True,
        ["#82aaff", "#f78c6c", "#c3e88d", "#c792ea", "#ffcb6b", "#89ddff", "#ff5370", "#b0bec5"],
        note="Deep navy.",
        bg="#0f1729",
        surface="#151f36",
        surface_2="#1c2843",
        raised="#18233d",
        fg="#e6ecff",
        muted="#a9b5d6",
        border="#2a3a5e",
        accent="#82aaff",
        accent_fg="#0a1020",
        accent_soft="#1f2f55",
    ),
    _theme(
        "forest-dark",
        "Forest",
        True,
        ["#95d5b2", "#f4a261", "#a2d2ff", "#ffafcc", "#e9c46a", "#cdb4db", "#80ed99", "#ced4da"],
        note="Dark green, soft colours.",
        bg="#10201a",
        surface="#152a22",
        surface_2="#1c362b",
        raised="#183026",
        fg="#e6f4ec",
        muted="#a7c4b5",
        border="#2b4a3c",
        accent="#95d5b2",
        accent_fg="#0b1a13",
        accent_soft="#1f3d30",
    ),
    _theme(
        "slate",
        "Slate",
        True,
        ["#7dd3fc", "#fca5a5", "#86efac", "#f0abfc", "#fcd34d", "#a5b4fc", "#5eead4", "#cbd5e1"],
        note="Blue-grey, quiet.",
        bg="#1e293b",
        surface="#243247",
        surface_2="#2c3b52",
        raised="#273549",
        fg="#f1f5f9",
        muted="#b8c4d3",
        border="#3b4b63",
        accent="#7dd3fc",
        accent_fg="#082030",
        accent_soft="#1f3a52",
    ),
    _theme(
        "plum",
        "Plum",
        True,
        ["#f0abfc", "#fdba74", "#a5f3fc", "#fda4af", "#bef264", "#c4b5fd", "#fde047", "#d4d4d8"],
        note="Dark purple, warm highlights.",
        bg="#1f1029",
        surface="#281535",
        surface_2="#321b42",
        raised="#2c183b",
        fg="#f6edfb",
        muted="#cdb9da",
        border="#46295a",
        accent="#f0abfc",
        accent_fg="#1f0a29",
        accent_soft="#3d1f4f",
    ),
    _theme(
        "carbon",
        "Carbon",
        True,
        ["#4fc3f7", "#ffb74d", "#81c784", "#f06292", "#ba68c8", "#fff176", "#4db6ac", "#bdbdbd"],
        note="True black, for dark rooms and OLED screens.",
        bg="#000000",
        surface="#0a0a0a",
        surface_2="#141414",
        raised="#0f0f0f",
        fg="#f5f5f5",
        muted="#bdbdbd",
        border="#2e2e2e",
        accent="#4fc3f7",
        accent_fg="#001a26",
        accent_soft="#0b2733",
    ),
)
BY_ID: dict[str, Theme] = {t.id: t for t in BUILT_IN}
APP: str = "app"  # follow the app's own theme (the default)


def _slug(name: str) -> str:
    return "custom-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def read_theme(raw: Any, by_agent: bool = False) -> Theme:
    """A theme from its file (see the module's note), checked and made readable."""
    if not isinstance(raw, dict):
        raise ThemeError('A theme is {"name": ..., "dark": true or false, "tokens": {"bg": "#ffffff", ...}}.')
    name = raw.get("name")
    if not isinstance(name, str) or not NAME.match(name.strip()):
        raise ThemeError("A theme's name is 1 to 40 letters, digits, spaces and simple punctuation.")
    dark = raw.get("dark", False)
    if not isinstance(dark, bool):
        raise ThemeError("dark is true or false.")
    tokens = raw.get("tokens") or {}
    if not isinstance(tokens, dict):
        raise ThemeError("tokens is an object of colour names to #rrggbb colours.")
    cat = tokens.get("cat", [])
    picked: dict[str, str] = {}
    for key, value in tokens.items():
        if key == "cat":
            continue
        if key not in TOKENS and key not in DERIVED:
            raise ThemeError(
                f"{key!r} is not a theme colour. They are: {', '.join((*TOKENS, *DERIVED))} and cat."
            )
        if not isinstance(value, str) or not HEX.match(value):
            raise ThemeError(f"{key} is a #rrggbb colour, not {value!r}.")
        picked[key] = value.lower()
    if (
        not isinstance(cat, list)
        or not all(isinstance(c, str) and HEX.match(c) for c in cat)
        or len(cat) > CATEGORIES
    ):
        raise ThemeError(f"cat is up to {CATEGORIES} #rrggbb colours, the chart series in order.")
    base = BY_ID["dark" if dark else "light"]
    colors = [c.lower() for c in cat] or list(base.cat)
    return accessible(
        _slug(name),
        name.strip(),
        dark,
        picked,
        colors,
        group="Yours",
        custom=True,
        by_agent=by_agent,
        note="Proposed by the agent." if by_agent else "Imported.",
    )
