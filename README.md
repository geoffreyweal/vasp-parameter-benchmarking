# vasp-parameter-benchmarking

Sweep [VASP](https://www.vasp.at/) **INCAR / KPOINTS parameters** (ENCUT, SIGMA,
k-point density, ...) to find the cheapest values that still give a converged
result. It builds one job per parameter combination, submits them, and produces
an interactive report of **convergence vs cost**.

> **Sibling tool.** [`vasp-core-benchmarking`](https://github.com/geoffreyweal/vasp-core-benchmarking)
> benchmarks the *parallel layout* (MPI ranks × OpenMP threads) by rewriting
> `submit.sl`. This tool does the opposite: it **never touches `submit.sl`** and
> varies only the calculation parameters in `INCAR`/`KPOINTS`.

## Install

```bash
pip install git+https://github.com/geoffreyweal/vasp-parameter-benchmarking.git
```

Check it installed with:

```bash
vasp-parameter-benchmarking --version
```

## Workflow

The tool runs in three parts, plus an optional cleanup step.

| Subcommand | Purpose |
| --- | --- |
| `setup`  | Generate one benchmark directory per parameter combination. |
| `submit` | `sbatch` every generated job (submit.sl copied unchanged). |
| `report` | Collect convergence + cost into CSV + HTML. |
| `clean`  | Delete bulky VASP outputs once you're done. |

### Part 1 — `setup`: create the benchmarking files

Provide a `VASP_Files/` directory of inputs (or point at it with `--vasp-files`):

```text
VASP_Files/
├── INCAR      # required
├── POSCAR     # required
├── POTCAR     # required
├── KPOINTS    # required (unless you only sweep INCAR with KSPACING)
├── submit.sl  # required — copied UNCHANGED into every job
└── ...         # any extras (ML_FF, WAVECAR, CHGCAR, …) are copied too
```

Every file in `VASP_Files/` is copied into each benchmark directory **unchanged**,
including your `submit.sl`. The tool then edits **only** the parameters you sweep:
it sets the relevant `INCAR` tags and, if you sweep the k-point grid, writes a
fresh `KPOINTS` file.

> `POTCAR` files are distributed under the VASP licence, so provide your own.

#### Choosing what to sweep

Specify the sweep with `--incar` (repeatable) and/or `--kpoints`:

```bash
vasp-parameter-benchmarking setup \
  --incar "ENCUT=400,500,600,700,800" \
  --kpoints "2x2x2,4x4x4,6x6x6,8x8x8" \
  --mode oat
```

- `--incar "TAG=v1,v2,..."` — sweep an INCAR tag. Repeat the flag for more tags
  (e.g. `--incar "ENCUT=..."` `--incar "SIGMA=0.05,0.1,0.2"`). Values are written
  verbatim, so `LREAL=.FALSE.,Auto` works too.
- `--kpoints "g1,g2,..."` — sweep the k-point grid, each grid written `n1xn2xn3`.
  Grids become Gamma-centred `KPOINTS` files (`--kpoints-style monkhorst` to
  switch). Pick the centring with `--kpoints-style`.

> **List the value you trust most first.** The first value of each sweep is the
> *baseline*: when the report plots one parameter, it holds the others at their
> baseline. The per-parameter reference for convergence is instead the
> highest-fidelity value (largest ENCUT, densest grid).

Alternatively, put the sweep in a **parameters file** (default
`vasp_parameter_benchmarking_parameters.txt`, or pass `--parameters`):

```text
# one spec per line; '#' starts a comment
INCAR ENCUT = 400, 500, 600, 700, 800
INCAR SIGMA = 0.05, 0.1, 0.2
KPOINTS      = 2x2x2, 4x4x4, 6x6x6, 8x8x8
```

CLI flags and the file are merged (CLI wins for a repeated tag).

#### `--mode`: how combinations are expanded

- `grid` *(default)* — the full **Cartesian product** of every value. With ENCUT
  (5) × KPOINTS (4) that is 20 jobs. Best when parameters interact.
- `oat` — **one-at-a-time**: a baseline (the first value of each parameter) plus,
  for each parameter, its remaining values with the rest held at baseline. The
  same ENCUT × KPOINTS sweep becomes 1 + 4 + 3 = 8 jobs. Best for independent
  convergence tests.

Each job lands in `VASP_Parameter_Benchmarking/<tokens>/`, e.g.
`ENCUT-600_KPOINTS-4x4x4/`, with a `parameters.json` recording its exact values.
A `benchmark_manifest.json` at the root records the whole sweep.

> To benchmark `KSPACING`, sweep it as an INCAR tag
> (`--incar "KSPACING=0.1,0.2,0.3"`) and **omit the KPOINTS file** from
> `VASP_Files/` — VASP uses `KSPACING` only when no `KPOINTS` file is present.

##### Other options

`--vasp-files` (default `VASP_Files`) points at the inputs; `--submit` overrides
the submit script (default `<vasp-files>/submit.sl`); `--root` (default
`VASP_Parameter_Benchmarking`) sets the output directory.

### Part 2 — `submit`: send the jobs to SLURM

```bash
vasp-parameter-benchmarking submit            # prompts for confirmation
vasp-parameter-benchmarking submit --dry-run  # list what would be submitted
vasp-parameter-benchmarking submit --yes      # no prompt
```

Finds every `submit.sl` under `--root` and `sbatch`es it as-is, pausing briefly
every 10 submissions to avoid scheduler rate limits.

#### Retrying failed jobs

A job is "failed" if it produced no usable result — an `OUTCAR` with no readable
final `energy(sigma->0)`. To reset and resubmit just those:

```bash
vasp-parameter-benchmarking submit --retry-failed --dry-run  # list which
vasp-parameter-benchmarking submit --retry-failed            # reset + resubmit
```

For each failed config this resets the directory to its inputs (`INCAR`,
`KPOINTS`, `POTCAR`, `POSCAR`, `submit.sl`, `parameters.json`) and resubmits.
Configs that already have a result are left untouched.

### Part 3 — `report`: compare convergence vs cost

```bash
vasp-parameter-benchmarking report                 # reads VASP_Parameter_Benchmarking/
vasp-parameter-benchmarking report --no-sacct      # skip SLURM accounting queries
vasp-parameter-benchmarking report --skip-steps 10 # drop the first 10 warm-up steps
```

For each completed run this collects:

- **Final energy** — `energy(sigma->0)` from `OUTCAR` (falling back to `E0` from
  `OSZICAR`), plus energy per atom.
- **Peak force** — the largest force on any ion in the last `TOTAL-FORCE` block,
  as an optional accuracy check.
- **Cost** — mean & std-dev of the per-electronic-step `LOOP: … real time`. The
  first few warm-up steps are dropped (`--skip-steps`, default 5).
- **SLURM utilisation** — elapsed time and peak memory via `sacct --json` (left
  blank with `--no-sacct`).

Outputs go to `report/` (change with `--out`): `results.csv` (all metrics),
`skipped.txt` (unusable runs), and a self-contained
`vasp_parameter_benchmark_results.html`.

The HTML answers *how high do I need to push this parameter?* For each swept
parameter — selectable from a dropdown, with the others held at baseline — it
shows two panels:

- **Convergence** — |E − E_ref| in **meV/atom** against the highest-fidelity
  value of that parameter (largest ENCUT, densest grid). A dotted line marks a
  1 meV/atom guide.
- **Cost** — mean wall time per electronic step.

Read the two together: pick the smallest parameter value whose convergence error
is below your tolerance, and see what it costs.

### Optional — `clean`: reclaim disk space

```bash
vasp-parameter-benchmarking clean --dry-run   # list what would go + total size
vasp-parameter-benchmarking clean             # prompts for confirmation
vasp-parameter-benchmarking clean --yes       # no prompt
```

In every directory under `--root` this keeps `INCAR`, `KPOINTS`, `POTCAR`,
`POSCAR`, `OUTCAR`, `OSZICAR`, `parameters.json`, scripts (`*.sh`, `*.sl`),
slurm logs and the root `benchmark_manifest.json`; deletes the rest (WAVECAR,
CHGCAR, vaspout.h5, vasprun.xml, ML_FF, …) and reports the space freed.
