"""Querying SLURM accounting (``sacct``) for per-job utilisation metrics.

Job ids are recovered from ``slurm-<jobid>.out`` files left in each run
directory; the most recent (largest) id is used when a directory has been rerun.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

_SLURM_OUT_RE = re.compile(r"slurm-(\d+)\.out$")


def find_job_id(run_dir: str | Path) -> int | None:
    """Return the largest job id from ``slurm-<id>.out`` files in ``run_dir``."""
    ids = []
    for entry in Path(run_dir).iterdir():
        m = _SLURM_OUT_RE.search(entry.name)
        if m:
            ids.append(int(m.group(1)))
    return max(ids) if ids else None


def run_sacct(job_id: int) -> dict | None:
    """Run ``sacct --json -j <job_id>`` and return the parsed JSON, or None."""
    try:
        result = subprocess.run(
            ["sacct", "--json", "-j", str(job_id)],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _number(value):
    """sacct --json encodes numbers either bare or as {"number": N, ...}."""
    if isinstance(value, dict):
        return value.get("number")
    return value


def parse_sacct(data: dict) -> tuple[float, float, float] | None:
    """Extract ``(elapsed_s, total_cpu_s, max_rss_gb)`` from sacct JSON.

    Peak memory is read the same way as cp2k-/orca-benchmarking: the maximum
    ``count`` of the ``type == "mem"`` entry in each step's
    ``tres.requested.total`` list, converted to GB (bytes / 1024**3).
    """
    jobs = data.get("jobs") or []
    if not jobs:
        return None
    job = jobs[0]

    elapsed = _number(job.get("time", {}).get("elapsed")) or 0

    total = job.get("time", {}).get("total", {})
    total_cpu = (_number(total.get("seconds")) or 0) + (
        _number(total.get("microseconds")) or 0
    ) / 1e6

    # Peak memory across all steps, from per-step tres.requested.total mem entry.
    max_mem_bytes = 0
    for step in job.get("steps", []) or []:
        tres = step.get("tres", {}) or {}
        for item in tres.get("requested", {}).get("total", []) or []:
            if item.get("type") == "mem":
                max_mem_bytes = max(max_mem_bytes, _number(item.get("count")) or 0)

    return float(elapsed), float(total_cpu), max_mem_bytes / (1024 ** 3)


def get_utilisation(run_dir: str | Path) -> tuple[float, float, float] | None:
    """Convenience: find the job id under ``run_dir`` and query sacct for it."""
    job_id = find_job_id(run_dir)
    if job_id is None:
        return None
    data = run_sacct(job_id)
    if data is None:
        return None
    return parse_sacct(data)


# SLURM states that mean the job is still in the system (not finished), so a run
# with no result yet is in progress rather than failed. Matched as a prefix
# because sacct may decorate a state (e.g. "CANCELLED by 12345").
_ACTIVE_STATES = (
    "RUNNING", "PENDING", "REQUEUED", "REQUEUE_HOLD", "RESIZING",
    "SUSPENDED", "CONFIGURING", "COMPLETING", "SIGNALING", "STAGE_OUT",
)


def job_state(job_id: int) -> str | None:
    """Return the SLURM state of ``job_id`` (e.g. ``"RUNNING"``, ``"FAILED"``).

    Returns None if sacct is unavailable or the job is unknown to it.
    """
    data = run_sacct(job_id)
    if not data:
        return None
    jobs = data.get("jobs") or []
    if not jobs:
        return None
    state = jobs[0].get("state")
    # sacct --json gives state as {"current": ["RUNNING"], ...}; tolerate a bare
    # string or a single value too.
    current = state.get("current") if isinstance(state, dict) else state
    if isinstance(current, list):
        current = current[0] if current else None
    return str(current).upper() if current else None


def is_running(run_dir: str | Path) -> bool | None:
    """Whether the run's most recent SLURM job is still active (running/queued).

    Returns True/False from sacct, or None if it can't be determined (no job id
    found, or sacct unavailable) so callers can fall back to a file-based guess.
    """
    job_id = find_job_id(run_dir)
    if job_id is None:
        return None
    state = job_state(job_id)
    if state is None:
        return None
    return any(state.startswith(s) for s in _ACTIVE_STATES)


# Terminal states that mean the job ended abnormally. Prefix-matched, since
# sacct decorates some (e.g. "CANCELLED by 12345").
_ERROR_STATES = (
    "FAILED", "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY", "NODE_FAIL",
    "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED",
)


def error_state(run_dir: str | Path) -> str | None:
    """The SLURM state if this run's job ended abnormally (e.g. ``"TIMEOUT"``).

    Returns None when the job completed normally, is still active, or its state
    cannot be determined (no job id / sacct unavailable).
    """
    job_id = find_job_id(run_dir)
    if job_id is None:
        return None
    state = job_state(job_id)
    if state is None:
        return None
    for s in _ERROR_STATES:
        if state.startswith(s):
            return s
    return None
