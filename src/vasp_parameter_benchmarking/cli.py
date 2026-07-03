"""Command-line interface for the VASP parameter-benchmarking toolkit.

Subcommands:

  vasp-parameter-benchmarking setup   - Part 1: create one dir per parameter combo.
  vasp-parameter-benchmarking submit  - Part 2: submit all jobs to SLURM.
  vasp-parameter-benchmarking report  - Part 3: collect convergence + cost results.
  vasp-parameter-benchmarking status  - re-scan folders + refresh folder_index.html.
  vasp-parameter-benchmarking clean   - delete bulky outputs, keep inputs + results.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__


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
    p_setup.add_argument("--vasp-files", default="VASP_Files", help="Directory of VASP inputs.")
    p_setup.add_argument(
        "--submit",
        help="Submit script copied unchanged into every config "
        "(default: <vasp-files>/submit.sl).",
    )
    p_setup.add_argument(
        "--root", default="VASP_Parameter_Benchmarking", help="Output root directory."
    )
    p_setup.add_argument(
        "--no-name-jobs",
        dest="name_jobs",
        action="store_false",
        help="Keep submit.sl's own --job-name. By default each job is named "
        "vasp-para-bench-<folder> (e.g. vasp-para-bench-001) so jobs are "
        "identifiable in squeue/sacct (the scheduler still assigns the job ID).",
    )

    # ---- submit ----------------------------------------------------------
    p_submit = sub.add_parser("submit", help="Part 2: submit all jobs to SLURM.")
    p_submit.add_argument(
        "--root", default="VASP_Parameter_Benchmarking", help="Benchmark root directory."
    )
    p_submit.add_argument("--dry-run", action="store_true", help="List jobs without submitting.")
    p_submit.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    p_submit.add_argument(
        "--retry-failed",
        action="store_true",
        help="Only (re)submit configs with no usable result; reset each to its "
        "inputs + submit.sl first.",
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

            setup(
                incar_flags=args.incar,
                parameters_file=args.parameters,
                mode=args.mode,
                vasp_files=args.vasp_files,
                submit=args.submit,
                root=args.root,
                name_jobs=args.name_jobs,
            )
        elif args.command == "submit":
            from .submit import submit

            submit(
                root=args.root,
                dry_run=args.dry_run,
                yes=args.yes,
                retry_failed=args.retry_failed,
            )
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
