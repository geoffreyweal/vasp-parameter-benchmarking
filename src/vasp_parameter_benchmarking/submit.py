"""Part 2: submit the generated jobs to SLURM (and reset errored ones).

``submit`` classifies every config first (same rules as the folder navigator)
and only submits the ones that need running - **pending** configs, and
**failed** ones (which are reset to their inputs first). Completed, running and
errored configs are never submitted: errors usually need attention (more
memory, a longer time limit, a fixed input) before rerunning, so clear them
explicitly with ``reset``, which returns them to pending.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from . import index as index_mod

# Pause briefly after this many submissions to avoid hammering the scheduler /
# tripping QOS submission-rate limits.
PAUSE_EVERY = 10
PAUSE_SECONDS = 2

# Files kept when resetting a failed run before resubmitting it. These are the
# per-config inputs, already edited by setup (the INCAR/KPOINTS are the record).
RESET_KEEP = {"INCAR", "KPOINTS", "POTCAR", "POSCAR", "submit.sl"}


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
) -> int:
    """Submit the configs that need running. Returns the number submitted.

    Every config is classified first (same rules as the folder navigator) and
    only **pending** and **failed** ones are submitted - failed directories are
    reset to their inputs first. Completed, still-running and errored configs
    are skipped; clear errors explicitly with :func:`reset` once you have
    addressed their cause.

    Each script is submitted exactly as it sits in its folder; any per-config
    ``--mem-per-cpu`` was already written into ``submit.sl`` by ``setup``.
    """
    scripts = find_submit_scripts(root)
    if not scripts:
        print(f"No submit.sl files found under {root}/")
        return 0

    to_submit: list[tuple[Path, bool]] = []  # (script, needs_reset)
    counts = {"done": 0, "running": 0, "error": 0}
    for s in scripts:
        status, _detail = index_mod.run_status(s.parent)
        if status in ("pending", "failed"):
            to_submit.append((s, status == "failed"))
        else:
            counts[status] += 1
    print(
        f"Found {len(scripts)} configs under {root}/: "
        f"{counts['done']} run, {counts['running']} running, "
        f"{counts['error']} error (all skipped); {len(to_submit)} to submit."
    )
    if counts["error"]:
        print("Errored configs are never resubmitted as-is - fix the cause, then "
              "run 'vasp-parameter-benchmarking reset' to make them pending.")
    if not to_submit:
        print("Nothing to submit.")
        return 0

    if dry_run:
        for script, needs_reset in to_submit:
            prefix = "reset + " if needs_reset else ""
            print(f"[dry-run] {prefix}sbatch (cwd={script.parent}) submit.sl")
        return 0

    if not yes:
        reply = input(
            f"Submit {len(to_submit)} job(s) to SLURM? [y/N] "
        ).strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 0

    submitted = 0
    for i, (script, needs_reset) in enumerate(to_submit, start=1):
        if needs_reset:
            removed = reset_run_dir(script.parent)
            print(f"[{i}/{len(to_submit)}] reset {script.parent} ({removed} files removed)")
        try:
            result = subprocess.run(
                ["sbatch", script.name],
                cwd=script.parent,
                capture_output=True,
                text=True,
                check=True,
            )
            print(f"[{i}/{len(to_submit)}] {script.parent}: {result.stdout.strip()}")
            submitted += 1
        except FileNotFoundError:
            print("ERROR: 'sbatch' not found - are you on a SLURM login node?")
            break
        except subprocess.CalledProcessError as exc:
            print(f"[{i}/{len(to_submit)}] FAILED {script.parent}: {exc.stderr.strip()}")

        if i % PAUSE_EVERY == 0 and i < len(to_submit):
            time.sleep(PAUSE_SECONDS)

    print(f"Submitted {submitted}/{len(to_submit)} jobs.")
    return submitted


def reset(
    root: str = "VASP_Parameter_Benchmarking",
    dry_run: bool = False,
    yes: bool = False,
) -> int:
    """Reset every **errored** config back to its inputs. Returns the count.

    A config counts as errored when it finished with an identifiable error (a
    VASP abort message in the OUTCAR, an abnormal SLURM terminal state, or an
    error line in ``slurm-<id>.out``). Resetting deletes everything except the
    inputs (:data:`RESET_KEEP`), returning the config to **pending** so the
    next ``submit`` picks it up. Completed, running, failed and pending configs
    are untouched.

    Each reset config's ``--mem-per-cpu`` is re-applied from the *current*
    ``mem_per_cpu`` table, so the usual OOM recovery - raise the table, re-run
    ``setup`` (which records it), then ``reset`` - relaunches with the new
    memory even though ``setup`` never edits existing folders.
    """
    root_dir = Path(root)
    if not root_dir.is_dir():
        raise FileNotFoundError(f"benchmark root not found: {root_dir}")

    targets: list[tuple[Path, str]] = []
    for d in index_mod.config_dirs(root_dir):
        status, detail = index_mod.run_status(d)
        if status == "error":
            targets.append((d, detail or "unknown error"))

    if not targets:
        print(f"No errored configs under {root_dir}/ - nothing to reset.")
        return 0

    print(f"{len(targets)} errored config(s) under {root_dir}/:")
    for d, detail in targets:
        print(f"  {d.name}: {detail}")

    if dry_run:
        print("[dry-run] no files were removed.")
        return 0

    if not yes:
        reply = input(
            f"Reset {len(targets)} errored config(s) to their inputs? [y/N] "
        ).strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 0

    # Load the current sweep + memory table so each reset config's
    # --mem-per-cpu can be refreshed (setup never edits existing folders).
    from .generate import PARAMETERS_FILENAME, mem_per_cpu_for, set_mem_per_cpu
    from .parameters import parse_parameters_file

    specs, mem_specs = [], []
    params_path = root_dir / PARAMETERS_FILENAME
    if params_path.is_file():
        specs, _settings, mem_specs = parse_parameters_file(params_path)

    for d, _detail in targets:
        removed = reset_run_dir(d)
        note = ""
        if mem_specs:
            assignment = index_mod.read_assignment(d, specs)
            mem = mem_per_cpu_for(assignment, specs, mem_specs)
            if mem is not None:
                set_mem_per_cpu(d / "submit.sl", mem)
                note = f", --mem-per-cpu={mem}"
        print(f"  reset {d} ({removed} files removed{note})")

    # Refresh the navigator so these configs show as pending again.
    try:
        index_path, _entries = index_mod.refresh_index(root_dir)
        print(f"Refreshed folder navigator -> {index_path}")
    except FileNotFoundError:
        pass  # no recorded parameters file (unusual); the reset itself is done

    print(f"Reset {len(targets)} config(s); run 'submit' to relaunch them.")
    return len(targets)
