"""Part 2: submit the generated jobs to SLURM (and reset errored ones).

``submit`` classifies every config first (same rules as the folder navigator)
and only submits the ones that need running - **pending** configs, and
**failed** ones (which are reset to their inputs first). Completed, running and
errored configs are never submitted: errors usually need attention (more
memory, a longer time limit, a fixed input) before rerunning, so clear them
explicitly with ``reset``, which returns them to pending.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from . import index as index_mod

# Pause briefly after this many submissions to avoid hammering the scheduler /
# tripping QOS submission-rate limits.
PAUSE_EVERY = 10
PAUSE_SECONDS = 2

# Why a --submit-only request is refused, phrased for the note it appears in.
STATUS_WORD = {
    "done": "already run",
    "running": "still running",
    "error": "error - fix the cause and use 'reset' first",
}

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


def _parse_folder_list(flags: list[str] | None) -> set[int] | None:
    """Parse folder-number lists (comma- and/or space-separated) into ints.

    Accepts ``"003"`` and ``"3"`` alike (folders are matched by number, not by
    zero-padding). Returns None when nothing was given at all.
    """
    if not flags:
        return None
    numbers: set[int] = set()
    for flag in flags:
        for token in re.split(r"[\s,]+", flag):
            if not token:
                continue
            if not token.isdigit():
                raise ValueError(
                    f"invalid folder number {token!r}: expected e.g. '003' or '3'"
                )
            numbers.add(int(token))
    return numbers


def _folder_number(script: Path) -> int | None:
    """The numeric folder name a submit.sl sits in, or None if non-numeric."""
    name = script.parent.name
    return int(name) if name.isdigit() else None


def _print_plan(to_submit: list[tuple[Path, bool]]) -> None:
    """Show exactly which folders would be submitted, and why."""
    print(f"Will submit {len(to_submit)} job(s):")
    for script, needs_reset in to_submit:
        why = "failed - will reset first" if needs_reset else "pending"
        print(f"  {script.parent.name}  ({why})")


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
    submit_only: list[str] | None = None,
    reject: list[str] | None = None,
) -> int:
    """Submit the configs that need running. Returns the number submitted.

    Every config is classified first (same rules as the folder navigator) and
    only **pending** and **failed** ones are submitted - failed directories are
    reset to their inputs first. Completed, still-running and errored configs
    are skipped; clear errors explicitly with :func:`reset` once you have
    addressed their cause. The exact list of folders to be submitted is shown
    before the confirmation prompt.

    ``submit_only`` restricts submission to the given folder numbers (the
    status rules still apply - a completed/running/errored folder is refused
    with a note, never force-submitted); ``reject`` excludes the given
    folder numbers. Both take comma-separated numbers and may be repeated.
    The same narrowing is available interactively: at the confirmation prompt,
    ``o`` asks for folder numbers to submit *only*, and ``r`` asks for folder
    numbers to *reject*; the plan is re-shown after each edit.

    Each script is submitted exactly as it sits in its folder; any per-config
    ``--mem-per-cpu`` was already written into ``submit.sl`` by ``setup``.
    """
    only = _parse_folder_list(submit_only)
    skip = _parse_folder_list(reject)

    scripts = find_submit_scripts(root)
    if not scripts:
        print(f"No submit.sl files found under {root}/")
        return 0

    to_submit: list[tuple[Path, bool]] = []  # (script, needs_reset)
    counts = {"done": 0, "running": 0, "error": 0}
    status_by_number: dict[int, str] = {}
    for s in scripts:
        status, _detail = index_mod.run_status(s.parent)
        number = _folder_number(s)
        if number is not None:
            status_by_number[number] = status
        if status in ("pending", "failed"):
            to_submit.append((s, status == "failed"))
        else:
            counts[status] += 1
    print(
        f"Found {len(scripts)} configs under {root}/: "
        f"{counts['done']} run, {counts['running']} running, "
        f"{counts['error']} error (all skipped); {len(to_submit)} eligible."
    )
    if counts["error"]:
        print("Errored configs are never resubmitted as-is - fix the cause, then "
              "run 'vasp-parameter-benchmarking reset' to make them pending.")

    # --submit-only: keep only the requested folders, and explain any request
    # that cannot be honoured (unknown folder, or one the status rules refuse).
    if only is not None:
        for n in sorted(only):
            if n not in status_by_number:
                print(f"note: --submit-only {n:03d}: no such config folder.")
            elif status_by_number[n] not in ("pending", "failed"):
                print(
                    f"note: --submit-only {n:03d}: not submitted "
                    f"(status: {STATUS_WORD[status_by_number[n]]})."
                )
        to_submit = [
            (s, r) for s, r in to_submit
            if _folder_number(s) is not None and _folder_number(s) in only
        ]

    # --reject: drop the excluded folders from the plan.
    if skip:
        excluded = [
            s.parent.name for s, _r in to_submit
            if _folder_number(s) in skip
        ]
        if excluded:
            print(f"Excluded via --reject: {', '.join(excluded)}")
        to_submit = [
            (s, r) for s, r in to_submit if _folder_number(s) not in skip
        ]

    if not to_submit:
        print("Nothing to submit.")
        return 0

    # Always show exactly what would be launched before doing anything.
    _print_plan(to_submit)

    if dry_run:
        print("[dry-run] nothing was submitted.")
        return 0

    if not yes:
        # Interactive confirmation: y submits, o/r edit the plan first.
        while True:
            reply = input(
                f"Submit these {len(to_submit)} job(s) to SLURM? "
                "[y=submit / N=abort / o=only these... / r=reject these...] "
            ).strip().lower()
            if reply in ("y", "yes"):
                break
            if reply in ("o", "only", "r", "reject"):
                keep_mode = reply.startswith("o")
                raw = input(
                    "Folder number(s) to submit ONLY (e.g. 3, 5): " if keep_mode
                    else "Folder number(s) to REJECT (e.g. 3, 5): "
                )
                try:
                    chosen = _parse_folder_list([raw])
                except ValueError as exc:
                    print(f"  {exc}")
                    continue
                if not chosen:
                    print("  no folder numbers entered.")
                    continue
                if keep_mode:
                    plan_numbers = {_folder_number(s) for s, _r in to_submit}
                    for n in sorted(chosen - plan_numbers):
                        st = status_by_number.get(n)
                        why = (
                            STATUS_WORD.get(st, "not in the current plan")
                            if st else "no such config folder"
                        )
                        print(f"  note: {n:03d} skipped ({why}).")
                    to_submit = [
                        (s, r) for s, r in to_submit
                        if _folder_number(s) in chosen
                    ]
                else:
                    to_submit = [
                        (s, r) for s, r in to_submit
                        if _folder_number(s) not in chosen
                    ]
                if not to_submit:
                    print("Nothing left to submit.")
                    return 0
                _print_plan(to_submit)
                continue
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
