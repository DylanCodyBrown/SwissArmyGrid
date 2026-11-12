"""Every theme's token pairs meet contrast targets (plan section 13, M8 acceptance).

Reads the four theme blocks from web/static/grid.css and checks WCAG 2 contrast ratios:
text on its backgrounds at least 4.5:1 (7:1 in the high-contrast themes), interface parts such
as borders, focus rings and meter fills at least 3:1. A new token or a tweaked color that
breaks a pair fails here before anyone sees it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "web" / "static" / "grid.css").read_text(encoding="utf-8")
THEMES = ("light", "dark", "hc-light", "hc-dark")

# (foreground token, background token, minimum normal, minimum high contrast)
TEXT_PAIRS = [
    ("fg", "bg"),
    ("fg", "surface"),
    ("fg", "surface-2"),
    ("fg", "raised"),
    ("muted", "bg"),
    ("muted", "surface"),
    ("muted", "raised"),
    ("accent", "bg"),
    ("accent", "surface"),
    ("accent-fg", "accent"),
    ("fg", "accent-soft"),
    ("fg", "selected"),
    ("fg", "highlight"),
    ("fg", "changed"),
    ("fg", "row-hover"),
    ("error", "bg"),
    ("error", "error-soft"),
    ("warn", "bg"),
    ("warn", "warn-soft"),
    ("ok", "bg"),
    ("ok", "surface"),
    ("ok-fg", "ok"),
    ("agent", "bg"),
    ("agent", "agent-soft"),
]
UI_PAIRS = [("border-strong", "bg"), ("focus", "bg"), ("focus", "surface"), ("accent", "accent-soft")]
CATEGORY_PAIRS = [(f"cat-{i}", "bg") for i in range(1, 9)]


def theme_tokens(theme: str) -> dict[str, str]:
    selector = re.escape(f'html[data-theme="{theme}"]')
    block = re.search(selector + r"\s*\{(.*?)\n\}", CSS, re.S)
    assert block, f"no block for {theme}"
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block.group(1)))


def luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def ratio(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("theme", THEMES)
def test_text_contrast(theme: str) -> None:
    tokens = theme_tokens(theme)
    minimum = 7.0 if theme.startswith("hc") else 4.5
    failures = [
        f"{fg} on {bg}: {ratio(tokens[fg], tokens[bg]):.2f}"
        for fg, bg in TEXT_PAIRS
        if ratio(tokens[fg], tokens[bg]) < minimum
    ]
    assert not failures, f"{theme} text below {minimum}:1 -> " + "; ".join(failures)


@pytest.mark.parametrize("theme", THEMES)
def test_interface_and_chart_contrast(theme: str) -> None:
    tokens = theme_tokens(theme)
    failures = [
        f"{fg} on {bg}: {ratio(tokens[fg], tokens[bg]):.2f}"
        for fg, bg in UI_PAIRS + CATEGORY_PAIRS
        if ratio(tokens[fg], tokens[bg]) < 3.0
    ]
    assert not failures, f"{theme} interface below 3:1 -> " + "; ".join(failures)


def test_every_theme_defines_the_same_tokens() -> None:
    names = [set(theme_tokens(t)) for t in THEMES]
    assert all(n == names[0] for n in names), "themes define different token sets"
