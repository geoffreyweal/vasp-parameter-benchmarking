"""Generation of automatic-mesh KPOINTS files from a grid string.

A grid is written ``n1xn2xn3`` (e.g. ``4x4x4`` or ``6x6x4``). It is turned into
a standard VASP automatic-mesh KPOINTS file::

    Automatic mesh (vasp-parameter-benchmarking)
    0
    Gamma
    4 4 4
    0 0 0

The centring (``Gamma`` or ``Monkhorst-Pack``) is chosen by ``style``. Gamma is
the default - it is the safe choice for hexagonal cells and never worse for
others.
"""

from __future__ import annotations

import re

_GRID_RE = re.compile(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*[xX]\s*(\d+)\s*$")

GAMMA = "gamma"
MONKHORST = "monkhorst"
_STYLE_HEADER = {GAMMA: "Gamma", MONKHORST: "Monkhorst-Pack"}


def parse_grid(grid: str) -> tuple[int, int, int]:
    """Parse ``"4x4x4"`` (or ``"6x6x4"``) into ``(4, 4, 4)``."""
    m = _GRID_RE.match(grid)
    if not m:
        raise ValueError(
            f"invalid KPOINTS grid {grid!r}: expected 'n1xn2xn3' (e.g. '4x4x4')"
        )
    dims = tuple(int(g) for g in m.groups())
    if any(d <= 0 for d in dims):
        raise ValueError(f"invalid KPOINTS grid {grid!r}: dimensions must be positive")
    return dims  # type: ignore[return-value]


def kpoint_count(grid: str) -> int:
    """Total number of mesh points in ``grid`` (n1 * n2 * n3)."""
    n1, n2, n3 = parse_grid(grid)
    return n1 * n2 * n3


def render_kpoints(grid: str, style: str = GAMMA) -> str:
    """Render a KPOINTS file body for ``grid`` with the given centring."""
    if style not in _STYLE_HEADER:
        raise ValueError(f"unknown KPOINTS style {style!r}; use 'gamma' or 'monkhorst'")
    n1, n2, n3 = parse_grid(grid)
    return (
        "Automatic mesh (vasp-parameter-benchmarking)\n"
        "0\n"
        f"{_STYLE_HEADER[style]}\n"
        f"{n1} {n2} {n3}\n"
        "0 0 0\n"
    )
