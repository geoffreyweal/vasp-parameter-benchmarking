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
from pathlib import Path

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


def read_grid(path: str | Path) -> str | None:
    """Read the mesh from an automatic-mesh KPOINTS file as ``"n1xn2xn3"``.

    Used by the report to read back the grid each config was run with. The file
    layout is comment / 0 / centring / ``n1 n2 n3`` / shift, so the mesh is the
    fourth line. Returns None if it cannot be parsed (e.g. a line-mode KPOINTS).
    """
    p = Path(path)
    if not p.is_file():
        return None
    lines = p.read_text(errors="replace").splitlines()
    if len(lines) < 4:
        return None
    tokens = lines[3].split()
    try:
        n1, n2, n3 = (int(tokens[0]), int(tokens[1]), int(tokens[2]))
    except (ValueError, IndexError):
        return None
    return f"{n1}x{n2}x{n3}"


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
