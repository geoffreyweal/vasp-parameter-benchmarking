"""Parsing of parameter-sweep specifications and expansion into configurations.

A *parameter spec* says "vary this INCAR tag (or the KPOINTS grid) over these
values". Specs come from two places, which are merged (CLI wins on a clash):

  * CLI flags - ``--incar "ENCUT=300,400,500"`` (repeatable) and
    ``--kpoints "2x2x2,4x4x4,6x6x6"``;
  * a parameters file (``--parameters``), one spec per line::

        # vasp_parameter_benchmarking_parameters.txt
        INCAR ENCUT = 300, 400, 500, 600, 700
        INCAR SIGMA = 0.05, 0.1, 0.2
        KPOINTS      = 2x2x2, 4x4x4, 6x6x6, 8x8x8

The specs are then expanded into *configurations* - one per directory - in one
of two modes:

  * ``grid`` - the full Cartesian product of every spec's values;
  * ``oat``  - one-at-a-time: a baseline (the first value of every spec) plus,
    for each spec, every other value with the rest held at baseline.

For convergence studies put the value you trust most (highest ENCUT, densest
KPOINTS) *first* in each list: it becomes the baseline that the other specs are
held at, and the per-parameter reference in the report.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .kpoints import kpoint_count, parse_grid

# Targets a spec can edit.
INCAR = "INCAR"
KPOINTS = "KPOINTS"


@dataclass
class ParamSpec:
    """One swept parameter: a target file, a key, and the values to try."""

    target: str  # INCAR or KPOINTS
    key: str  # the INCAR tag (e.g. "ENCUT"); always "KPOINTS" for the grid
    values: list[str]


def _split_values(raw: str) -> list[str]:
    """Split a comma-separated value list, trimming blanks, keeping order."""
    return [v.strip() for v in raw.split(",") if v.strip()]


def parse_cli_incar(spec: str) -> ParamSpec:
    """Parse a ``--incar`` flag value like ``"ENCUT=300,400,500"``."""
    if "=" not in spec:
        raise ValueError(
            f"invalid --incar {spec!r}: expected 'TAG=v1,v2,...' (e.g. 'ENCUT=300,400,500')"
        )
    key, rest = spec.split("=", 1)
    key = key.strip().upper()
    if not key:
        raise ValueError(f"invalid --incar {spec!r}: missing tag name before '='")
    values = _split_values(rest)
    if not values:
        raise ValueError(f"invalid --incar {spec!r}: no values after '='")
    return ParamSpec(INCAR, key, values)


def parse_cli_kpoints(spec: str) -> ParamSpec:
    """Parse a ``--kpoints`` flag value like ``"2x2x2,4x4x4,6x6x6"``."""
    values = _split_values(spec)
    if not values:
        raise ValueError(f"invalid --kpoints {spec!r}: no grids given")
    for v in values:  # validate every grid up front
        parse_grid(v)
    return ParamSpec(KPOINTS, KPOINTS, values)


def parse_parameters_file(path: str | Path) -> list[ParamSpec]:
    """Parse a parameters file into specs.

    Each non-blank, non-comment line is one of::

        INCAR <TAG> = v1, v2, ...
        KPOINTS      = g1, g2, ...

    (``#`` starts a comment, either whole-line or trailing.)
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"parameters file not found: {p}")

    specs: list[ParamSpec] = []
    for lineno, raw in enumerate(p.read_text().splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"{p}:{lineno}: expected '<TARGET> ... = values', got {raw!r}")

        lhs, rhs = line.split("=", 1)
        tokens = lhs.split()
        if not tokens:
            raise ValueError(f"{p}:{lineno}: missing target before '='")
        target = tokens[0].upper()

        if target == KPOINTS:
            if len(tokens) != 1:
                raise ValueError(f"{p}:{lineno}: KPOINTS takes no tag name, got {raw!r}")
            specs.append(parse_cli_kpoints(rhs))
        elif target == INCAR:
            if len(tokens) != 2:
                raise ValueError(
                    f"{p}:{lineno}: expected 'INCAR <TAG> = ...', got {raw!r}"
                )
            specs.append(parse_cli_incar(f"{tokens[1]}={rhs}"))
        else:
            raise ValueError(
                f"{p}:{lineno}: unknown target {target!r}; use INCAR or KPOINTS"
            )
    return specs


def merge_specs(file_specs: list[ParamSpec], cli_specs: list[ParamSpec]) -> list[ParamSpec]:
    """Merge file + CLI specs, de-duplicating by (target, key); CLI wins.

    Order is preserved: file specs first (in file order), then any CLI specs
    that introduce a new key. A CLI spec with the same (target, key) as a file
    spec replaces that spec's values in place.
    """
    merged: list[ParamSpec] = [ParamSpec(s.target, s.key, list(s.values)) for s in file_specs]
    index = {(s.target, s.key): i for i, s in enumerate(merged)}
    for s in cli_specs:
        ident = (s.target, s.key)
        if ident in index:
            merged[index[ident]].values = list(s.values)
        else:
            index[ident] = len(merged)
            merged.append(ParamSpec(s.target, s.key, list(s.values)))
    if len({(s.target, s.key) for s in merged}) != len(merged):  # pragma: no cover
        raise ValueError("duplicate parameter keys after merge")
    return merged


def baseline_assignment(specs: list[ParamSpec]) -> dict[str, str]:
    """The baseline value of every spec (its first listed value)."""
    return {s.key: s.values[0] for s in specs}


def build_configs(specs: list[ParamSpec], mode: str) -> list[dict[str, str]]:
    """Expand specs into a list of assignments ``{key: value}``.

    ``mode`` is ``"grid"`` (Cartesian product) or ``"oat"`` (one-at-a-time).
    The returned assignments are de-duplicated while preserving order.
    """
    if not specs:
        raise ValueError("no parameters to sweep; pass --incar/--kpoints or --parameters")

    if mode == "grid":
        assignments = [dict()]
        for s in specs:
            assignments = [{**a, s.key: v} for a in assignments for v in s.values]
    elif mode == "oat":
        base = baseline_assignment(specs)
        assignments = [dict(base)]
        for s in specs:
            for v in s.values[1:]:
                assignments.append({**base, s.key: v})
    else:
        raise ValueError(f"unknown mode {mode!r}; use 'grid' or 'oat'")

    # De-duplicate (oat baseline can coincide; grid never repeats) on identity.
    seen: set[tuple] = set()
    unique: list[dict[str, str]] = []
    for a in assignments:
        ident = tuple(a[s.key] for s in specs)
        if ident not in seen:
            seen.add(ident)
            unique.append(a)
    return unique


def _sanitize(value: str) -> str:
    """Make a parameter value safe for a directory-name token."""
    return value.strip().replace(" ", "").replace("/", "-").replace(":", "-")


def config_name(specs: list[ParamSpec], assignment: dict[str, str]) -> str:
    """Build the directory name for an assignment, e.g. ``ENCUT-400_KPOINTS-4x4x4``."""
    return "_".join(f"{s.key}-{_sanitize(assignment[s.key])}" for s in specs)


def numeric_value(spec: ParamSpec, value: str) -> float | None:
    """A numeric x-coordinate for plotting, or None if the value isn't numeric.

    KPOINTS grids map to their total k-point count (n1 x n2 x n3); INCAR tags
    map to ``float(value)`` when possible.
    """
    if spec.target == KPOINTS:
        return float(kpoint_count(value))
    try:
        return float(value)
    except ValueError:
        return None


def config_record(specs: list[ParamSpec], assignment: dict[str, str], mode: str) -> dict:
    """The JSON record written into each config dir (read back by the report)."""
    parameters = {}
    for s in specs:
        value = assignment[s.key]
        parameters[s.key] = {
            "target": s.target,
            "value": value,
            "numeric": numeric_value(s, value),
        }
    return {
        "name": config_name(specs, assignment),
        "mode": mode,
        "parameters": parameters,
    }
