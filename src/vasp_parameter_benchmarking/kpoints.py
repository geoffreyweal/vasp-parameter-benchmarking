"""KPOINTS handling for parameter benchmarking.

KPOINTS variations are **user-authored files**, not generated grids: put a
single ``KPOINTS`` in ``VASP_Files/`` to keep it fixed, or ``KPOINTS_1``,
``KPOINTS_2``, ... to sweep over them (any KPOINTS format works - automatic
mesh, line mode, explicit lists - the files are copied verbatim).

When ``setup`` copies ``KPOINTS_<n>`` into a config directory as ``KPOINTS``,
it tags the first line - VASP's free comment line, which VASP ignores - with
the label ``KPOINTS_<n>`` so the report and folder navigator can read back
which variation each folder holds without needing ``VASP_Files/`` around.

``parse_grid``/``kpoint_count``/``read_grid`` are kept for automatic-mesh files
(numeric plotting, and reading trees made by older versions of this tool).
"""

from __future__ import annotations

import re
from pathlib import Path

_GRID_RE = re.compile(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*[xX]\s*(\d+)\s*$")

# A swept-KPOINTS input file: KPOINTS_1, KPOINTS_2, ... (also matched at the
# start of a copied file's comment line to recover the label).
LABEL_RE = re.compile(r"^KPOINTS_(\d+)$")
_COMMENT_LABEL_RE = re.compile(r"^(KPOINTS_\d+)\b")


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

    The file layout is comment / 0 / centring / ``n1 n2 n3`` / shift, so the
    mesh is the fourth line. Returns None if it cannot be parsed (e.g. a
    line-mode KPOINTS). Kept as a fallback for trees made by older versions.
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


def read_label(path: str | Path) -> str | None:
    """Read the ``KPOINTS_<n>`` label from a copied KPOINTS file's comment line.

    Returns None if the file is missing or its first line does not start with a
    label (e.g. a KPOINTS that was not produced by a labelled sweep).
    """
    p = Path(path)
    if not p.is_file():
        return None
    lines = p.read_text(errors="replace").splitlines()
    if not lines:
        return None
    m = _COMMENT_LABEL_RE.match(lines[0].strip())
    return m.group(1) if m else None


def copy_with_label(src: str | Path, dest: str | Path, label: str) -> None:
    """Copy a KPOINTS file, tagging its comment line with ``label``.

    Only the first line - VASP's free comment line - is changed (the original
    comment is kept in parentheses after the label); every other line is copied
    byte-for-byte, so any KPOINTS format survives intact.
    """
    lines = Path(src).read_text(errors="replace").splitlines()
    original = lines[0].strip() if lines else ""
    header = f"{label} ({original})" if original else label
    Path(dest).write_text("\n".join([header] + lines[1:]) + "\n")
