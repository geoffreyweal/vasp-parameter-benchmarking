# vasp-parameter-benchmarking

Sweep [VASP](https://www.vasp.at/) **INCAR / KPOINTS parameters** (ENCUT, SIGMA,
k-point density, ...) to find the cheapest values that still give a converged
result. It builds one job per parameter combination, submits them, and produces
an interactive report of **convergence vs cost**.

> **Sibling tool.** [`vasp-core-benchmarking`](https://github.com/geoffreyweal/vasp-core-benchmarking)
> benchmarks the *parallel layout* (MPI ranks × OpenMP threads) by rewriting
> `submit.sl`. This tool does the opposite: it **leaves `submit.sl` alone** and
> varies only the calculation parameters in `INCAR`/`KPOINTS`. The only `#SBATCH`
> directives it ever touches are `--job-name` (set to `vasp-para-bench-<folder>`
> so jobs are identifiable in `squeue`; opt out with `--no-name-jobs`) and the
> optional `--mem-per-cpu` (see below), so heavier runs can get more memory.

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
| `submit` | `sbatch` every generated job (submit.sl as written by `setup`). |
| `report` | Collect convergence + cost into CSV + HTML. |
| `status` | Re-scan folders and refresh `folder_index.html` (run/running/failed/pending). |
| `clean`  | Delete bulky VASP outputs once you're done. |

### Part 1 — `setup`: create the benchmarking files

Provide a `VASP_Files/` directory of inputs (or point at it with `--vasp-files`):

```text
VASP_Files/
├── INCAR      # required
├── POSCAR     # required
├── POTCAR     # required
├── KPOINTS    # keep ONE KPOINTS to not sweep it (omit if using KSPACING) ...
├── KPOINTS_1  # ... OR provide KPOINTS_1, KPOINTS_2, ... to sweep over them
├── KPOINTS_2
├── submit.sl  # required — copied into every job (verbatim, bar --job-name/--mem-per-cpu)
└── ...         # any extras (ML_FF, WAVECAR, CHGCAR, …) are copied too
```

Every file in `VASP_Files/` is copied into each benchmark directory **unchanged**,
including your `submit.sl`. The tool then edits **only** the parameters you sweep:
it sets the relevant `INCAR` tags, and each config gets its assigned
`KPOINTS_<n>` copied in as `KPOINTS`. Your base `INCAR` stays an ordinary
single-value file; the INCAR sweep lives in the parameters file, and the KPOINTS
sweep lives in the `KPOINTS_<n>` files themselves. (The only edits ever made to
`submit.sl` are the `--job-name` directive — set to `vasp-para-bench-<folder>` by
default, disable with `--no-name-jobs` — and `--mem-per-cpu`, if you add a
`mem_per_cpu` table; see below.)

> `POTCAR` files are distributed under the VASP licence, so provide your own.

#### Choosing what to sweep — `vasp_parameter_benchmarking_parameters.txt`

**INCAR tags** are swept via a parameters file (default
`vasp_parameter_benchmarking_parameters.txt`, or pass `--parameters`). One line
per swept tag, plus optional run settings (like `mode`) at the top:

```text
# run settings
mode = grid

# what to sweep — one line each
INCAR ENCUT = 300, 400, 500, 600, 700
INCAR SIGMA = 0.05, 0.1, 0.2

# optional: more memory for the heavier configs (applied by `setup`)
mem_per_cpu from ENCUT = 2G, 4G, 6G, 8G, 8G
```

**KPOINTS** is swept with **files, not lines**: put `KPOINTS_1`, `KPOINTS_2`, …
in `VASP_Files/` and `setup` sweeps over them automatically (a single plain
`KPOINTS` means it is not swept and is copied unchanged). Since you author the
files yourself, *any* KPOINTS format works — automatic mesh, Monkhorst-Pack,
line mode, explicit lists. Each config's copy is verbatim except the first
(comment) line, which is tagged with its label (e.g. `KPOINTS_2 (…)`) so the
report and navigator can identify it; VASP ignores that line.

- `INCAR <TAG> = v1, v2, ...` — sweep any INCAR tag. Add a new parameter by
  adding a line; nothing in the tool is hard-coded to specific tags. Values are
  written verbatim, so `INCAR LREAL = .FALSE., Auto` works too.
- `mem_per_cpu from <KEY> = m1, m2, ...` — request more SLURM memory for the
  heavier configs (e.g. higher `ENCUT` or a denser `KPOINTS_<n>`). Give one
  `--mem-per-cpu` value per value of the driving parameter, lined up by position
  (`2G`, `512M`, or a bare number in MB); `mem_per_cpu from KPOINTS = 6G, 2G`
  lines up with `KPOINTS_1, KPOINTS_2`. It is **not** a sweep axis — it creates
  no extra folders. `setup` writes the chosen value into each config's
  `#SBATCH --mem-per-cpu` line (adding one if your `submit.sl` has none), so each
  folder is self-contained. List several lines (e.g. one keyed to `ENCUT`, one to
  `KPOINTS`) and the **greatest** value wins for each config.
- `mode = grid | oat` — see below. A CLI `--mode` overrides it.

You can also (or instead) pass INCAR sweeps on the command line — `--incar
"ENCUT=400,500,600"` (repeatable). CLI flags and the file are merged (CLI wins
for a repeated tag).

```bash
vasp-parameter-benchmarking setup        # reads the parameters file
vasp-parameter-benchmarking setup --incar "ENCUT=400,500,600" --mode oat
```

> **List your existing/default value first.** In `oat` mode the first value of
> each parameter is the centre the others are varied around, and listing the
> value already in your base `INCAR` first keeps later additive runs lined up
> (see *Adding a parameter later*). The same goes for KPOINTS: `KPOINTS_1` is
> the baseline, so make it your default/most-trusted variation.
>
> **No separate manifest.** The generated `INCAR`/`KPOINTS` in each config dir
> *are* the record — `report` reads each config's actual values straight from
> those files. `setup` also drops the effective sweep (mode + parameters) into
> `<root>/vasp_parameter_benchmarking_parameters.txt` so `report` knows which
> tags were swept, in what order.

#### `mode`: how combinations are expanded

- `grid` *(default)* — the full **Cartesian product** of every value. With ENCUT
  (5) × KPOINTS (4) that is 20 jobs. Best when parameters interact.
- `oat` — **one-at-a-time**: a baseline (the first value of each parameter) plus,
  for each parameter, its remaining values with the rest held at baseline. The
  same ENCUT × KPOINTS sweep becomes 1 + 4 + 3 = 8 jobs. Best for independent
  convergence tests.

#### Numbered folders + the folder navigator

Each job lands in a plain **numbered** directory — `VASP_Parameter_Benchmarking/001/`,
`002/`, … The number is just a label; the `INCAR`/`KPOINTS` *inside* each folder
define what it is, and that is what `report` reads. To find which folder holds a
given variation, open the **folder navigator** that `setup` writes:

```text
VASP_Parameter_Benchmarking/folder_index.html
```

Open it in a browser and pick a value for each parameter from the dropdowns; it
lists the matching folder number(s) and each one's status. Leave any parameter
on **(any)** to not constrain it — e.g. ENCUT=600 with KPOINTS on **(any)**
lists every folder at ENCUT=600. A full table of every folder and its values is
shown below the selectors.

The statuses come primarily from each folder's own files (the OUTCAR above all):

- **✓ run** — the OUTCAR ends with VASP's normal-termination timing footer
  (*"General timing and accounting informations"*) and yields a final energy.
  An energy alone is **not** enough — it appears after the first SCF loop, long
  before a job finishes — so still-running jobs are not misreported as run.
- **⏳ running** — launched and not complete, and either SLURM (`sacct`) says the
  job is still active, or — with `--no-sacct` / no scheduler — the OUTCAR/OSZICAR
  was written to within the last 30 minutes (VASP writes at least once per
  electronic step).
- **✗ error (…)** — finished with an identifiable error, shown in parentheses:
  a VASP abort message near the end of the OUTCAR (e.g. `VERY BAD NEWS`,
  `ZBRENT: fatal`), an abnormal SLURM terminal state (`TIMEOUT`,
  `OUT_OF_MEMORY`, `FAILED`, …), or an error line in `slurm-<id>.out`
  (e.g. `DUE TO TIME LIMIT`).
- **✗ failed** — launched, not complete, not running, but no specific error
  could be identified (e.g. killed without leaving a message).
- **— pending** — no sign the run has been launched yet.

**The page is a snapshot, not live — pressing refresh in your browser does
nothing.** When you open `folder_index.html` from disk (a `file://` page), the
browser security model forbids that page from re-scanning your folders or
querying SLURM. The statuses are baked into the file when it is written, so
reloading the tab just re-loads the same frozen data. The page shows a
*"Status as of …"* timestamp so you can see how old it is.

To bring it up to date you must **regenerate the file first, then refresh the
tab**:

```bash
vasp-parameter-benchmarking status   # re-scan folders + rewrite folder_index.html
# then hit refresh in the browser — now it reflects what has run
```

This is quick — it only re-scans and rewrites the navigator (no CSV/plots).
`report` also refreshes it as part of collecting results. `status` uses `sacct`
to confirm whether a job is still active; pass `--no-sacct` to skip that —
*running* is then inferred from recent OUTCAR/OSZICAR write activity, so the
classification works from the folder contents alone.

**Want a truly live view where refresh alone updates it?** That requires serving
the page from a small local process (so each refresh re-scans) rather than opening
it from disk — it is not possible for a plain `file://` page. Not currently built
in; ask if you'd like a `browse`/server command added.

> To benchmark `KSPACING`, sweep it as an INCAR tag
> (`INCAR KSPACING = 0.1, 0.2, 0.3`) and **omit all KPOINTS files**
> (`KPOINTS` and `KPOINTS_<n>`) from `VASP_Files/` — VASP uses `KSPACING` only
> when no `KPOINTS` file is present.

#### Adding a parameter later (incremental studies)

`setup` is **additive and idempotent**. To extend a study — add a parameter, or
more values to an existing one — just edit the parameters file and run `setup`
again. It works out the combinations the new sweep needs, **reuses every folder
that already exists** (matched by the values in their `INCAR`/`KPOINTS`), and
creates only the genuinely new ones with the next free numbers. Existing folders
are never renamed, touched or re-run, so completed jobs are preserved.

```text
Study 1 (ENCUT × KPOINTS, oat) → 001 … 006
add 'INCAR SIGMA = 0.05, 0.1, 0.2' to the parameters file, run setup again:
  → reuses 001–006, creates only 007 (SIGMA=0.1) and 008 (SIGMA=0.2)
```

> For the reuse to line up, **list each parameter's existing/default value (the
> one already in your base `INCAR`/`KPOINTS`) first** — your earlier runs hold the
> new parameter at its base value, so they match the combinations that keep it
> there and aren't duplicated. Then `submit` (or `submit --retry-failed`) only
> runs the new folders.

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
every 10 submissions to avoid scheduler rate limits. Any per-config
`--mem-per-cpu` was already written into each `submit.sl` at `setup` time (if you
gave a `mem_per_cpu` table), so submission needs no special flags.

#### Retrying failed jobs

A config needs retrying if it did not complete successfully — its `OUTCAR` does
not end with VASP's normal-termination timing footer (the same test the folder
navigator's **✓ run** uses). To reset and resubmit just those:

```bash
vasp-parameter-benchmarking submit --retry-failed --dry-run  # list which
vasp-parameter-benchmarking submit --retry-failed            # reset + resubmit
```

This retries every config whose status is *error*, *failed* or *pending*. For
each one it resets the directory to its inputs (`INCAR`, `KPOINTS`, `POTCAR`,
`POSCAR`, `submit.sl`) and resubmits. Completed configs are left untouched, and
**still-running jobs are left alone** (never reset out from under the
scheduler).

### Part 3 — `report`: compare convergence vs cost

```bash
vasp-parameter-benchmarking report                 # reads VASP_Parameter_Benchmarking/
vasp-parameter-benchmarking report --no-sacct      # skip SLURM accounting queries
vasp-parameter-benchmarking report --skip-steps 10 # drop the first 10 warm-up steps
```

The sweep (which tags, their order, the mode) is read from the parameters file
`setup` wrote into `--root` — or pass your own with `--parameters`. For each
completed run this collects:

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
`vasp_parameter_benchmark_results.html`. It also refreshes the root
`folder_index.html` so each folder's run/pending status is up to date.

The HTML shows results against each swept parameter, selectable from a dropdown,
in two panels:

- **Energy per atom** vs the parameter value.
- **Cost** — mean wall time per electronic step vs the parameter value.

When more than one parameter is swept, the remaining parameters split the points
into coloured series (shown in the legend), so every config is plotted without
assuming any reference point. Read the two panels together: find where the energy
stops changing and see what each value costs.

### Optional — `clean`: reclaim disk space

```bash
vasp-parameter-benchmarking clean --dry-run   # list what would go + total size
vasp-parameter-benchmarking clean             # prompts for confirmation
vasp-parameter-benchmarking clean --yes       # no prompt
```

In every directory under `--root` this keeps `INCAR`, `KPOINTS`, `POTCAR`,
`POSCAR`, `OUTCAR`, `OSZICAR`, scripts (`*.sh`, `*.sl`), slurm logs and the root
parameters file + `folder_index.html`; deletes the rest (WAVECAR, CHGCAR,
vaspout.h5, vasprun.xml, ML_FF, …) and reports the space freed.
