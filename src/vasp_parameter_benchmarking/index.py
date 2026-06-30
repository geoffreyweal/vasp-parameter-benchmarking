"""The folder navigator: map parameter values <-> numbered config directory.

Config directories are named only by number (``001``, ``002``, ...); the
``INCAR``/``KPOINTS`` inside each directory define what it is. This module reads
those files back to learn each folder's swept-parameter values, and builds a
self-contained HTML page where you pick a value per parameter and it tells you
which numbered folder holds that variation (and whether it has run, is still
running, failed, or is pending).

The same value-reading/matching helpers drive ``setup``'s additive behaviour:
re-running after adding a parameter reuses the folders that already exist and
only creates the genuinely new ones.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from . import incar as incar_mod
from . import kpoints as kpoints_mod
from . import sacct
from .outcar import final_energy
from .parameters import INCAR, KPOINTS, ParamSpec

INDEX_FILENAME = "folder_index.html"

# Per-status display text and CSS class, shared by the table and the result chips.
STATUS_TEXT = {
    "done": "✓ run",
    "running": "⏳ running",
    "failed": "✗ failed",
    "pending": "— pending",
}


def read_assignment(run_dir: Path, specs: list[ParamSpec]) -> dict[str, str | None]:
    """Read each swept parameter's actual value from this folder's input files."""
    values: dict[str, str | None] = {}
    for s in specs:
        if s.target == INCAR:
            values[s.key] = incar_mod.read_tag(run_dir / "INCAR", s.key)
        elif s.target == KPOINTS:
            values[s.key] = kpoints_mod.read_grid(run_dir / "KPOINTS")
    return values


def values_match(spec: ParamSpec, a: str | None, b: str | None) -> bool:
    """Whether two values of ``spec`` are the same point.

    INCAR values compare numerically when both parse as floats (so ``0.05`` and
    ``0.050`` match), otherwise as trimmed strings; KPOINTS grids compare as
    trimmed strings.
    """
    if a is None or b is None:
        return a == b
    if spec.target == INCAR:
        try:
            return float(a) == float(b)
        except ValueError:
            return a.strip() == b.strip()
    return a.strip() == b.strip()


def assignment_matches(
    desired: dict[str, str], actual: dict[str, str | None], specs: list[ParamSpec]
) -> bool:
    """True if every swept parameter agrees between two assignments."""
    return all(values_match(s, desired.get(s.key), actual.get(s.key)) for s in specs)


def config_dirs(root_dir: Path) -> list[Path]:
    """Numbered config directories under ``root`` (immediate subdirs with an INCAR)."""
    dirs = [
        p
        for p in root_dir.iterdir()
        if p.is_dir() and p.name.isdigit() and (p / "INCAR").is_file()
    ]
    return sorted(dirs, key=lambda p: int(p.name))


def run_status(run_dir: Path, use_sacct: bool = True) -> str:
    """Classify a config folder as ``"done"``, ``"running"``, ``"failed"`` or ``"pending"``.

    * **done** - the OUTCAR holds a final ``energy(sigma->0)`` (a usable result);
    * **running** - launched with no result yet, and its SLURM job is still
      active (running or queued) according to ``sacct``;
    * **failed** - launched (an OUTCAR, OSZICAR or ``slurm-<id>.out`` is present)
      but produced no final energy and is not still running: it crashed, was
      killed, or timed out;
    * **pending** - no sign the run has been launched yet (only input files).

    When ``use_sacct`` is False (or ``sacct`` is unavailable / the job is no
    longer known to it) a launched run with no result is reported as failed -
    running and failed cannot be told apart without the scheduler.
    """
    outcar = run_dir / "OUTCAR"
    if outcar.is_file() and final_energy(outcar) is not None:
        return "done"
    started = (
        outcar.is_file()
        or (run_dir / "OSZICAR").is_file()
        or sacct.find_job_id(run_dir) is not None
    )
    if not started:
        return "pending"
    if use_sacct and sacct.is_running(run_dir):
        return "running"
    return "failed"


def scan_configs(
    root_dir: Path, specs: list[ParamSpec], use_sacct: bool = True
) -> list[dict]:
    """Read every numbered folder into ``{folder, params, status, has_result}``."""
    entries: list[dict] = []
    for d in config_dirs(root_dir):
        params = read_assignment(d, specs)
        status = run_status(d, use_sacct=use_sacct)
        entries.append(
            {
                "folder": d.name,
                "params": params,
                "status": status,
                "has_result": status == "done",
            }
        )
    return entries


def build_index_html(entries: list[dict], specs: list[ParamSpec]) -> str:
    """Build the self-contained navigator HTML (dropdowns -> folder number)."""
    keys = [s.key for s in specs]
    kinds = {s.key: s.target for s in specs}
    # Dropdown options per key, in the spec's declared order.
    options = {s.key: list(s.values) for s in specs}

    data_json = json.dumps(entries)
    keys_json = json.dumps(keys)
    kinds_json = json.dumps(kinds)

    def esc(x: str) -> str:
        return html.escape(str(x), quote=True)

    selectors = []
    for s in specs:
        label = "KPOINTS grid" if s.target == KPOINTS else f"INCAR {s.key}"
        # An "(any)" choice (selected by default) leaves that parameter unconstrained.
        opts = '<option value="__any__" selected>(any)</option>'
        opts += "".join(f'<option value="{esc(v)}">{esc(v)}</option>' for v in options[s.key])
        selectors.append(
            f'<label class="sel"><span>{esc(label)}</span>'
            f'<select id="sel__{esc(s.key)}" onchange="lookup()">{opts}</select></label>'
        )
    selectors_html = "\n".join(selectors)

    # Reference table of every folder.
    head_cells = "".join(f"<th>{esc(k)}</th>" for k in keys)
    rows = []
    for e in entries:
        cells = "".join(f"<td>{esc(e['params'].get(k))}</td>" for k in keys)
        status_cls = e["status"]
        status = STATUS_TEXT[status_cls]
        rows.append(
            f'<tr><td class="num">{esc(e["folder"])}</td>{cells}'
            f'<td class="{status_cls}">{status}</td></tr>'
        )
    table_rows = "\n".join(rows)
    status_text_json = json.dumps(STATUS_TEXT)

    return f"""<style>
  :root {{ --fg:#222; --muted:#667; --accent:#2c7fb8; --line:#e2e4e8; }}
  body {{ font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; color: var(--fg);
          margin: 0; padding: 28px 32px; background: #fafafb; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  p.lead {{ color: var(--muted); margin: 0 0 22px; }}
  .panel {{ background: #fff; border: 1px solid var(--line); border-radius: 10px;
            padding: 20px; margin-bottom: 22px; }}
  .selectors {{ display: flex; flex-wrap: wrap; gap: 16px; }}
  label.sel {{ display: flex; flex-direction: column; font-size: 12px; color: var(--muted); }}
  label.sel span {{ margin-bottom: 4px; font-weight: 600; }}
  select {{ font-size: 14px; padding: 6px 8px; border: 1px solid #ccd; border-radius: 6px;
            background: #fff; min-width: 120px; }}
  #result {{ margin-top: 18px; font-size: 15px; }}
  #result .count {{ color: var(--muted); margin-bottom: 8px; }}
  #result .none {{ color: #b00; font-weight: 600; }}
  .chips {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .chip {{ display: inline-flex; align-items: baseline; gap: 6px; padding: 6px 12px;
           border-radius: 8px; border: 1px solid var(--line); background: #f7f9fb; }}
  .chip .folder {{ font-size: 18px; font-weight: 700; color: var(--accent);
                   font-variant-numeric: tabular-nums; }}
  .chip .st {{ font-size: 11px; }}
  .status-done {{ color: #2ca25f; font-weight: 600; }}
  .status-running {{ color: #2c7fb8; font-weight: 600; }}
  .status-failed {{ color: #c0392b; font-weight: 600; }}
  .status-pending {{ color: #d08000; font-weight: 600; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); }}
  th {{ color: var(--muted); font-weight: 600; }}
  td.num {{ font-variant-numeric: tabular-nums; font-weight: 700; color: var(--accent); }}
  td.done {{ color: #2ca25f; }} td.running {{ color: #2c7fb8; }}
  td.failed {{ color: #c0392b; }} td.pending {{ color: #999; }}
  .wrap {{ overflow-x: auto; }}
</style>

<h1>VASP parameter benchmarking — folder navigator</h1>
<p class="lead">Pick a value for each parameter to find the numbered folder(s) that hold that variation.
Leave a parameter on <b>(any)</b> to not constrain it. The folder's own INCAR/KPOINTS are the
definition; folder numbers are just labels.</p>

<div class="panel">
  <div class="selectors">
{selectors_html}
  </div>
  <div id="result"></div>
</div>

<div class="panel wrap">
  <table>
    <thead><tr><th>Folder</th>{head_cells}<th>Status</th></tr></thead>
    <tbody>
{table_rows}
    </tbody>
  </table>
</div>

<script>
const DATA = {data_json};
const KEYS = {keys_json};
const KINDS = {kinds_json};
const STATUS_TEXT = {status_text_json};

function valuesMatch(kind, a, b) {{
  if (a == null || b == null) return a === b;
  if (kind === "INCAR") {{
    const fa = parseFloat(a), fb = parseFloat(b);
    if (!Number.isNaN(fa) && !Number.isNaN(fb)) return fa === fb;
  }}
  return String(a).trim() === String(b).trim();
}}

function lookup() {{
  const sel = {{}};
  for (const k of KEYS) sel[k] = document.getElementById("sel__" + k).value;
  // "__any__" leaves a parameter unconstrained.
  const hits = DATA.filter(e =>
    KEYS.every(k => sel[k] === "__any__" || valuesMatch(KINDS[k], e.params[k], sel[k])));
  const out = document.getElementById("result");
  if (hits.length === 0) {{
    out.innerHTML = '<span class="none">No folder matches.</span> ' +
      'This combination is not part of the current sweep — run <code>setup</code> again to add it.';
    return;
  }}
  const chips = hits.map(h => {{
    const st = '<span class="st status-' + h.status + '">' + STATUS_TEXT[h.status] + '</span>';
    return '<span class="chip"><span class="folder">' + h.folder + '</span>' + st + '</span>';
  }}).join("");
  const noun = hits.length === 1 ? "folder matches" : "folders match";
  out.innerHTML = '<div class="count">' + hits.length + ' ' + noun + '</div>' +
    '<div class="chips">' + chips + '</div>';
}}

lookup();
</script>
"""


def write_index(
    root_dir: Path, specs: list[ParamSpec], use_sacct: bool = True
) -> Path:
    """Scan ``root`` and (re)write the navigator HTML. Returns its path.

    ``use_sacct`` lets the navigator query SLURM to tell a still-running job
    apart from a failed one; set it False to rely only on local files.
    """
    entries = scan_configs(root_dir, specs, use_sacct=use_sacct)
    out_path = root_dir / INDEX_FILENAME
    out_path.write_text(build_index_html(entries, specs))
    return out_path
