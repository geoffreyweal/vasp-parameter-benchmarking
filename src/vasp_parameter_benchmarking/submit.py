"""Part 2: submit every generated ``submit.sl`` to SLURM.

The submit scripts are run exactly as generated (which is exactly as you wrote
them - this tool never edits submit.sl). With ``--retry-failed`` only configs
that produced no usable result are reset to their inputs and resubmitted.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from . import index as index_mod
from .outcar import final_energy
from .parameters import MemSpec, ParamSpec, parse_mem_mb, parse_parameters_file

# Pause briefly after this many submissions to avoid hammering the scheduler /
# tripping QOS submission-rate limits.
PAUSE_EVERY = 10
PAUSE_SECONDS = 2

# Where setup records the sweep + memory table inside the benchmark root.
PARAMETERS_FILENAME = "vasp_parameter_benchmarking_parameters.txt"

# Files kept when resetting a failed run before resubmitting it. These are the
# per-config inputs, already edited by setup (the INCAR/KPOINTS are the record).
RESET_KEEP = {"INCAR", "KPOINTS", "POTCAR", "POSCAR", "submit.sl"}


def has_result(run_dir: Path) -> bool:
    """True if this run produced a usable final energy.

    Matches the report's validity test: an OUTCAR from which a final
    ``energy(sigma->0)`` can be read (i.e. at least one electronic step finished).
    """
    outcar = run_dir / "OUTCAR"
    if not outcar.is_file():
        return False
    return final_energy(outcar) is not None


def reset_run_dir(run_dir: Path) -> int:
    """Delete everything in ``run_dir`` except the inputs and submit.sl.

    Returns the number of files removed. Keeps the files in :data:`RESET_KEEP`,
    so the directory is a clean starting point for a fresh run.
    """
    removed = 0
    for p in run_dir.iterdir():
        if p.is_file() and p.name not in RESET_KEEP:
            p.unlink()
            removed += 1
    return removed


def load_mem_table(
    root: str,
) -> tuple[list[ParamSpec], list[MemSpec]]:
    """Read the swept specs + memory table from the root parameters file.

    Returns ``([], [])`` if the file is absent or has no ``mem_per_cpu`` lines,
    so submission proceeds normally (the script's own directive is used).
    """
    path = Path(root) / PARAMETERS_FILENAME
    if not path.is_file():
        return [], []
    specs, _settings, mem_specs = parse_parameters_file(path)
    return specs, mem_specs


def mem_per_cpu_for(
    run_dir: Path, specs: list[ParamSpec], mem_specs: list[MemSpec]
) -> str | None:
    """Greatest ``--mem-per-cpu`` value for this config, or None if none applies.

    Each :class:`MemSpec` is keyed by position to its driving parameter's swept
    values: the folder's own value of that parameter is matched (the same way the
    navigator matches values) to find the aligned memory value. When several
    tables apply, the largest memory value wins.
    """
    if not mem_specs:
        return None
    assignment = index_mod.read_assignment(run_dir, specs)
    by_key = {s.key: s for s in specs}
    candidates: list[str] = []
    for m in mem_specs:
        drv = by_key.get(m.driver)
        if drv is None:
            continue
        actual = assignment.get(m.driver)
        if actual is None:
            continue
        for i, dv in enumerate(drv.values):
            if index_mod.values_match(drv, actual, dv):
                candidates.append(m.values[i])
                break
    if not candidates:
        return None
    return max(candidates, key=parse_mem_mb)


def find_submit_scripts(root: str) -> list[Path]:
    """Return every ``submit.sl`` beneath ``root``, sorted by directory name."""
    root_dir = Path(root)
    if not root_dir.is_dir():
        raise FileNotFoundError(f"benchmark root not found: {root_dir}")
    return sorted(root_dir.rglob("submit.sl"), key=lambda p: p.parent.name)


def submit(
    root: str = "VASP_Parameter_Benchmarking",
    dry_run: bool = False,
    yes: bool = False,
    retry_failed: bool = False,
    no_mem: bool = False,
) -> int:
    """Submit benchmark jobs. Returns the number successfully submitted.

    By default every config is submitted. With ``retry_failed``, only configs
    that have **not** produced a usable final energy are (re)submitted, and each
    such directory is first reset to just its inputs and submit.sl.

    If the parameters file in ``root`` carries a ``mem_per_cpu`` table, each job
    is launched with ``sbatch --mem-per-cpu=<value>`` (the value chosen from that
    config's swept parameters; the greatest when several tables apply). The CLI
    flag overrides the script's own directive, so ``submit.sl`` stays unchanged.
    Pass ``no_mem`` to ignore the table and submit the scripts as-is.
    """
    scripts = find_submit_scripts(root)
    if not scripts:
        print(f"No submit.sl files found under {root}/")
        return 0

    specs, mem_specs = ([], []) if no_mem else load_mem_table(root)
    if mem_specs:
        drivers = ", ".join(sorted({m.driver for m in mem_specs}))
        print(f"Applying per-config --mem-per-cpu from the {drivers} memory table.")

    if retry_failed:
        all_n = len(scripts)
        scripts = [s for s in scripts if not has_result(s.parent)]
        print(
            f"Found {all_n} configs under {root}/; "
            f"{all_n - len(scripts)} already have a result, "
            f"{len(scripts)} to retry."
        )
        if not scripts:
            print("Nothing to retry - every config has a usable result.")
            return 0
    else:
        print(f"Found {len(scripts)} submit.sl scripts under {root}/")

    if dry_run:
        for script in scripts:
            prefix = "reset + " if retry_failed else ""
            mem = mem_per_cpu_for(script.parent, specs, mem_specs)
            mem_flag = f" --mem-per-cpu={mem}" if mem else ""
            print(f"[dry-run] {prefix}sbatch{mem_flag} (cwd={script.parent}) submit.sl")
        return 0

    if not yes:
        action = (
            f"Reset and resubmit {len(scripts)} failed/incomplete jobs"
            if retry_failed
            else f"Submit all {len(scripts)} jobs to SLURM"
        )
        reply = input(f"{action}? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 0

    submitted = 0
    for i, script in enumerate(scripts, start=1):
        if retry_failed:
            removed = reset_run_dir(script.parent)
            print(f"[{i}/{len(scripts)}] reset {script.parent} ({removed} files removed)")
        mem = mem_per_cpu_for(script.parent, specs, mem_specs)
        cmd = ["sbatch"] + ([f"--mem-per-cpu={mem}"] if mem else []) + [script.name]
        try:
            result = subprocess.run(
                cmd,
                cwd=script.parent,
                capture_output=True,
                text=True,
                check=True,
            )
            mem_note = f" (--mem-per-cpu={mem})" if mem else ""
            print(f"[{i}/{len(scripts)}] {script.parent}{mem_note}: {result.stdout.strip()}")
            submitted += 1
        except FileNotFoundError:
            print("ERROR: 'sbatch' not found - are you on a SLURM login node?")
            break
        except subprocess.CalledProcessError as exc:
            print(f"[{i}/{len(scripts)}] FAILED {script.parent}: {exc.stderr.strip()}")

        if i % PAUSE_EVERY == 0 and i < len(scripts):
            time.sleep(PAUSE_SECONDS)

    print(f"Submitted {submitted}/{len(scripts)} jobs.")
    return submitted
