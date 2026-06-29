"""VASP parameter benchmarking toolkit.

Three-part workflow for benchmarking VASP across INCAR / KPOINTS *parameter*
values (e.g. ENCUT, SIGMA, k-point density), to find the cheapest parameters
that still give a converged result:

  1. ``setup``  - create one directory per parameter combination, copying the
                  inputs and the (unchanged) submit.sl, and editing only the
                  swept INCAR tags / KPOINTS grid.
  2. ``submit`` - submit every generated submit.sl to SLURM.
  3. ``report`` - collect the final energy, electronic-step timing and SLURM
                  utilisation, then write a CSV and an interactive HTML report
                  comparing convergence against cost.

Plus an optional ``clean`` step that deletes the large output files, keeping
only the inputs, OUTCAR/OSZICAR, submit script and slurm logs.

Sibling tool: ``vasp-core-benchmarking`` varies the SLURM parallel layout
(ntasks x cpus-per-task) instead of the calculation parameters. This tool does
the opposite - it leaves submit.sl untouched and varies INCAR/KPOINTS.
"""

__version__ = "0.1.0"
