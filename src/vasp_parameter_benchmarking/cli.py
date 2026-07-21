"""Command-line interface for the VASP parameter-benchmarking toolkit.

Subcommands:

  vasp-parameter-benchmarking setup   - Part 1: create one dir per parameter combo.
  vasp-parameter-benchmarking submit  - Part 2: submit the configs that need running.
  vasp-parameter-benchmarking report  - Part 3: collect convergence + cost results.
  vasp-parameter-benchmarking status  - re-scan folders + refresh folder_index.html.
  vasp-parameter-benchmarking reset   - reset errored configs back to their inputs.
  vasp-parameter-benchmarking clean   - delete bulky outputs, keep inputs + results.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__


# argparse dest -> setup() keyword. Every setup flag defaults to None on the
# command line, so an omitted flag falls through to setup()'s own default.
_SETUP_OPTIONS = (
    ("incar", "incar_flags"),
    ("parameters", "parameters_file"),
    ("mode", "mode"),
    ("vasp_files", "vasp_files"),
    ("submit", "submit"),
    ("root", "root"),
    ("name_jobs", "name_jobs"),
)


def _setup_kwargs(args) -> dict:
    """Collect the setup flags that were actually given into ``setup()`` kwargs."""
    kwargs = {}
    for dest, kwarg in _SETUP_OPTIONS:
        value = getattr(args, dest)
        if value is not None:
            kwargs[kwarg] = value
    return kwargs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vasp-parameter-benchmarking",
        description=(
            "Benchmark VASP across INCAR/KPOINTS parameter values to find the "
            "cheapest parameters that still converge. The submit.sl is copied "
            "unchanged - only the swept parameters vary."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- setup -----------------------------------------------------------
    # Every setup option below defaults to None (rather than its real default) so
    # _setup_kwargs can tell "given on the command line" from "left to the
    # built-in default". The real defaults live in setup()'s signature and are
    # noted in the help text here.
    p_setup = sub.add_parser("setup", help="Part 1: create benchmarking files.")
    p_setup.add_argument(
        "--incar",
        action="append",
        metavar="TAG=v1,v2,...",
        help='Sweep an INCAR tag, e.g. "ENCUT=300,400,500,600". Repeatable for '
        "multiple tags. List the value you trust most first.",
    )
    p_setup.add_argument(
        "--parameters",
        help="Parameters file (default: vasp_parameter_benchmarking_parameters.txt "
        "if present). One spec per line: 'INCAR <TAG> = v1, v2'. Optionally "
        "'mem_per_cpu from <KEY> = m1, m2' to request more memory for heavier "
        "configs (written into each submit.sl, greatest wins). To sweep KPOINTS, "
        "put KPOINTS_1, KPOINTS_2, ... files in the VASP inputs directory "
        "(a single plain KPOINTS is copied unchanged, not swept).",
    )
    p_setup.add_argument(
        "--mode",
        choices=["grid", "oat"],
        default=None,
        help="grid = every combination of values (Cartesian product); "
        "oat = one-at-a-time from a baseline (the first value of each). "
        "Overrides 'mode' in the parameters file; defaults to grid.",
    )
    p_setup.add_argument(
        "--vasp-files", help="Directory of VASP inputs (default: VASP_Files)."
    )
    p_setup.add_argument(
        "--submit",
        help="Submit script copied unchanged into every config "
        "(default: <vasp-files>/submit.sl).",
    )
    p_setup.add_argument(
        "--root",
        help="Output root directory (default: VASP_Parameter_Benchmarking).",
    )
    p_setup.add_argument(
        "--no-name-jobs",
        dest="name_jobs",
        action="store_false",
        default=None,
        help="Keep submit.sl's own --job-name. By default each job is named "
        "vasp-para-bench-<folder> (e.g. vasp-para-bench-001) so jobs are "
        "identifiable in squeue/sacct (the scheduler still assigns the job ID).",
    )

    # ---- submit ----------------------------------------------------------
    p_submit = sub.add_parser(
        "submit",
        help="Part 2: submit the configs that need running (pending + failed; "
        "completed/running/errored are skipped).",
    )
    p_submit.add_argument(
        "--root", default="VASP_Parameter_Benchmarking", help="Benchmark root directory."
    )
    p_submit.add_argument("--dry-run", action="store_true", help="List jobs without submitting.")
    p_submit.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    p_submit.add_argument(
        "--submit-only",
        action="append",
        metavar="N1,N2,...",
        help="Submit only these folder numbers (comma-separated, repeatable; "
        "'3' and '003' both work). Status rules still apply - completed/"
        "running/errored folders are refused with a note.",
    )
    p_submit.add_argument(
        "--reject",
        action="append",
        metavar="N1,N2,...",
        help="Exclude these folder numbers from this submission "
        "(comma-separated, repeatable).",
    )

    # ---- report ----------------------------------------------------------
    p_report = sub.add_parser("report", help="Part 3: collect results into CSV + HTML.")
    p_report.add_argument(
        "--root", default="VASP_Parameter_Benchmarking", help="Benchmark root directory."
    )
    p_report.add_argument("--out", default="report", help="Report output directory.")
    p_report.add_argument(
        "--parameters",
        help="Parameters file describing the sweep (default: the one 'setup' "
        "wrote into the benchmark root).",
    )
    p_report.add_argument("--no-sacct", action="store_true", help="Skip sacct utilisation queries.")
    p_report.add_argument(
        "--skip-steps",
        type=int,
        default=5,
        help="Number of leading (warm-up) electronic steps to drop from each "
        "run's timing average (default 5).",
    )

    # ---- status ----------------------------------------------------------
    p_status = sub.add_parser(
        "status",
        help="Re-scan folders and refresh folder_index.html (run/running/error/"
        "failed/pending), printing a summary. Run this to bring the navigator "
        "up to date.",
    )
    p_status.add_argument(
        "--root", default="VASP_Parameter_Benchmarking", help="Benchmark root directory."
    )
    p_status.add_argument(
        "--no-sacct",
        action="store_true",
        help="Skip sacct queries; 'running' is then inferred from recent "
        "output-file activity instead of the scheduler.",
    )

    # ---- reset -----------------------------------------------------------
    p_reset = sub.add_parser(
        "reset",
        help="Reset errored configs back to their inputs (they become pending, "
        "so the next 'submit' relaunches them). Fix the error's cause first.",
    )
    p_reset.add_argument(
        "--root", default="VASP_Parameter_Benchmarking", help="Benchmark root directory."
    )
    p_reset.add_argument(
        "--dry-run", action="store_true", help="List errored configs without resetting."
    )
    p_reset.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")

    # ---- clean -----------------------------------------------------------
    p_clean = sub.add_parser(
        "clean",
        help="Delete unnecessary files, keeping inputs, OUTCAR/OSZICAR and slurm logs.",
    )
    p_clean.add_argument(
        "--root", default="VASP_Parameter_Benchmarking", help="Benchmark root directory."
    )
    p_clean.add_argument("--dry-run", action="store_true", help="List files without deleting.")
    p_clean.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "setup":
            from .generate import setup

            setup(**_setup_kwargs(args))
        elif args.command == "submit":
            from .submit import submit

            submit(
                root=args.root,
                dry_run=args.dry_run,
                yes=args.yes,
                submit_only=args.submit_only,
                reject=args.reject,
            )
        elif args.command == "reset":
            from .submit import reset

            reset(root=args.root, dry_run=args.dry_run, yes=args.yes)
        elif args.command == "report":
            from .report import report

            report(
                root=args.root,
                out=args.out,
                no_sacct=args.no_sacct,
                skip_steps=args.skip_steps,
                parameters_file=args.parameters,
            )
        elif args.command == "status":
            from .index import STATUS_TEXT, refresh_index

            out_path, entries = refresh_index(args.root, use_sacct=not args.no_sacct)
            counts: dict[str, int] = {}
            for e in entries:
                counts[e["status"]] = counts.get(e["status"], 0) + 1
            order = ["done", "running", "error", "failed", "pending"]
            summary = ", ".join(
                f"{counts.get(k, 0)} {STATUS_TEXT[k].split(' ', 1)[-1]}" for k in order
            )
            print(f"Rewrote {out_path} ({len(entries)} folder(s): {summary}).")
            print("Refresh the page in your browser to see the updated statuses.")
        elif args.command == "clean":
            from .clean import clean

            clean(root=args.root, dry_run=args.dry_run, yes=args.yes)
        else:  # pragma: no cover - argparse enforces a valid command
            return 1
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
