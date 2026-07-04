"""Part 3: collect parameter-benchmark results and write a CSV + HTML report.

For every config directory under ``--root`` with a usable OUTCAR this records:

  * the swept parameter values (from ``parameters.json``);
  * the final energy ``energy(sigma->0)`` and energy per atom;
  * the peak force on any ion (an optional accuracy check);
  * the mean & std-dev per-electronic-step wall time (cost), with the first
    ``--skip-steps`` warm-up steps dropped;
  * SLURM utilisation (elapsed, CPU utilisation, peak memory) unless
    ``--no-sacct``.

The HTML report answers the practical question - *how high do I need to push
this parameter?* It is interactive: you choose which swept parameter goes on
the x-axis, and you may pin each of the *other* swept parameters to a constant
value (or leave it on "All values" to plot every combination as a colour-coded
series). For the current selection it shows:

  * **Energy** - the final total energy ``energy(sigma->0)`` (eV);
  * **Cost** - mean wall time per electronic step.

Controls at the top of the page drive both panels.
"""

from __future__ import annotations

import statistics
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from . import index as index_mod
from . import sacct
from .outcar import (
    final_energy,
    max_force,
    n_ions,
    oszicar_final_e0,
    parse_loop_times,
)
from .parameters import (
    KPOINTS,
    ParamSpec,
    numeric_value,
    parse_parameters_file,
)

PARAMETERS_FILENAME = "vasp_parameter_benchmarking_parameters.txt"

FONT_FAMILY = "Helvetica Neue, Helvetica, Arial, sans-serif"
# A qualitative palette for the per-series lines (one per combination of the
# other parameters).
PALETTE = [
    "#2c7fb8", "#e6550d", "#2ca25f", "#756bb1", "#d6616b",
    "#8c6d31", "#3182bd", "#e6ab02", "#66a61e", "#a6761d",
]


def load_specs(root_dir: Path, parameters_file: str | None = None) -> tuple[list[ParamSpec], dict]:
    """Load the swept specs + settings written by ``setup`` (or ``--parameters``)."""
    path = Path(parameters_file) if parameters_file else root_dir / PARAMETERS_FILENAME
    if not path.is_file():
        raise FileNotFoundError(
            f"parameters file not found: {path}\n"
            "Expected the one written by 'setup' in the benchmark root, or pass --parameters."
        )
    specs, settings, _mem_specs = parse_parameters_file(path)
    return specs, settings


def collect_run(
    run_dir: Path, specs: list[ParamSpec], use_sacct: bool, skip_steps: int = 5
) -> dict | None:
    """Build a result row for one config directory, or None if it is unusable.

    A run is usable if its OUTCAR yields a final energy. The first ``skip_steps``
    electronic steps are dropped from the timing average (warm-up overhead); if
    fewer steps remain the timing is reported as NaN but the energy is still kept.
    """
    outcar = run_dir / "OUTCAR"
    energy = final_energy(outcar) if outcar.is_file() else None
    if energy is None:
        energy = oszicar_final_e0(run_dir / "OSZICAR")
    if energy is None:
        return None

    nions = n_ions(outcar) if outcar.is_file() else None
    fmax = max_force(outcar) if outcar.is_file() else None

    loops = parse_loop_times(outcar) if outcar.is_file() else []
    steady = loops[skip_steps:] if len(loops) > skip_steps else []
    loop_mean = statistics.fmean(steady) if steady else None
    loop_std = statistics.pstdev(steady) if len(steady) > 1 else (0.0 if steady else None)

    row: dict = {
        "config": run_dir.name,
        "energy_eV": energy,
        "n_atoms": nions,
        "energy_per_atom_eV": (energy / nions) if nions else None,
        "max_force_eV_per_A": fmax,
        "n_electronic_steps": len(loops),
        "loop_real_mean_s": loop_mean,
        "loop_real_std_s": loop_std,
        "elapsed_s": None,
        "cpu_utilisation_pct": None,
        "max_memory_utilisation_gb": None,
        "job_id": None,
    }

    # Swept parameter values, read back from this config's own INCAR/KPOINTS.
    by_key = {s.key: s for s in specs}
    for key, value in index_mod.read_assignment(run_dir, specs).items():
        row[f"param_{key}"] = value
        row[f"param_{key}__num"] = (
            numeric_value(by_key[key], value) if value is not None else None
        )

    if use_sacct:
        row["job_id"] = sacct.find_job_id(run_dir)
        util = sacct.get_utilisation(run_dir)
        if util is not None:
            elapsed, total_cpu, max_rss_gb = util
            row["elapsed_s"] = elapsed
            row["max_memory_utilisation_gb"] = max_rss_gb
            ncores = None  # cores are not swept here; leave utilisation % blank
            if elapsed > 0 and ncores:
                row["cpu_utilisation_pct"] = total_cpu / (elapsed * ncores) * 100.0

    return row


def _figure_payload(df: pd.DataFrame, specs: list[ParamSpec]) -> dict:
    """Build the JSON payload the in-page JavaScript uses to draw the plots.

    The payload carries one record per usable run (its swept-parameter values,
    total energy and per-step cost) plus per-parameter metadata (display title,
    the ordered list of swept values, and whether the values are numeric). The
    browser uses this to redraw both panels as the controls change, so no
    server is needed.
    """
    params = []
    for spec in specs:
        key = spec.key
        numcol = f"param_{key}__num"
        numeric = numcol in df.columns and df[numcol].notna().all() and not df.empty
        params.append(
            {
                "key": key,
                "isKpoints": spec.target == KPOINTS,
                "title": "KPOINTS file" if spec.target == KPOINTS else key,
                "values": [str(v) for v in spec.values],
                "numeric": bool(numeric),
            }
        )

    def _num(value):
        try:
            return None if pd.isna(value) else float(value)
        except (TypeError, ValueError):
            return None

    records = []
    for _, r in df.iterrows():
        vals, nums = {}, {}
        for spec in specs:
            key = spec.key
            v = r.get(f"param_{key}")
            vals[key] = None if pd.isna(v) else str(v)
            nums[key] = _num(r.get(f"param_{key}__num"))
        records.append(
            {
                "config": str(r["config"]),
                "energy": _num(r.get("energy_eV")),
                "cost": _num(r.get("loop_real_mean_s")),
                "vals": vals,
                "nums": nums,
            }
        )

    return {"params": params, "records": records, "palette": PALETTE}


# Browser-side application: builds the controls and redraws both panels with
# Plotly whenever the x-axis parameter or any "constant" selector changes. Kept
# as a plain string (not an f-string) so its braces are not Python format chars.
_REPORT_JS = r"""
const P = window.__REPORT_PAYLOAD__;
const byKey = {};
P.params.forEach(p => { byKey[p.key] = p; });
const ALL = "__all__";

function el(tag, attrs, children) {
  const e = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([k, v]) => {
    if (k === "class") e.className = v; else e.setAttribute(k, v);
  });
  (children || []).forEach(c => e.appendChild(
    typeof c === "string" ? document.createTextNode(c) : c));
  return e;
}

function makeSelect(id, options) {
  const s = el("select", { id: id, class: "vpb-select" });
  options.forEach(o => {
    const opt = el("option", { value: o.value }, [o.label]);
    s.appendChild(opt);
  });
  return s;
}

function buildControls() {
  const bar = document.getElementById("controls");

  // x-axis selector
  const xField = el("div", { class: "vpb-field" }, [
    el("label", { for: "xsel" }, ["x-axis parameter"]),
    makeSelect("xsel", P.params.map(p => ({ value: p.key, label: p.key }))),
  ]);
  bar.appendChild(xField);

  // one "constant" selector per parameter (disabled for the current x-axis one)
  P.params.forEach(p => {
    const opts = [{ value: ALL, label: "All values" }]
      .concat(p.values.map(v => ({ value: v, label: v })));
    const field = el("div", { class: "vpb-field", id: "field_" + p.key }, [
      el("label", { for: "const_" + p.key }, [p.key]),
      makeSelect("const_" + p.key, opts),
    ]);
    bar.appendChild(field);
  });

  // tick box: the cost panel is hidden unless this is selected
  const costBox = el("input", { type: "checkbox", id: "showCost" });
  const costField = el("div", { class: "vpb-field" }, [
    el("label", { class: "vpb-checkline" }, [
      costBox, " show cost per electronic step",
    ]),
  ]);
  bar.appendChild(costField);

  document.getElementById("xsel").addEventListener("change", redraw);
  P.params.forEach(p =>
    document.getElementById("const_" + p.key).addEventListener("change", redraw));
  costBox.addEventListener("change", redraw);
}

function xOf(rec, key) {
  const p = byKey[key];
  if (p.numeric) return rec.nums[key];
  const idx = p.values.indexOf(rec.vals[key]);
  return idx < 0 ? p.values.length : idx;
}

function redraw() {
  const xKey = document.getElementById("xsel").value;
  const xMeta = byKey[xKey];

  // The x-axis parameter cannot also be held constant.
  P.params.forEach(p => {
    const sel = document.getElementById("const_" + p.key);
    const isX = p.key === xKey;
    sel.disabled = isX;
    document.getElementById("field_" + p.key).style.opacity = isX ? 0.4 : 1;
  });

  const otherKeys = P.params.map(p => p.key).filter(k => k !== xKey);
  const constraints = {};
  const groupKeys = [];
  otherKeys.forEach(k => {
    const v = document.getElementById("const_" + k).value;
    if (v === ALL) groupKeys.push(k); else constraints[k] = v;
  });

  let rows = P.records.filter(r =>
    r.vals[xKey] !== null && r.vals[xKey] !== undefined &&
    Object.entries(constraints).every(([k, v]) => r.vals[k] === v));

  // Split the remaining rows into series by every "All values" parameter.
  const groups = new Map();
  rows.forEach(r => {
    const label = groupKeys.length
      ? groupKeys.map(k => k + "=" + r.vals[k]).join(", ")
      : "all configs";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(r);
  });
  const labels = Array.from(groups.keys()).sort();

  // Shared tick labels: original parameter strings at their x positions.
  const tickMap = new Map();
  rows.forEach(r => tickMap.set(xOf(r, xKey), r.vals[xKey]));
  const tickvals = Array.from(tickMap.keys()).sort((a, b) => a - b);
  const ticktext = tickvals.map(v => tickMap.get(v));

  const energyTraces = [];
  const costTraces = [];
  labels.forEach((label, gi) => {
    const g = groups.get(label).slice().sort((a, b) => xOf(a, xKey) - xOf(b, xKey));
    const color = P.palette[gi % P.palette.length];
    const x = g.map(r => xOf(r, xKey));
    const text = g.map(r => r.vals[xKey]);
    const cfg = g.map(r => r.config);
    const showLegend = groupKeys.length > 0;
    const common = {
      x: x, text: text, customdata: cfg, mode: "markers+lines",
      line: { color: color, width: 2 },
      marker: { size: 8, color: color, line: { width: 1, color: "white" } },
      legendgroup: label, name: label,
    };
    energyTraces.push(Object.assign({}, common, {
      y: g.map(r => r.energy), showlegend: showLegend,
      hovertemplate: xKey + " = %{text}<br>energy = %{y:.5f} eV" +
        "<br>folder %{customdata}<extra>" + label + "</extra>",
    }));
    costTraces.push(Object.assign({}, common, {
      y: g.map(r => r.cost), showlegend: false,
      hovertemplate: xKey + " = %{text}<br>time/step = %{y:.4g} s" +
        "<br>folder %{customdata}<extra>" + label + "</extra>",
    }));
  });

  const FONT = { family: "Helvetica Neue, Helvetica, Arial, sans-serif", size: 12, color: "#333" };
  const xaxis = { title: { text: xMeta.title }, tickvals: tickvals, ticktext: ticktext };
  const baseLayout = {
    template: "plotly_white", font: FONT, paper_bgcolor: "white", plot_bgcolor: "white",
    hovermode: "closest", margin: { t: 50, b: 60, l: 80, r: 20 },
    legend: { orientation: "h", yanchor: "top", y: -0.2, xanchor: "center", x: 0.5,
              font: { size: 11 }, title: { text: "other parameters: " } },
    hoverlabel: { bgcolor: "white", bordercolor: "black" },
  };
  const energyLayout = Object.assign({}, baseLayout, {
    title: { text: "Energy", x: 0.5, xanchor: "center", font: { size: 16 } },
    xaxis: xaxis, yaxis: { title: { text: "energy (eV)" } },
  });
  const costLayout = Object.assign({}, baseLayout, {
    title: { text: "Cost per electronic step", x: 0.5, xanchor: "center", font: { size: 16 } },
    xaxis: xaxis, yaxis: { title: { text: "time / electronic step (s)" }, rangemode: "tozero" },
  });
  const opts = { responsive: true, displaylogo: false };

  // The cost panel sits beneath the energy panel and only exists while its
  // tick box is selected.
  const showCost = document.getElementById("showCost").checked;
  const costDiv = document.getElementById("plotCost");
  costDiv.style.display = showCost ? "" : "none";

  Plotly.react("plotEnergy", energyTraces, energyLayout, opts);
  if (showCost) {
    Plotly.react("plotCost", costTraces, costLayout, opts);
  } else if (costDiv.data) {
    Plotly.purge(costDiv);
  }
}

buildControls();
redraw();
"""

_REPORT_CSS = """
body { margin: 0; padding: 24px; background: white;
  font-family: Helvetica Neue, Helvetica, Arial, sans-serif; color: #222; }
h1 { font-size: 20px; font-weight: 600; text-align: center; margin: 0 0 18px; }
#controls { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-end;
  justify-content: center; padding: 14px 16px; margin: 0 auto 18px; max-width: 1100px;
  background: #f6f8fa; border: 1px solid rgba(0,0,0,0.08); border-radius: 8px; }
.vpb-field { display: flex; flex-direction: column; gap: 4px; }
.vpb-field label { font-size: 11px; color: #555; font-weight: 600; }
.vpb-select { font: 13px Helvetica Neue, Helvetica, Arial, sans-serif; padding: 5px 8px;
  border: 1px solid rgba(0,0,0,0.25); border-radius: 5px; background: white; min-width: 120px; }
.vpb-select:disabled { background: #eee; color: #999; }
.vpb-checkline { display: flex; align-items: center; gap: 6px; font-size: 13px;
  color: #333; font-weight: 400; padding: 6px 0; cursor: pointer; }
.vpb-checkline input { width: 15px; height: 15px; cursor: pointer; }
#plots { display: flex; flex-direction: column; gap: 16px; max-width: 1100px; margin: 0 auto; }
#plotEnergy, #plotCost { width: 100%; height: 520px; }
"""


def write_html(df: pd.DataFrame, specs: list[ParamSpec], out_path: Path) -> None:
    """Write the self-contained interactive report (plotly.js + data embedded)."""
    import json

    from plotly.offline import get_plotlyjs

    payload = _figure_payload(df, specs)
    html = (
        "<!DOCTYPE html>\n<html lang='en'>\n<head>\n<meta charset='utf-8'>\n"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>\n"
        "<title>VASP parameter benchmarking &#8226; energy &amp; cost</title>\n"
        f"<style>{_REPORT_CSS}</style>\n"
        f"<script>{get_plotlyjs()}</script>\n"
        "</head>\n<body>\n"
        "<h1>VASP parameter benchmarking &#8226; energy &amp; cost</h1>\n"
        "<div id='controls'></div>\n"
        "<div id='plots'><div id='plotEnergy'></div><div id='plotCost'></div></div>\n"
        f"<script>window.__REPORT_PAYLOAD__ = {json.dumps(payload)};</script>\n"
        f"<script>{_REPORT_JS}</script>\n"
        "</body>\n</html>\n"
    )
    out_path.write_text(html, encoding="utf-8")


def report(
    root: str = "VASP_Parameter_Benchmarking",
    out: str = "report",
    no_sacct: bool = False,
    skip_steps: int = 5,
    parameters_file: str | None = None,
) -> pd.DataFrame:
    """Run the full report pipeline. Returns the results DataFrame.

    ``skip_steps`` is the number of leading (warm-up) electronic steps dropped
    from each run's timing average. The sweep (which tags, their order, the mode)
    is read from the parameters file ``setup`` wrote into ``root`` (override with
    ``parameters_file``); each config's actual values are read from its own
    INCAR/KPOINTS.
    """
    if skip_steps < 0:
        raise ValueError(f"--skip-steps must be >= 0, got {skip_steps}")

    root_dir = Path(root)
    if not root_dir.is_dir():
        raise FileNotFoundError(f"benchmark root not found: {root_dir}")

    specs, _settings = load_specs(root_dir, parameters_file)

    use_sacct = not no_sacct
    rows: list[dict] = []
    skipped: list[str] = []

    print(f"Scanning {root_dir}/ for config directories...")
    run_dirs = index_mod.config_dirs(root_dir)
    print(
        f"Found {len(run_dirs)} config director{'y' if len(run_dirs) == 1 else 'ies'}. "
        f"Reading INCAR/KPOINTS + OUTCARs"
        + (" and querying sacct" if use_sacct else " (sacct disabled)")
        + f" (dropping the first {skip_steps} electronic step(s) for timing)..."
    )

    progress = tqdm(run_dirs, desc="Collecting", unit="run")
    for run_dir in progress:
        progress.set_postfix_str(run_dir.name)
        row = collect_run(run_dir, specs, use_sacct, skip_steps=skip_steps)
        if row is None:
            skipped.append(str(run_dir))
        else:
            rows.append(row)
    progress.close()
    print(f"  parsed {len(rows)} usable run(s); skipped {len(skipped)}.")

    if not rows:
        print(f"No usable runs found under {root_dir}/ (no parseable final energy).")
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values("config").reset_index(drop=True)

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "results.csv"
    print(f"Writing results table -> {csv_path}")
    df.to_csv(csv_path, index=False)

    html_path = out_dir / "vasp_parameter_benchmark_results.html"
    print(f"Building interactive plot -> {html_path} (embedding plotly.js)...")
    write_html(df, specs, html_path)

    # Refresh the folder navigator so its run/running/failed/pending status
    # reflects this report (sacct distinguishes running from failed).
    index_path = index_mod.write_index(root_dir, specs, use_sacct=use_sacct)
    print(f"Refreshed folder navigator -> {index_path}")

    if skipped:
        (out_dir / "skipped.txt").write_text("\n".join(skipped) + "\n")
        print(f"Wrote list of skipped directories -> {out_dir / 'skipped.txt'}")

    print(
        f"Done: {len(df)} run(s) reported, {len(skipped)} skipped. "
        f"Open {html_path} to view."
    )
    return df
