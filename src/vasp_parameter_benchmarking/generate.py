"""Part 1: generate the VASP parameter-benchmarking directory tree.

For every parameter combination this creates a directory, copies the VASP inputs
*and the submit.sl unchanged*, then edits only the swept parameters: the relevant
INCAR tags are set, and (if the KPOINTS grid is swept) a fresh KPOINTS file is
written. No per-config manifest is written - the generated INCAR/KPOINTS in each
directory *are* the record, and the report reads the values back out of them.

The effective sweep (mode + parameters) is written once to
``<root>/vasp_parameter_benchmarking_parameters.txt`` so the report knows which
tags were swept, in what order, and what the baseline is.

Unlike vasp-core-benchmarking, the submit.sl here is never rewritten - the
parallel layout and all SLURM directives are exactly what you provide.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import incar as incar_mod
from . import kpoints as kpoints_mod
from .parameters import (
    INCAR,
    KPOINTS,
    VALID_MODES,
    ParamSpec,
    build_configs,
    config_name,
    merge_specs,
    parse_cli_incar,
    parse_cli_kpoints,
    parse_parameters_file,
    render_parameters_file,
)

# VASP inputs that must be present in the inputs directory. KPOINTS is required
# only when the KPOINTS grid is being swept (otherwise it is copied if present).
REQUIRED_INPUTS = ["INCAR", "POSCAR", "POTCAR"]

PARAMETERS_FILENAME = "vasp_parameter_benchmarking_parameters.txt"
SUBMIT_NAME = "submit.sl"


def resolve_specs(
    incar_flags: list[str] | None,
    kpoints_flag: str | None,
    parameters_file: str | None,
) -> tuple[list[ParamSpec], dict[str, str]]:
    """Build the merged specs and settings from CLI flags + parameters file.

    The file is read from ``parameters_file`` if given, else from the default
    name if it happens to exist; a missing default is fine as long as CLI flags
    supply the sweep. Returns ``(specs, file_settings)`` - ``file_settings`` may
    carry ``mode``/``kpoints_style`` read from the file.
    """
    file_specs: list[ParamSpec] = []
    settings: dict[str, str] = {}
    if parameters_file:
        file_specs, settings = parse_parameters_file(parameters_file)
    elif Path(PARAMETERS_FILENAME).is_file():
        file_specs, settings = parse_parameters_file(PARAMETERS_FILENAME)

    cli_specs: list[ParamSpec] = []
    for flag in incar_flags or []:
        cli_specs.append(parse_cli_incar(flag))
    if kpoints_flag:
        cli_specs.append(parse_cli_kpoints(kpoints_flag))

    return merge_specs(file_specs, cli_specs), settings


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
) -> list[Path]:
    """Generate the benchmarking tree. Returns the list of created directories.

    Every file in ``vasp_files`` is copied unchanged into each configuration;
    then the swept INCAR tags are set and, if swept, the KPOINTS grid is written.
    The submit script (``--submit``, default ``<vasp_files>/submit.sl``) is copied
    in as ``submit.sl`` unchanged.

    ``mode``/``kpoints_style`` given here (from the CLI) win over the parameters
    file; if neither sets them they default to ``grid`` / ``gamma``.
    """
    specs, file_settings = resolve_specs(incar_flags, kpoints_flag, parameters_file)
    if not specs:
        raise ValueError(
            "no parameters to sweep; add INCAR/KPOINTS lines to the parameters "
            "file or pass --incar/--kpoints"
        )

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
        render_parameters_file(specs, mode, kpoints_style)
    )

    created: list[Path] = []
    for assignment in configs:
        run_dir = root_dir / config_name(specs, assignment)
        run_dir.mkdir(parents=True, exist_ok=True)

        for src in input_items:
            dest = run_dir / src.name
            if src.is_dir():
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)

        # The submit script is copied unchanged (may already be among the inputs;
        # copying again as submit.sl makes the --submit override authoritative).
        shutil.copy2(submit_path, run_dir / SUBMIT_NAME)

        _apply_parameters(run_dir, specs, assignment, kpoints_style)
        created.append(run_dir)

    print(f"Created {len(created)} parameter configurations under {root_dir}/ (mode: {mode})")
    print("Sweeping:")
    for s in specs:
        target = "KPOINTS grid" if s.target == KPOINTS else f"INCAR {s.key}"
        print(f"  - {target}: {', '.join(s.values)}")
    return created
