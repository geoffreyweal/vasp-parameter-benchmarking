"""Part 1: generate the VASP parameter-benchmarking directory tree.

Each parameter combination gets a plain **numbered** directory (``001``, ``002``,
...). The number is just a label; the ``INCAR``/``KPOINTS`` inside *are* the
definition of that system, and the report (and folder navigator) read the values
back out of them - there is no per-config manifest.

``setup`` is **additive and idempotent**: it works out the combinations the
current sweep needs, then creates only the ones that do not already exist (matched
by reading existing folders' INCAR/KPOINTS). Re-running after adding a parameter
therefore reuses every completed run and only appends the genuinely new folders -
existing folders are never renamed, touched or re-run.

Each new directory gets the VASP inputs *and the submit.sl* copied in; only the
swept parameters are edited (INCAR tags set, KPOINTS grid written). The effective
sweep (mode + parameters) is written to
``<root>/vasp_parameter_benchmarking_parameters.txt`` so the report knows which
tags were swept, in what order, and what the baseline is; a ``folder_index.html``
navigator is (re)written so you can look a combination up by folder number.

The submit.sl is copied verbatim except for at most two ``#SBATCH`` directives
(inserted if absent): ``--job-name``, set by default to ``vasp-para-bench-<folder>``
so each job is identifiable in ``squeue`` (pass ``--no-name-jobs`` to keep your
own), and ``--mem-per-cpu`` when the parameters file carries a ``mem_per_cpu``
table (so heavier configs request more memory). Everything else - the parallel
layout and all other SLURM directives - is exactly what you provide.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import incar as incar_mod
from . import index as index_mod
from . import kpoints as kpoints_mod
from .parameters import (
    INCAR,
    KPOINTS,
    VALID_MODES,
    MemSpec,
    ParamSpec,
    build_configs,
    merge_specs,
    parse_cli_incar,
    parse_cli_kpoints,
    parse_mem_mb,
    parse_parameters_file,
    render_parameters_file,
    validate_mem_specs,
)

def _directive_re(long: str, short: str | None) -> "re.Pattern[str]":
    """Match a ``#SBATCH`` line that already sets ``--<long>`` (or ``-<short>``).

    Matches both ``--name=value`` and ``--name value`` forms (and ``-X value`` for
    the short flag); ignores commented ``##SBATCH`` lines and unrelated flags that
    merely share a prefix (e.g. ``--mem`` vs ``--mem-per-cpu``).
    """
    forms = [rf"--{re.escape(long)}(?:=|\s+)"]
    if short:
        forms.append(rf"-{re.escape(short)}\s+")
    return re.compile(rf"^\s*#SBATCH\s+(?:{'|'.join(forms)})\S")


def set_sbatch_directive(
    submit_path: Path, long: str, value: str, short: str | None = None
) -> None:
    """Set ``#SBATCH --<long>=<value>`` in a submit script, in place.

    Replaces an existing directive (keeping its position) or, if there is none,
    inserts one after the last ``#SBATCH`` line (or after the shebang). Only this
    one directive is ever touched - the rest of the script is left verbatim.
    """
    rx = _directive_re(long, short)
    lines = submit_path.read_text().splitlines()
    new_line = f"#SBATCH --{long}={value}"

    for i, line in enumerate(lines):
        if rx.match(line):
            lines[i] = new_line
            submit_path.write_text("\n".join(lines) + "\n")
            return

    insert_at = next(
        (i + 1 for i in reversed(range(len(lines))) if lines[i].lstrip().startswith("#SBATCH")),
        1 if lines and lines[0].startswith("#!") else 0,
    )
    lines.insert(insert_at, new_line)
    submit_path.write_text("\n".join(lines) + "\n")

# Width of the zero-padded folder numbers (001, 002, ...).
NUMBER_WIDTH = 3

# VASP inputs that must be present in the inputs directory. KPOINTS is required
# only when the KPOINTS grid is being swept (otherwise it is copied if present).
REQUIRED_INPUTS = ["INCAR", "POSCAR", "POTCAR"]

PARAMETERS_FILENAME = "vasp_parameter_benchmarking_parameters.txt"
SUBMIT_NAME = "submit.sl"

# SLURM job names are set (by default) to this prefix + the folder number,
# e.g. "vasp-para-bench-001", so jobs are identifiable in squeue/sacct.
JOB_NAME_PREFIX = "vasp-para-bench-"


def resolve_specs(
    incar_flags: list[str] | None,
    kpoints_flag: str | None,
    parameters_file: str | None,
) -> tuple[list[ParamSpec], dict[str, str], list[MemSpec]]:
    """Build the merged specs, settings and memory table from CLI + file.

    The file is read from ``parameters_file`` if given, else from the default
    name if it happens to exist; a missing default is fine as long as CLI flags
    supply the sweep. Returns ``(specs, file_settings, mem_specs)`` -
    ``file_settings`` may carry ``mode``/``kpoints_style``; ``mem_specs`` is the
    per-value ``--mem-per-cpu`` table (file-only; there is no CLI form).
    """
    file_specs: list[ParamSpec] = []
    settings: dict[str, str] = {}
    mem_specs: list[MemSpec] = []
    if parameters_file:
        file_specs, settings, mem_specs = parse_parameters_file(parameters_file)
    elif Path(PARAMETERS_FILENAME).is_file():
        file_specs, settings, mem_specs = parse_parameters_file(PARAMETERS_FILENAME)

    cli_specs: list[ParamSpec] = []
    for flag in incar_flags or []:
        cli_specs.append(parse_cli_incar(flag))
    if kpoints_flag:
        cli_specs.append(parse_cli_kpoints(kpoints_flag))

    return merge_specs(file_specs, cli_specs), settings, mem_specs


def mem_per_cpu_for(
    assignment: dict[str, str], specs: list[ParamSpec], mem_specs: list[MemSpec]
) -> str | None:
    """Greatest ``--mem-per-cpu`` value for one assignment, or None if none applies.

    Each :class:`MemSpec` is keyed by position to its driving parameter's swept
    values, so the assignment's value of that parameter selects the aligned
    memory value. When several tables apply, the largest value wins.
    """
    if not mem_specs:
        return None
    by_key = {s.key: s for s in specs}
    candidates: list[str] = []
    for m in mem_specs:
        drv = by_key.get(m.driver)
        if drv is None:
            continue
        actual = assignment.get(m.driver)
        if actual is None:
            continue
        table = dict(zip(drv.values, m.values))
        if actual in table:
            candidates.append(table[actual])
    return max(candidates, key=parse_mem_mb) if candidates else None


def set_mem_per_cpu(submit_path: Path, mem: str) -> None:
    """Set ``#SBATCH --mem-per-cpu=<mem>`` in a submit script, in place."""
    set_sbatch_directive(submit_path, "mem-per-cpu", mem)


def set_job_name(submit_path: Path, name: str) -> None:
    """Set ``#SBATCH --job-name=<name>`` (replacing ``-J`` too) in place."""
    set_sbatch_directive(submit_path, "job-name", name, short="J")


def _apply_parameters(
    run_dir: Path,
    specs: list[ParamSpec],
    assignment: dict[str, str],
    kpoints_style: str,
) -> None:
    """Edit the copied INCAR/KPOINTS in ``run_dir`` for one assignment."""
    incar_tags = {
        s.key: assignment[s.key] for s in specs if s.target == INCAR
    }
    if incar_tags:
        incar_mod.write_with_tags(run_dir / "INCAR", incar_tags)

    for s in specs:
        if s.target == KPOINTS:
            body = kpoints_mod.render_kpoints(assignment[s.key], kpoints_style)
            (run_dir / "KPOINTS").write_text(body)


def setup(
    *,
    incar_flags: list[str] | None = None,
    kpoints_flag: str | None = None,
    parameters_file: str | None = None,
    mode: str | None = None,
    kpoints_style: str | None = None,
    vasp_files: str = "VASP_Files",
    submit: str | None = None,
    root: str = "VASP_Parameter_Benchmarking",
    name_jobs: bool = True,
) -> list[Path]:
    """Generate the benchmarking tree. Returns the list of created directories.

    Every file in ``vasp_files`` is copied unchanged into each configuration;
    then the swept INCAR tags are set and, if swept, the KPOINTS grid is written.
    The submit script (``--submit``, default ``<vasp_files>/submit.sl``) is copied
    in as ``submit.sl``; the only directives it may edit are ``--mem-per-cpu``
    (from a ``mem_per_cpu`` table) and ``--job-name``: unless ``name_jobs`` is
    False, each job is named ``<JOB_NAME_PREFIX><folder>`` (e.g.
    ``vasp-para-bench-001``) so it is identifiable in ``squeue``/``sacct``.

    ``mode``/``kpoints_style`` given here (from the CLI) win over the parameters
    file; if neither sets them they default to ``grid`` / ``gamma``.
    """
    specs, file_settings, mem_specs = resolve_specs(
        incar_flags, kpoints_flag, parameters_file
    )
    if not specs:
        raise ValueError(
            "no parameters to sweep; add INCAR/KPOINTS lines to the parameters "
            "file or pass --incar/--kpoints"
        )
    # CLI flags may have changed a driver's values, so re-check the memory table
    # lines up with the final merged sweep before we persist it.
    validate_mem_specs(specs, mem_specs)

    # Precedence: CLI argument > parameters-file setting > built-in default.
    mode = mode or file_settings.get("mode") or "grid"
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode {mode!r}; use one of {', '.join(VALID_MODES)}")
    kpoints_style = kpoints_style or file_settings.get("kpoints_style") or kpoints_mod.GAMMA
    if kpoints_style not in (kpoints_mod.GAMMA, kpoints_mod.MONKHORST):
        raise ValueError(
            f"invalid kpoints_style {kpoints_style!r}; use 'gamma' or 'monkhorst'"
        )

    vasp_files_dir = Path(vasp_files)
    if not vasp_files_dir.is_dir():
        raise FileNotFoundError(f"VASP input directory not found: {vasp_files_dir}")

    missing = [f for f in REQUIRED_INPUTS if not (vasp_files_dir / f).is_file()]
    if missing:
        raise FileNotFoundError(
            f"missing required VASP input(s) in {vasp_files_dir}: {', '.join(missing)}"
        )

    sweeps_kpoints = any(s.target == KPOINTS for s in specs)
    if sweeps_kpoints and not (vasp_files_dir / "KPOINTS").is_file():
        # The generated grids replace it, but VASP convention expects a KPOINTS;
        # warn rather than fail since the grid files are written below regardless.
        print(
            f"note: no KPOINTS in {vasp_files_dir} - the swept grids will be written fresh."
        )

    # Resolve the submit script (copied unchanged into every config).
    submit_path = Path(submit) if submit else vasp_files_dir / SUBMIT_NAME
    if not submit_path.is_file():
        raise FileNotFoundError(
            f"submit script not found: {submit_path}\n"
            f"Place your (unchanged) submit.sl in {vasp_files_dir}/ or point at it with --submit."
        )

    configs = build_configs(specs, mode)

    # Everything in VASP_Files to copy into each run: inputs plus any extras,
    # including subdirectories (copied recursively) in case they are needed.
    input_items = sorted(vasp_files_dir.iterdir())

    root_dir = Path(root)
    root_dir.mkdir(parents=True, exist_ok=True)

    # Record the effective sweep (mode + parameters) in the user's own
    # parameters-file format, not JSON. The report reads this to learn which
    # tags were swept, their order (-> baseline) and the mode; the actual
    # per-config values are read back from each directory's INCAR/KPOINTS.
    (root_dir / PARAMETERS_FILENAME).write_text(
        render_parameters_file(specs, mode, kpoints_style, mem_specs)
    )

    # Existing numbered folders and the values they already hold. Additive setup
    # reuses any folder whose parameters match a needed combination and only
    # creates the missing ones; existing folders are never renamed or touched.
    existing = index_mod.config_dirs(root_dir)
    existing_assignments = {d: index_mod.read_assignment(d, specs) for d in existing}
    next_number = max((int(d.name) for d in existing), default=0) + 1

    created: list[Path] = []
    reused = 0
    for assignment in configs:
        if any(
            index_mod.assignment_matches(assignment, actual, specs)
            for actual in existing_assignments.values()
        ):
            reused += 1
            continue

        run_dir = root_dir / f"{next_number:0{NUMBER_WIDTH}d}"
        next_number += 1
        run_dir.mkdir(parents=True, exist_ok=True)

        for src in input_items:
            dest = run_dir / src.name
            if src.is_dir():
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)

        # The submit script is copied in (may already be among the inputs;
        # copying again as submit.sl makes the --submit override authoritative).
        shutil.copy2(submit_path, run_dir / SUBMIT_NAME)

        _apply_parameters(run_dir, specs, assignment, kpoints_style)
        # If a memory table was given, set this config's --mem-per-cpu directive.
        mem = mem_per_cpu_for(assignment, specs, mem_specs)
        if mem is not None:
            set_mem_per_cpu(run_dir / SUBMIT_NAME, mem)
        # Name the SLURM job after the folder so it is identifiable in squeue.
        if name_jobs:
            set_job_name(run_dir / SUBMIT_NAME, JOB_NAME_PREFIX + run_dir.name)
        # Record this new folder so later combinations in the same run can match it.
        existing_assignments[run_dir] = index_mod.read_assignment(run_dir, specs)
        created.append(run_dir)

    # (Re)write the navigator so combinations can be looked up by folder number.
    index_path = index_mod.write_index(root_dir, specs)

    print(
        f"{len(configs)} combination(s) in this sweep (mode: {mode}): "
        f"created {len(created)} new folder(s), reused {reused} existing."
    )
    print("Sweeping:")
    for s in specs:
        target = "KPOINTS grid" if s.target == KPOINTS else f"INCAR {s.key}"
        print(f"  - {target}: {', '.join(s.values)}")
    for m in mem_specs:
        print(f"  - mem-per-cpu (from {m.driver}): {', '.join(m.values)} "
              "[written into each submit.sl]")
    if name_jobs:
        print(f"  - job name: set to {JOB_NAME_PREFIX}<folder> in each submit.sl")
    print(f"Folder navigator: {index_path}")
    return created
