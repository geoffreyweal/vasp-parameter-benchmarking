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

A run-level ``mode`` setting may sit at the top of the parameters file::

    mode = oat

    INCAR ENCUT = 300, 400, 500
    KPOINTS      = 2x2x2, 4x4x4

A ``mem_per_cpu from <KEY>`` line requests more SLURM memory for the heavier
configs - one ``--mem-per-cpu`` value per value of the driving parameter, lined
up by position. It is not a sweep axis (it creates no configurations); ``submit``
applies it as ``sbatch --mem-per-cpu=...`` so ``submit.sl`` is never edited. When
several tables apply to a config the greatest value is used::

    INCAR ENCUT = 300, 400, 500
    mem_per_cpu from ENCUT = 2G, 3G, 4G

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

# Run-level settings allowed at the top of a parameters file.
VALID_MODES = ("grid", "oat")
RECOGNISED_SETTINGS = ("mode", "kpoints_style")


@dataclass
class ParamSpec:
    """One swept parameter: a target file, a key, and the values to try."""

    target: str  # INCAR or KPOINTS
    key: str  # the INCAR tag (e.g. "ENCUT"); always "KPOINTS" for the grid
    values: list[str]


@dataclass
class MemSpec:
    """A ``--mem-per-cpu`` lookup keyed positionally to a swept parameter.

    ``values`` lines up 1:1 with the driving spec's ``values``: the i-th memory
    value is used for configs whose ``driver`` parameter takes the i-th value.
    It is not a sweep axis - it never creates configurations, it only tells
    ``submit`` how much memory to request for each one.
    """

    driver: str  # the ParamSpec.key this tracks (e.g. "ENCUT" or "KPOINTS")
    values: list[str]  # one mem-per-cpu value per driver value, by position


# SLURM memory-size suffixes, expressed as a multiple of one megabyte. SLURM
# treats these as binary (1024-based) and a bare number as megabytes (the
# default unit for --mem-per-cpu).
_MEM_UNITS = {"K": 1.0 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 ** 2}


def parse_mem_mb(text: str) -> float:
    """Parse a SLURM memory size (``"2G"``, ``"512M"``, ``"4096"``) into MB.

    Used to compare memory values (so the greatest can be chosen) and to validate
    the table up front; raises ``ValueError`` on anything unparseable.
    """
    s = str(text).strip()
    if not s:
        raise ValueError("empty memory value")
    unit = s[-1].upper()
    if unit in _MEM_UNITS:
        num, factor = s[:-1].strip(), _MEM_UNITS[unit]
    else:
        num, factor = s, 1.0
    try:
        mb = float(num) * factor
    except ValueError:
        raise ValueError(
            f"invalid memory value {text!r}: expected e.g. '2G', '512M' or '4096' (MB)"
        ) from None
    if mb <= 0:
        raise ValueError(f"invalid memory value {text!r}: must be positive")
    return mb


def validate_mem_specs(specs: list[ParamSpec], mem_specs: list[MemSpec]) -> None:
    """Check every MemSpec names a swept parameter and aligns 1:1 with its values."""
    by_key = {s.key: s for s in specs}
    for m in mem_specs:
        drv = by_key.get(m.driver)
        if drv is None:
            raise ValueError(
                f"mem_per_cpu refers to '{m.driver}', which is not a swept parameter"
            )
        if len(m.values) != len(drv.values):
            raise ValueError(
                f"mem_per_cpu from {m.driver} has {len(m.values)} value(s) but "
                f"{m.driver} sweeps {len(drv.values)}; they must line up 1:1"
            )


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


def parse_parameters_file(
    path: str | Path,
) -> tuple[list[ParamSpec], dict[str, str], list[MemSpec]]:
    """Parse a parameters file into ``(specs, settings, mem_specs)``.

    Each non-blank, non-comment line is one of::

        mode = grid | oat              # run-level settings (mode, kpoints_style)
        INCAR <TAG> = v1, v2, ...       # sweep an INCAR tag
        KPOINTS      = g1, g2, ...      # sweep the KPOINTS grid
        mem_per_cpu from <KEY> = m1, m2 # --mem-per-cpu per value of <KEY>

    (``#`` starts a comment, either whole-line or trailing.) Settings are
    returned as a dict; spec lines as a list of :class:`ParamSpec`; any
    ``mem_per_cpu`` lines as a list of :class:`MemSpec` (validated to line up
    with their driving parameter).
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"parameters file not found: {p}")

    specs: list[ParamSpec] = []
    settings: dict[str, str] = {}
    mem_specs: list[MemSpec] = []
    for lineno, raw in enumerate(p.read_text().splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(
                f"{p}:{lineno}: expected '<TARGET> ... = values' or 'setting = value', got {raw!r}"
            )

        lhs, rhs = line.split("=", 1)
        tokens = lhs.split()
        if not tokens:
            raise ValueError(f"{p}:{lineno}: missing name before '='")
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
        elif tokens[0].lower() == "mem_per_cpu":
            if len(tokens) != 3 or tokens[1].lower() != "from":
                raise ValueError(
                    f"{p}:{lineno}: expected 'mem_per_cpu from <KEY> = m1, m2, ...', got {raw!r}"
                )
            values = _split_values(rhs)
            if not values:
                raise ValueError(f"{p}:{lineno}: no memory values after '='")
            for v in values:  # validate every size up front
                try:
                    parse_mem_mb(v)
                except ValueError as exc:
                    raise ValueError(f"{p}:{lineno}: {exc}") from None
            mem_specs.append(MemSpec(tokens[2].upper(), values))
        elif tokens[0].lower() in RECOGNISED_SETTINGS and len(tokens) == 1:
            settings[tokens[0].lower()] = rhs.strip()
        else:
            raise ValueError(
                f"{p}:{lineno}: unknown line {raw!r}; expected INCAR/KPOINTS, "
                f"mem_per_cpu, or a setting ({', '.join(RECOGNISED_SETTINGS)})"
            )

    if "mode" in settings and settings["mode"] not in VALID_MODES:
        raise ValueError(
            f"{p}: invalid mode {settings['mode']!r}; use one of {', '.join(VALID_MODES)}"
        )
    try:
        validate_mem_specs(specs, mem_specs)
    except ValueError as exc:
        raise ValueError(f"{p}: {exc}") from None
    return specs, settings, mem_specs


def render_parameters_file(
    specs: list[ParamSpec],
    mode: str,
    kpoints_style: str,
    mem_specs: list[MemSpec] | None = None,
) -> str:
    """Render the effective sweep back into parameters-file text.

    ``setup`` writes this into the benchmark root so ``report`` (and ``submit``,
    for the memory table) can recover the sweep, mode and baseline without a
    separate JSON manifest.
    """
    lines = [f"mode = {mode}", f"kpoints_style = {kpoints_style}", ""]
    for s in specs:
        prefix = "KPOINTS" if s.target == KPOINTS else f"INCAR {s.key}"
        lines.append(f"{prefix} = {', '.join(s.values)}")
    for m in mem_specs or []:
        lines.append(f"mem_per_cpu from {m.driver} = {', '.join(m.values)}")
    return "\n".join(lines) + "\n"


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
