"""Optional cleanup: delete files not needed to keep/analyse a VASP benchmark.

After a sweep the run directories hold large output files (WAVECAR, CHGCAR,
vaspout.h5, vasprun.xml, ML_FF, ...) that are not needed once the energy and
timing data have been recorded. This removes everything except the inputs, the
OUTCAR / OSZICAR, the submit script, parameters.json and the slurm logs.
"""

from __future__ import annotations

import re
from pathlib import Path

from tqdm import tqdm

# Files kept in each benchmark directory; everything else is deleted.
KEEP_NAMES = {
    "INCAR", "KPOINTS", "POTCAR", "POSCAR", "OUTCAR", "OSZICAR",
    "vasp_parameter_benchmarking_parameters.txt", "folder_index.html",
}
# Extensions kept regardless of name (scripts: submit.sl, *.sh helpers).
KEEP_SUFFIXES = {".sh", ".sl"}
# slurm logs, e.g. slurm-7072949.out / slurm-6332267.err
_SLURM_LOG_RE = re.compile(r"^slurm-.*\.(out|err)$")


def is_kept(name: str) -> bool:
    """True if a file with this basename should be preserved."""
    return (
        name in KEEP_NAMES
        or Path(name).suffix in KEEP_SUFFIXES
        or bool(_SLURM_LOG_RE.match(name))
    )


def human_size(num_bytes: float) -> str:
    """Format a byte count as a short human-readable string."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def find_deletable(root: str) -> list[Path]:
    """Return every file under ``root`` that is not on the keep-list."""
    root_dir = Path(root)
    if not root_dir.is_dir():
        raise FileNotFoundError(f"benchmark root not found: {root_dir}")
    return sorted(
        p for p in root_dir.rglob("*") if p.is_file() and not is_kept(p.name)
    )


def clean(
    root: str = "VASP_Parameter_Benchmarking",
    dry_run: bool = False,
    yes: bool = False,
) -> int:
    """Delete unnecessary files under ``root``. Returns the number deleted.

    Keeps INCAR, KPOINTS, POTCAR, POSCAR, OUTCAR, OSZICAR, parameters.json,
    *.sh/*.sl and slurm-*.out/.err in every directory; deletes everything else.
    """
    files = find_deletable(root)
    if not files:
        print(f"Nothing to clean under {root}/ - only kept files are present.")
        return 0

    total = sum(f.stat().st_size for f in files)
    print(f"Found {len(files)} files to delete under {root}/ ({human_size(total)}).")
    print(
        "Keeping per directory: INCAR, KPOINTS, POTCAR, POSCAR, OUTCAR, OSZICAR, "
        "*.sh, *.sl, slurm-*.out/.err"
    )

    if dry_run:
        for f in files:
            print(f"[dry-run] rm {f}  ({human_size(f.stat().st_size)})")
        print(f"[dry-run] would free ~{human_size(total)}.")
        return 0

    if not yes:
        reply = (
            input(f"Delete these {len(files)} files ({human_size(total)})? [y/N] ")
            .strip()
            .lower()
        )
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 0

    deleted = 0
    freed = 0
    progress = tqdm(files, desc="Deleting", unit="file")
    for f in progress:
        progress.set_postfix_str(str(f))
        try:
            size = f.stat().st_size
            f.unlink()
            deleted += 1
            freed += size
        except OSError as exc:
            progress.write(f"FAILED to delete {f}: {exc}")
    progress.close()

    print(f"Deleted {deleted}/{len(files)} files, freed ~{human_size(freed)}.")
    return deleted
