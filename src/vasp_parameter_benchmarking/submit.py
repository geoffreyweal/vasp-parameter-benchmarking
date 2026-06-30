"""Part 2: submit every generated ``submit.sl`` to SLURM.

The submit scripts are run exactly as generated (which is exactly as you wrote
them - this tool never edits submit.sl). With ``--retry-failed`` only configs
that produced no usable result are reset to their inputs and resubmitted.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .outcar import final_energy

# Pause briefly after this many submissions to avoid hammering the scheduler /
# tripping QOS submission-rate limits.
PAUSE_EVERY = 10
PAUSE_SECONDS = 2

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
) -> int:
    """Submit benchmark jobs. Returns the number successfully submitted.

    By default every config is submitted. With ``retry_failed``, only configs
    that have **not** produced a usable final energy are (re)submitted, and each
    such directory is first reset to just its inputs and submit.sl.

    Each script is submitted exactly as it sits in its folder; any per-config
    ``--mem-per-cpu`` was already written into ``submit.sl`` by ``setup``.
    """
    scripts = find_submit_scripts(root)
    if not scripts:
        print(f"No submit.sl files found under {root}/")
        return 0

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
            print(f"[dry-run] {prefix}sbatch (cwd={script.parent}) submit.sl")
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
        try:
            result = subprocess.run(
                ["sbatch", script.name],
                cwd=script.parent,
                capture_output=True,
                text=True,
                check=True,
            )
            print(f"[{i}/{len(scripts)}] {script.parent}: {result.stdout.strip()}")
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
