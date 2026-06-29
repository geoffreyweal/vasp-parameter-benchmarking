"""Parsing of VASP OUTCAR / OSZICAR files for parameter benchmarking.

What this tool compares is *convergence vs cost*, so it pulls out:

  * the final total energy ``energy(sigma->0)`` (the convergence target);
  * the number of ions ``NIONS`` (to report energy per atom);
  * the peak force on any ion in the last ionic step (an optional accuracy check);
  * per-electronic-step wall times from ``LOOP:`` lines (the cost metric).

OSZICAR's final ``E0=`` is used as a fallback energy if the OUTCAR one is absent.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

# "  energy  without entropy=     -23.456  energy(sigma->0) =     -23.460"
_SIGMA0_RE = re.compile(r"energy\(sigma->0\)\s*=\s*(-?\d+\.?\d*(?:[eE][+-]?\d+)?)")

# "   NIONS =       8"
_NIONS_RE = re.compile(r"NIONS\s*=\s*(\d+)")

# "      LOOP:  cpu time     10.7003: real time     10.7910"  (not "LOOP+:")
_LOOP_RE = re.compile(
    r"^\s*LOOP:\s+cpu time\s+([\d.]+)\s*:\s*real time\s+([\d.]+)", re.MULTILINE
)

# OSZICAR step line: "   1 F= -.234E+02 E0= -.234E+02 d E =..."
_E0_RE = re.compile(r"E0=\s*(-?\.?\d+\.?\d*(?:[eE][+-]?\d+)?)")


def final_energy(path: str | Path) -> float | None:
    """Return the last ``energy(sigma->0)`` from an OUTCAR, or None."""
    text = Path(path).read_text(errors="replace")
    matches = _SIGMA0_RE.findall(text)
    return float(matches[-1]) if matches else None


def n_ions(path: str | Path) -> int | None:
    """Return ``NIONS`` from an OUTCAR, or None if absent."""
    text = Path(path).read_text(errors="replace")
    m = _NIONS_RE.search(text)
    return int(m.group(1)) if m else None


def parse_loop_times(path: str | Path) -> list[float]:
    """Return the per-electronic-step ``real time`` values (seconds)."""
    text = Path(path).read_text(errors="replace")
    return [float(m.group(2)) for m in _LOOP_RE.finditer(text)]


def max_force(path: str | Path) -> float | None:
    """Return the largest force magnitude (eV/A) in the last TOTAL-FORCE block.

    Each block lists, per ion, ``x y z fx fy fz``; the per-ion force magnitude is
    ``sqrt(fx^2+fy^2+fz^2)`` and this returns the maximum over ions. Returns None
    if no force block is present.
    """
    text = Path(path).read_text(errors="replace")
    starts = [m.end() for m in re.finditer(r"TOTAL-FORCE", text)]
    if not starts:
        return None

    block = text[starts[-1]:]
    lines = block.splitlines()
    # Skip the "-----" separator that follows the TOTAL-FORCE header.
    forces: list[float] = []
    started = False
    for line in lines:
        if set(line.strip()) <= {"-"} and line.strip():
            if started:
                break  # closing separator
            started = True
            continue
        if not started:
            continue
        parts = line.split()
        if len(parts) != 6:
            break
        try:
            fx, fy, fz = (float(parts[3]), float(parts[4]), float(parts[5]))
        except ValueError:
            break
        forces.append(math.sqrt(fx * fx + fy * fy + fz * fz))
    return max(forces) if forces else None


def oszicar_final_e0(path: str | Path) -> float | None:
    """Return the last ``E0=`` value from an OSZICAR, or None."""
    p = Path(path)
    if not p.is_file():
        return None
    matches = _E0_RE.findall(p.read_text(errors="replace"))
    return float(matches[-1]) if matches else None
