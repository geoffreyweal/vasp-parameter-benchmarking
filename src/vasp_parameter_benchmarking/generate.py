"""Part 1: generate the VASP parameter-benchmarking directory tree.

For every parameter combination this creates a directory, copies the VASP inputs
*and the submit.sl unchanged*, then edits only the swept parameters: the relevant
INCAR tags are set, and (if the KPOINTS grid is swept) a fresh KPOINTS file is
written. A ``parameters.json`` recording the exact values is dropped in each
directory for the report to read back.

Unlike vasp-core-benchmarking, the submit.sl here is never rewritten - the
parallel layout and all SLURM directives are exactly what you provide.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import incar as incar_mod
from . import kpoints as kpoints_mod
from .parameters import (
    INCAR,
    KPOINTS,
    ParamSpec,
    build_configs,
    config_name,
    config_record,
    merge_specs,
    parse_cli_incar,
    parse_cli_kpoints,
    parse_parameters_file,
)

# VASP inputs that must be present in the inputs directory. KPOINTS is required
# only when the KPOINTS grid is being swept (otherwise it is copied if present).
REQUIRED_INPUTS = ["INCAR", "POSCAR", "POTCAR"]

DEFAULT_PARAMETERS_FILE = "vasp_parameter_benchmarking_parameters.txt"
SUBMIT_NAME = "submit.sl"


def resolve_specs(
    incar_flags: list[str] | None,
    kpoints_flag: str | None,
    parameters_file: str | None,
) -> list[ParamSpec]:
    """Build the merged list of parameter specs from CLI flags + file.

    The file is read from ``parameters_file`` if given, else from the default
    name if it happens to exist; a missing default is fine as long as CLI flags
    supply the sweep.
    """
    file_specs: list[ParamSpec] = []
    if parameters_file:
        file_specs = parse_parameters_file(parameters_file)
    elif Path(DEFAULT_PARAMETERS_FILE).is_file():
        file_specs = parse_parameters_file(DEFAULT_PARAMETERS_FILE)

    cli_specs: list[ParamSpec] = []
    for flag in incar_flags or []:
        cli_specs.append(parse_cli_incar(flag))
    if kpoints_flag:
        cli_specs.append(parse_cli_kpoints(kpoints_flag))

    return merge_specs(file_specs, cli_specs)


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
    mode: str = "grid",
    kpoints_style: str = kpoints_mod.GAMMA,
    vasp_files: str = "VASP_Files",
    submit: str | None = None,
    root: str = "VASP_Parameter_Benchmarking",
) -> list[Path]:
    """Generate the benchmarking tree. Returns the list of created directories.

    Every file in ``vasp_files`` is copied unchanged into each configuration;
    then the swept INCAR tags are set and, if swept, the KPOINTS grid is written.
    The submit script (``--submit``, default ``<vasp_files>/submit.sl``) is copied
    in as ``submit.sl`` unchanged.
    """
    specs = resolve_specs(incar_flags, kpoints_flag, parameters_file)
    if not specs:
        raise ValueError(
            "no parameters to sweep; pass --incar/--kpoints or a --parameters file"
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

    # A top-level manifest records the sweep so the report knows the spec order,
    # targets, values and baseline without re-deriving them from directory names.
    manifest = {
        "mode": mode,
        "kpoints_style": kpoints_style,
        "specs": [
            {"target": s.target, "key": s.key, "values": list(s.values)} for s in specs
        ],
        "baseline": {s.key: s.values[0] for s in specs},
    }
    (root_dir / "benchmark_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

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

        (run_dir / "parameters.json").write_text(
            json.dumps(config_record(specs, assignment, mode), indent=2) + "\n"
        )
        created.append(run_dir)

    print(f"Created {len(created)} parameter configurations under {root_dir}/ (mode: {mode})")
    print("Sweeping:")
    for s in specs:
        target = "KPOINTS grid" if s.target == KPOINTS else f"INCAR {s.key}"
        print(f"  - {target}: {', '.join(s.values)}")
    return created
