# vasp-parameter-benchmarking

A command-line tool for answering a practical VASP question: **how high do the
calculation parameters really need to be?** Give it your VASP inputs and the
parameter values you want to test (ENCUT, SIGMA, k-point density, …); it
generates one job directory per combination, submits them to SLURM, tracks
which have completed / are running / hit an error, and collects everything into
an interactive **convergence vs cost** report — so you can pick the cheapest
settings that still give a converged result.

> **Sibling tool.** [`vasp-core-benchmarking`](https://github.com/geoffreyweal/vasp-core-benchmarking)
> benchmarks the *parallel layout* (MPI ranks × OpenMP threads) by rewriting
> `submit.sl`. This tool does the opposite: it varies only the calculation
> parameters and **leaves your `submit.sl` alone** — the only `#SBATCH`
> directives it ever sets are `--job-name` (so jobs are identifiable in
> `squeue`) and, optionally, `--mem-per-cpu` (so heavier configs can request
> more memory). Everything else about how your jobs run is exactly what you
> wrote.

## Install

```bash
pip install git+https://github.com/geoffreyweal/vasp-parameter-benchmarking.git
```

Check it installed:

```bash
vasp-parameter-benchmarking --version
```

## The workflow at a glance

| Subcommand | Purpose |
| --- | --- |
| `setup` | Generate one numbered job directory per parameter combination. |
| `submit` | Send the jobs that need running to SLURM (never re-submits finished/running/errored work). |
| `status` | Re-scan the folders and refresh the `folder_index.html` navigator. |
| `reset` | Return errored configs to their inputs so `submit` can relaunch them. |
| `report` | Collect all results into a CSV and an interactive HTML report. |
| `clean` | Delete bulky VASP outputs once you are done. |

A typical study:

```bash
# 1. put your inputs in VASP_Files/ and your sweep in the parameters file, then
vasp-parameter-benchmarking setup
vasp-parameter-benchmarking submit

# 2. while jobs run, check progress whenever you like
vasp-parameter-benchmarking status

# 3. if something hit a wall (e.g. out of memory): fix the cause, then
vasp-parameter-benchmarking reset
vasp-parameter-benchmarking submit

# 4. when the jobs are done
vasp-parameter-benchmarking report
vasp-parameter-benchmarking clean
```

## Part 1 — `setup`: generate the benchmark directories

### The inputs — `VASP_Files/`

Provide a directory of ordinary VASP inputs (default `VASP_Files/`, or point at
it with `--vasp-files`):

```text
VASP_Files/
├── INCAR      # required
├── POSCAR     # required
├── POTCAR     # required
├── KPOINTS    # keep ONE plain KPOINTS to not sweep k-points ...
├── KPOINTS_1  # ... OR provide KPOINTS_1, KPOINTS_2, ... to sweep over them
├── KPOINTS_2
├── submit.sl  # required — your SLURM script, used as-is
└── ...        # any extras (ML_FF, WAVECAR, CHGCAR, …) are copied too
```

Every file is copied into each job directory unchanged. `setup` then edits only
what defines that particular combination: it sets the swept `INCAR` tags, and
copies in the assigned `KPOINTS_<n>` as `KPOINTS`. Your base `INCAR` stays an
ordinary single-value file — the sweep is described separately.

> `POTCAR` files are distributed under the VASP licence, so provide your own.

### The sweep — `vasp_parameter_benchmarking_parameters.txt`

**INCAR tags** are swept via a plain-text parameters file (default
`vasp_parameter_benchmarking_parameters.txt`, or pass `--parameters`):

```text
# run settings
mode = grid

# what to sweep — one line per INCAR tag
INCAR ENCUT = 300, 400, 500, 600, 700
INCAR SIGMA = 0.05, 0.1, 0.2

# optional: more memory for the heavier configs
mem_per_cpu from ENCUT = 2G, 4G, 6G, 8G, 8G
```

- `INCAR <TAG> = v1, v2, ...` — sweep any INCAR tag. Nothing is hard-coded to
  specific tags, and values are written verbatim, so
  `INCAR LREAL = .FALSE., Auto` works too.
- `mode = grid | oat` — how combinations are expanded (see below); a CLI
  `--mode` overrides it.
- `mem_per_cpu from <KEY> = m1, m2, ...` — a memory table (see below).

INCAR sweeps can also be given on the command line — `--incar
"ENCUT=400,500,600"` (repeatable). CLI flags and the file are merged; the CLI
wins for a repeated tag.

**KPOINTS** is swept with **files, not lines**: put `KPOINTS_1`, `KPOINTS_2`, …
in `VASP_Files/` and `setup` sweeps over them automatically. A single plain
`KPOINTS` means k-points are not swept. Because you author the files yourself,
*any* KPOINTS format works — automatic meshes, Monkhorst-Pack, line mode,
explicit lists. Each config receives its assigned file verbatim, except that
the first line (VASP's free comment line, which VASP ignores) is tagged with
the label — e.g. `KPOINTS_2 (your original comment)` — so the report and
navigator can identify which variation each folder holds.

> **Put your most-trusted value first.** The first value of each parameter —
> and `KPOINTS_1` — is the baseline: in `oat` mode it is the centre the other
> parameters are varied around, and listing the value already in your base
> `INCAR` first keeps later additive runs lined up.
>
> **To benchmark `KSPACING`**, sweep it as an INCAR tag
> (`INCAR KSPACING = 0.1, 0.2, 0.3`) and **omit all KPOINTS files** from
> `VASP_Files/` — VASP uses `KSPACING` only when no `KPOINTS` file is present.

### `mode`: how combinations are expanded

- `grid` *(default)* — the full **Cartesian product** of every value. ENCUT (5
  values) × KPOINTS (4 files) = 20 jobs. Best when parameters interact.
- `oat` — **one-at-a-time**: one baseline job (the first value of everything),
  plus each parameter's remaining values with the rest held at baseline. The
  same sweep becomes 1 + 4 + 3 = 8 jobs. Best for independent convergence
  tests.

### The memory table — `mem_per_cpu`

Heavier configs (higher `ENCUT`, denser k-points) can need more memory. A
`mem_per_cpu` line gives one `--mem-per-cpu` value per value of a driving
parameter, matched by position:

```text
INCAR ENCUT = 300, 400, 500, 600, 700
mem_per_cpu from ENCUT   = 2G, 4G, 6G, 8G, 8G
mem_per_cpu from KPOINTS = 2G, 5G          # lines up with KPOINTS_1, KPOINTS_2
```

Sizes are `2G`, `512M`, or a bare number in MB. The table is **not** a sweep
axis — it creates no extra folders. `setup` writes the chosen value into each
config's `#SBATCH --mem-per-cpu` directive (adding one if your `submit.sl` has
none), so each folder is self-contained. When several tables apply to a config,
the **greatest** value wins.

### What `setup` produces

Each combination gets a plain **numbered** directory:

```text
VASP_Parameter_Benchmarking/     # change with --root
├── 001/  002/  003/  ...        # one complete VASP job each
├── folder_index.html            # the folder navigator (see below)
└── vasp_parameter_benchmarking_parameters.txt   # the recorded sweep
```

The number is just a label — the `INCAR`/`KPOINTS` *inside* each folder define
what it is, and that is what every later command reads back. There is no hidden
manifest. `setup` records the effective sweep (mode, tags, order, memory table)
in the root's parameters file so `report`, `status` and `reset` know the sweep
without you re-passing it.

Two `#SBATCH` directives are set in each copied `submit.sl` (everything else is
byte-for-byte yours):

- `--job-name=vasp-para-bench-<folder>` (e.g. `vasp-para-bench-001`) so jobs
  are identifiable in `squeue`/`sacct`. Opt out with `--no-name-jobs`.
- `--mem-per-cpu=<value>`, only if you gave a `mem_per_cpu` table.

### Extending a study later

`setup` is **additive and idempotent**: edit the parameters file (or add
`KPOINTS_<n>` files) and run it again. It reuses every existing folder whose
`INCAR`/`KPOINTS` match a needed combination and creates only the genuinely new
ones, with the next free numbers. Existing folders are never renamed, touched
or re-run.

```text
Study 1 (ENCUT × KPOINTS, oat) → 001 … 006
add 'INCAR SIGMA = 0.05, 0.1, 0.2' and run setup again:
  → reuses 001–006, creates only 007 (SIGMA=0.1) and 008 (SIGMA=0.2)
```

Then `submit` runs only the new folders, because it skips everything that has
already run.

## Part 2 — `submit`: send the jobs to SLURM

```bash
vasp-parameter-benchmarking submit            # shows the plan, prompts
vasp-parameter-benchmarking submit --dry-run  # shows the plan, submits nothing
vasp-parameter-benchmarking submit --yes      # no prompt
```

`submit` classifies every config first (same rules as the navigator, below) and
**only submits what needs running**:

- **pending** configs are submitted;
- **failed** configs (died without an identifiable error) are reset to their
  inputs and resubmitted;
- **run**, **running** and **error** configs are skipped — completed work is
  never re-run, running jobs are never touched, and errored configs are never
  blindly resubmitted (fix the cause, then `reset`; see below).

So re-running `submit` is always safe: a fresh tree submits everything, a
finished tree submits nothing. Jobs are `sbatch`ed with a short pause every 10
submissions to respect scheduler rate limits.

Before anything is launched, the exact plan is shown and confirmed — nothing is
ever submitted by accident:

```text
Found 6 configs under VASP_Parameter_Benchmarking/: 1 run, 0 running, 1 error (all skipped); 4 eligible.
Will submit 4 job(s):
  003  (pending)
  004  (pending)
  005  (pending)
  006  (failed - will reset first)
Submit these 4 job(s) to SLURM? [y=submit / N=abort / o=only these... / r=reject these...]
```

At the prompt, **`o`** asks for folder numbers to submit *only*, and **`r`**
asks for folder numbers to *reject*; the plan is re-shown after each edit and
you confirm again. Numbers may be comma- or space-separated, and `3` and `003`
both work. The same narrowing is available as flags:

```bash
vasp-parameter-benchmarking submit --submit-only 3,4   # only these folders
vasp-parameter-benchmarking submit --reject 5,6        # all but these
```

Both are repeatable. Neither `--submit-only` nor `o` can override the status
rules — asking for a completed/running/errored folder prints a note and skips
it, so a double submission cannot be forced.

### Recovering from errors — `reset`

Errored configs (e.g. `TIMEOUT`, out-of-memory, a VASP abort) are deliberately
not resubmitted by `submit`: rerunning them unchanged would usually hit the
same wall. Fix the cause first — raise the `mem_per_cpu` table, extend the time
limit, correct the input — then:

```bash
vasp-parameter-benchmarking reset --dry-run   # list errored configs + reasons
vasp-parameter-benchmarking reset             # reset them to their inputs
vasp-parameter-benchmarking submit            # relaunch them (now pending)
```

`reset` deletes everything in each errored config except its inputs (`INCAR`,
`KPOINTS`, `POTCAR`, `POSCAR`, `submit.sl`), returning it to **pending**, and
refreshes the navigator. All other configs are untouched.

It also re-applies each reset config's `--mem-per-cpu` from the **current**
memory table. So the out-of-memory recovery is: raise the table in your
parameters file → `setup` (records it; existing folders are not otherwise
touched) → `reset` → `submit` — and the relaunched job requests the new memory.

## Watching progress — `status` and the folder navigator

`setup` writes a self-contained **folder navigator** into the benchmark root:

```text
VASP_Parameter_Benchmarking/folder_index.html
```

Open it in a browser and pick a value for each parameter from the dropdowns; it
lists the matching folder number(s) and each one's status. Leave a parameter on
**(any)** to not constrain it — e.g. ENCUT=600 with KPOINTS on **(any)** lists
every folder at ENCUT=600. A full table of every folder, its values and its
status sits below the selectors.

### How statuses are decided

Statuses come primarily from each folder's own files — the OUTCAR above all —
so they work with or without the scheduler:

- **✓ run** — the OUTCAR ends with VASP's normal-termination timing footer
  (*"General timing and accounting informations"*) and yields a final energy.
  An energy alone is **not** enough — it appears after the first SCF loop, long
  before a job finishes — so still-running jobs are never misreported as run.
- **⏳ running** — launched and not complete, and either `sacct` says the job is
  still active, or (without `sacct`) the OUTCAR/OSZICAR was written to within
  the last 30 minutes — VASP writes at least once per electronic step.
- **✗ error (reason)** — finished with an identifiable error, shown in
  parentheses: a VASP abort message near the end of the OUTCAR (`VERY BAD
  NEWS`, `ZBRENT: fatal`, …), an abnormal SLURM terminal state (`TIMEOUT`,
  `OUT_OF_MEMORY`, `FAILED`, …), or an error line in `slurm-<id>.out` (`DUE TO
  TIME LIMIT`, `oom-kill`, …).
- **✗ failed** — launched, not complete, not running, but no specific error
  could be identified (e.g. killed without leaving a message).
- **— pending** — no sign the run has been launched yet.

### The page is a snapshot — refresh it with `status`

Pressing refresh in the browser does **nothing** on its own: a page opened from
disk (`file://`) is forbidden by the browser from re-scanning your folders, so
the statuses are frozen at the moment the file was written (a *"Status as of
…"* timestamp on the page shows how old it is). To bring it up to date,
regenerate the file, then refresh the tab:

```bash
vasp-parameter-benchmarking status
# Rewrote VASP_Parameter_Benchmarking/folder_index.html (15 folder(s): 9 run, 2 running, 1 error, 0 failed, 3 pending).
# Refresh the page in your browser to see the updated statuses.
```

`status` is quick — it only re-scans and rewrites the navigator (no CSV or
plots). `report` also refreshes it as part of collecting results. Pass
`--no-sacct` to skip scheduler queries; *running* is then inferred from recent
output-file activity alone.

## Part 3 — `report`: compare convergence vs cost

```bash
vasp-parameter-benchmarking report                 # reads VASP_Parameter_Benchmarking/
vasp-parameter-benchmarking report --no-sacct      # skip SLURM accounting queries
vasp-parameter-benchmarking report --skip-steps 10 # drop the first 10 warm-up steps
```

For every usable run this collects:

- **Final energy** — `energy(sigma->0)` from the OUTCAR (falling back to `E0`
  from OSZICAR), plus energy per atom.
- **Peak force** — the largest force on any ion in the last `TOTAL-FORCE`
  block, as an optional accuracy check.
- **Cost** — mean and std-dev of the per-electronic-step `LOOP: … real time`,
  with the first few warm-up steps dropped (`--skip-steps`, default 5).
- **SLURM utilisation** — elapsed time and peak memory via `sacct --json`
  (left blank with `--no-sacct`).

Outputs go to `report/` (change with `--out`):

- `results.csv` — every metric for every run;
- `skipped.txt` — runs that could not be parsed;
- `vasp_parameter_benchmark_results.html` — the interactive report
  (self-contained; open it anywhere).

The HTML report shows two panels — **Energy** (final total energy, eV) and
**Cost per electronic step** (s) — with controls along the top:

- an **x-axis parameter** selector: choose which swept parameter to plot
  against;
- one selector per remaining parameter: **pin it to a constant value**, or
  leave it on **All values** to plot every combination as its own
  colour-coded series.

Read the two panels together: find where the energy stops changing as the
x-axis parameter increases, then check what each step up costs.

## Optional — `clean`: reclaim disk space

```bash
vasp-parameter-benchmarking clean --dry-run   # list what would go + total size
vasp-parameter-benchmarking clean             # prompts for confirmation
vasp-parameter-benchmarking clean --yes       # no prompt
```

In every directory under `--root` this keeps the inputs (`INCAR`, `KPOINTS`,
`POTCAR`, `POSCAR`), the results (`OUTCAR`, `OSZICAR`), scripts (`*.sh`,
`*.sl`), slurm logs, and the root parameters file + `folder_index.html`. It
deletes the rest (WAVECAR, CHGCAR, vaspout.h5, vasprun.xml, ML_FF, …) and
reports the space freed.
