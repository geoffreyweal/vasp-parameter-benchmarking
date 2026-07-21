# vasp-parameter-benchmarking

This tool is designed to allow you to determine what the minimum value you can set for VASP parameters (like ENCUT, SIGMA, k-point density, and so on) that gives you enough accuracy to perform your calculations while minimising the computational time required to run your jobs. 

> **Sibling tool.** [`vasp-core-benchmarking`](https://github.com/geoffreyweal/vasp-core-benchmarking)
> benchmarks the parallel layout (MPI ranks × OpenMP threads) by rewriting
> `submit.sl`. 

## Install

```bash
pip install git+https://github.com/geoffreyweal/vasp-parameter-benchmarking.git
```

Check it installed:

```bash
vasp-parameter-benchmarking --version
```

## The commands

| Subcommand | Purpose |
| --- | --- |
| `setup` | Generate one numbered job directory per parameter combination. |
| `submit` | Send the jobs that need running to SLURM (never re-submits finished, running, or errored work). |
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

There are two files/folders you need to give, `VASP_Files` and `vasp_parameter_benchmarking_parameters.txt`

### `VASP_Files/` - Input files

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

### `vasp_parameter_benchmarking_parameters.txt` - What VASP parameters you want to test

INCAR tags are swept with a plain-text parameters file (default
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

- `INCAR <TAG> = v1, v2, ...` sweeps any INCAR tag. Nothing is hard-coded to
  specific tags, and values are written verbatim, so
  `INCAR LREAL = .FALSE., Auto` works too.
- `mode = grid | oat` controls how combinations are expanded (see below). A CLI
  `--mode` overrides it.
- `mem_per_cpu from <KEY> = m1, m2, ...` is a memory table (see below).

### `mode`: how combinations are expanded

- `grid` (default) — the full Cartesian product of every value. ENCUT (5 values)
  × KPOINTS (4 files) = 20 jobs. Best when parameters interact.
- `oat` — one-at-a-time: one baseline job (the first value of everything), plus
  each parameter's remaining values with the rest held at baseline. The same
  sweep becomes 1 + 4 + 3 = 8 jobs. Best for independent convergence tests.

### The memory table — `mem_per_cpu`

Heavier configs (higher `ENCUT`, denser k-points) may need more memory. A
`mem_per_cpu` line gives one `--mem-per-cpu` value per value of a driving
parameter, matched by position:

```text
INCAR ENCUT = 300, 400, 500, 600, 700
mem_per_cpu from ENCUT   = 2G, 4G, 6G, 8G, 8G
mem_per_cpu from KPOINTS = 2G, 5G          # lines up with KPOINTS_1, KPOINTS_2
```

Sizes are `2G`, `512M`, or a bare number in MB.  If several memories apply to 
a benchmark test, the highest memory value is the one past to slurm.

### What `setup` produces

Each combination gets a plain numbered directory:

```text
VASP_Parameter_Benchmarking/     # change with --root
├── 001/  002/  003/  ...        # one complete VASP job each
├── folder_index.html            # the folder navigator (see below)
└── vasp_parameter_benchmarking_parameters.txt   # the recorded sweep
```

The folder number is just a label. The `INCAR` and `KPOINTS` inside each folder
are what define it, and those are what every later command reads back.

Alongside the numbered folders, `setup` writes two files into the root:

- `folder_index.html` is the folder navigator. Open it in a browser to see every
  combination, filter by parameter value, and check each folder's status (see
  [Watching progress](#watching-progress--status-and-the-folder-navigator)).
- `vasp_parameter_benchmarking_parameters.txt` is a record of the sweep you set
  up (mode, tags, order, and memory table). `status`, `report`, and `reset` read
  it back, so you don't have to describe the sweep again.

Two `#SBATCH` directives are set in each copied `submit.sl` (everything else is
exactly yours):

- `--job-name=vasp-para-bench-<folder>` (e.g. `vasp-para-bench-001`) so jobs are
  identifiable in `squeue`/`sacct`. Opt out with `--no-name-jobs`.
- `--mem-per-cpu=<value>`, only if you gave a `mem_per_cpu` table.

### Extending a study later

`setup` is additive and idempotent: edit the parameters file (or add
`KPOINTS_<n>` files) and run it again. It reuses every existing folder whose
`INCAR`/`KPOINTS` match a needed combination and creates only the new ones, with
the next free numbers. Existing folders are never renamed, touched, or re-run.

```text
Study 1 (ENCUT × KPOINTS, oat) → 001 … 006
add 'INCAR SIGMA = 0.05, 0.1, 0.2' and run setup again:
  → reuses 001–006, creates only 007 (SIGMA=0.1) and 008 (SIGMA=0.2)
```

Then `submit` runs only the new folders, because it skips everything that has
already run.

### Options file (`options.txt`)

Instead of passing `setup`'s options on the command line, you can write them into
a plain-text `options.txt`. If an `options.txt` is present in the directory you
run from, `setup` picks it up automatically; point at a differently named file
with `--options path/to/file`. Command-line flags always override the file, so
you can keep a base `options.txt` and tweak a single run with a flag.

> This file holds `setup`'s command-line options. The sweep itself, meaning which
> INCAR tags to vary and the memory table, still lives in the parameters file
> ([`vasp_parameter_benchmarking_parameters.txt`](#the-sweep--vasp_parameter_benchmarking_parameterstxt)),
> exactly as described above.

Write one `key = value` per line, using the long option name without the leading
`--` (e.g. `vasp-files`). Blank lines and lines starting with `#` are ignored,
`-` and `_` are interchangeable in keys, and quotes around a value are optional:

```text
# options.txt — VASP parameter-benchmarking setup
mode       = oat
vasp-files = VASP_Files
root       = VASP_Parameter_Benchmarking
name-jobs  = true
```

Then run:

```bash
vasp-parameter-benchmarking setup                    # auto-loads ./options.txt
vasp-parameter-benchmarking setup --options my.txt   # use a differently named file
vasp-parameter-benchmarking setup --mode grid        # override just --mode; file supplies the rest
```

Every `setup` option is accepted: `parameters`, `mode`, `vasp-files`, `submit`,
`root`, plus two with a twist:

- `name-jobs = true | false` is the boolean behind the `--no-name-jobs` flag
  (`name-jobs = false` is the same as passing `--no-name-jobs`).
- `incar = TAG=v1,v2,...` may be repeated, one line per tag, mirroring the
  repeatable `--incar` flag (though the parameters file is usually the better
  home for the sweep).

An unknown key, a missing value, or a duplicated key is reported with its line
number, so typos are caught before any files are written.

## Part 2 — `submit`: send the jobs to SLURM

```bash
vasp-parameter-benchmarking submit            # shows the plan, prompts
vasp-parameter-benchmarking submit --dry-run  # shows the plan, submits nothing
vasp-parameter-benchmarking submit --yes      # no prompt
```

`submit` classifies every config first (same rules as the navigator, below) and
only submits what needs running:

- pending configs are submitted;
- failed configs (died without an identifiable error) are reset to their inputs
  and resubmitted;
- run, running, and error configs are skipped. Completed work is never re-run,
  running jobs are never touched, and errored configs are never blindly
  resubmitted (fix the cause, then `reset`; see below).

So re-running `submit` is always safe: a fresh tree submits everything, a
finished tree submits nothing. Jobs are `sbatch`ed with a short pause every 10
submissions to respect scheduler rate limits.

Before anything is launched, the plan is shown and confirmed, so nothing is
submitted by accident:

```text
Found 6 configs under VASP_Parameter_Benchmarking/: 1 run, 0 running, 1 error (all skipped); 4 eligible.
Will submit 4 job(s):
  003  (pending)
  004  (pending)
  005  (pending)
  006  (failed - will reset first)
Submit these 4 job(s) to SLURM? [y=submit / N=abort / o=only these... / r=reject these...]
```

At the prompt, `o` asks for folder numbers to submit only, and `r` asks for
folder numbers to reject; the plan is re-shown after each edit and you confirm
again. Numbers may be comma- or space-separated, and `3` and `003` both work. The
same narrowing is available as flags:

```bash
vasp-parameter-benchmarking submit --submit-only 3,4   # only these folders
vasp-parameter-benchmarking submit --reject 5,6        # all but these
```

Both are repeatable. Neither `--submit-only` nor `o` can override the status
rules: asking for a completed, running, or errored folder prints a note and skips
it, so a double submission cannot be forced.

### Recovering from errors — `reset`

Errored configs (e.g. `TIMEOUT`, out-of-memory, a VASP abort) are deliberately
not resubmitted by `submit`, since rerunning them unchanged would usually hit the
same wall. Fix the cause first (raise the `mem_per_cpu` table, extend the time
limit, or correct the input), then:

```bash
vasp-parameter-benchmarking reset --dry-run   # list errored configs + reasons
vasp-parameter-benchmarking reset             # reset them to their inputs
vasp-parameter-benchmarking submit            # relaunch them (now pending)
```

`reset` deletes everything in each errored config except its inputs (`INCAR`,
`KPOINTS`, `POTCAR`, `POSCAR`, `submit.sl`), returning it to pending, and
refreshes the navigator. All other configs are untouched.

It also re-applies each reset config's `--mem-per-cpu` from the current memory
table. So the out-of-memory recovery is: raise the table in your parameters file,
run `setup` (which records it without otherwise touching existing folders), then
`reset` and `submit`. The relaunched job requests the new memory.

## Watching progress — `status` and the folder navigator

`setup` writes a self-contained folder navigator into the benchmark root:

```text
VASP_Parameter_Benchmarking/folder_index.html
```

Open it in a browser and pick a value for each parameter from the dropdowns. It
lists the matching folder number(s) and each one's status. Leave a parameter on
(any) to not constrain it: ENCUT=600 with KPOINTS on (any) lists every folder at
ENCUT=600. A full table of every folder, its values, and its status sits below
the selectors.

### How statuses are decided

Statuses come mainly from each folder's own files, especially the OUTCAR, so they
work with or without the scheduler:

- **✓ run** — the OUTCAR ends with VASP's normal-termination timing footer
  (*"General timing and accounting informations"*) and yields a final energy. An
  energy alone is not enough, since it appears after the first SCF loop, long
  before a job finishes, so still-running jobs are never misreported as run.
- **⏳ running** — launched and not complete, and either `sacct` says the job is
  still active, or (without `sacct`) the OUTCAR/OSZICAR was written to within the
  last 30 minutes. VASP writes at least once per electronic step.
- **✗ error (reason)** — finished with an identifiable error, shown in
  parentheses: a VASP abort message near the end of the OUTCAR (`VERY BAD NEWS`,
  `ZBRENT: fatal`, …), an abnormal SLURM terminal state (`TIMEOUT`,
  `OUT_OF_MEMORY`, `FAILED`, …), or an error line in `slurm-<id>.out` (`DUE TO
  TIME LIMIT`, `oom-kill`, …).
- **✗ failed** — launched, not complete, not running, but no specific error could
  be identified (e.g. killed without leaving a message).
- **— pending** — no sign the run has been launched yet.

### The page is a snapshot — refresh it with `status`

Pressing refresh in the browser does nothing on its own: a page opened from disk
(`file://`) is not allowed to re-scan your folders, so the statuses are frozen at
the moment the file was written (a *"Status as of …"* timestamp on the page shows
how old it is). To bring it up to date, regenerate the file, then refresh the
tab:

```bash
vasp-parameter-benchmarking status
# Rewrote VASP_Parameter_Benchmarking/folder_index.html (15 folder(s): 9 run, 2 running, 1 error, 0 failed, 3 pending).
# Refresh the page in your browser to see the updated statuses.
```

`status` is quick: it only re-scans and rewrites the navigator (no CSV or plots).
`report` also refreshes it while collecting results. Pass `--no-sacct` to skip
scheduler queries; running is then inferred from recent output-file activity
alone.

## Part 3 — `report`: compare convergence vs cost

```bash
vasp-parameter-benchmarking report                 # reads VASP_Parameter_Benchmarking/
vasp-parameter-benchmarking report --no-sacct      # skip SLURM accounting queries
vasp-parameter-benchmarking report --skip-steps 10 # drop the first 10 warm-up steps
```

For every usable run this collects:

- Final energy — `energy(sigma->0)` from the OUTCAR (falling back to `E0` from
  OSZICAR), plus energy per atom.
- Peak force — the largest force on any ion in the last `TOTAL-FORCE` block, as
  an optional accuracy check.
- Cost — mean and std-dev of the per-electronic-step `LOOP: … real time`, with
  the first few warm-up steps dropped (`--skip-steps`, default 5).
- SLURM utilisation — elapsed time and peak memory via `sacct --json` (left blank
  with `--no-sacct`).

Outputs go to `report/` (change with `--out`):

- `results.csv` — every metric for every run;
- `skipped.txt` — runs that could not be parsed;
- `vasp_parameter_benchmark_results.html` — the interactive report
  (self-contained; open it anywhere).

The HTML report shows the Energy panel (final total energy, eV), with controls
along the top:

- an x-axis parameter selector: choose which swept parameter to plot against;
- one selector per remaining parameter: pin it to a constant value, or leave it
  on All values to plot every combination as its own colour-coded series;
- a "show cost per electronic step" tick box: selecting it adds the Cost panel
  (mean wall time per electronic step, s) beneath the energy panel; it is hidden
  by default.

Find where the energy stops changing as the x-axis parameter increases, then tick
the cost box to see what each step up costs.

## Optional — `clean`: reclaim disk space

```bash
vasp-parameter-benchmarking clean --dry-run   # list what would go + total size
vasp-parameter-benchmarking clean             # prompts for confirmation
vasp-parameter-benchmarking clean --yes       # no prompt
```

In every directory under `--root` this keeps the inputs (`INCAR`, `KPOINTS`,
`POTCAR`, `POSCAR`), the results (`OUTCAR`, `OSZICAR`), scripts (`*.sh`, `*.sl`),
slurm logs, and the root parameters file plus `folder_index.html`. It deletes the
rest (WAVECAR, CHGCAR, vaspout.h5, vasprun.xml, ML_FF, …) and reports the space
freed.
