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
this parameter?* For each swept parameter it shows, with the other parameters
held at their baseline value:

  * **Convergence** - |E - E_ref| in meV/atom against the highest-fidelity value
    of that parameter (highest ENCUT, densest KPOINTS, ...);
  * **Cost** - mean wall time per electronic step.

A dropdown switches which parameter is shown; a dotted line marks a 1 meV/atom
convergence guide.
"""

from __future__ import annotations

import statistics
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from . import incar as incar_mod
from . import kpoints as kpoints_mod
from . import sacct
from .outcar import (
    final_energy,
    max_force,
    n_ions,
    oszicar_final_e0,
    parse_loop_times,
)
from .parameters import (
    INCAR,
    KPOINTS,
    ParamSpec,
    numeric_value,
    parse_parameters_file,
)

PARAMETERS_FILENAME = "vasp_parameter_benchmarking_parameters.txt"

FONT_FAMILY = "Helvetica Neue, Helvetica, Arial, sans-serif"
CONV_COLOR = "#2c7fb8"
COST_COLOR = "#e6550d"
# Common "good enough" convergence guide drawn on the convergence panel.
CONV_GUIDE_MEV = 1.0


def load_specs(root_dir: Path, parameters_file: str | None = None) -> tuple[list[ParamSpec], dict]:
    """Load the swept specs + settings written by ``setup`` (or ``--parameters``)."""
    path = Path(parameters_file) if parameters_file else root_dir / PARAMETERS_FILENAME
    if not path.is_file():
        raise FileNotFoundError(
            f"parameters file not found: {path}\n"
            "Expected the one written by 'setup' in the benchmark root, or pass --parameters."
        )
    return parse_parameters_file(path)


def read_config_parameters(run_dir: Path, specs: list[ParamSpec]) -> dict[str, str | None]:
    """Read each swept parameter's actual value from this config's input files.

    The INCAR/KPOINTS in the directory are the record - this reads the swept
    INCAR tags from its INCAR and the grid from its KPOINTS, so the report always
    reflects what was actually run.
    """
    values: dict[str, str | None] = {}
    for s in specs:
        if s.target == INCAR:
            values[s.key] = incar_mod.read_tag(run_dir / "INCAR", s.key)
        elif s.target == KPOINTS:
            values[s.key] = kpoints_mod.read_grid(run_dir / "KPOINTS")
    return values


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
    for key, value in read_config_parameters(run_dir, specs).items():
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


def _spec_slice(
    df: pd.DataFrame, spec: ParamSpec, baseline: dict, all_keys: list[str]
) -> pd.DataFrame:
    """Rows where ``spec`` varies and every other swept key sits at baseline."""
    mask = pd.Series(True, index=df.index)
    for key in all_keys:
        if key == spec.key:
            continue
        col = f"param_{key}"
        if col in df.columns:
            mask &= df[col].astype(str) == str(baseline.get(key))
    return df[mask].copy()


def _build_figure(df: pd.DataFrame, specs: list[ParamSpec]):
    """Build the convergence-vs-cost figure with a per-parameter dropdown."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    baseline = {s.key: s.values[0] for s in specs}
    all_keys = [s.key for s in specs]

    fig = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=("Convergence", "Cost"),
        horizontal_spacing=0.12,
    )

    # Two traces (convergence, cost) per spec; only the first spec visible initially.
    trace_visible: list[bool] = []
    axis_settings: list[dict] = []  # per-spec x-axis title + tick labels

    for si, spec in enumerate(specs):
        key = spec.key
        is_kpoints = spec.target == KPOINTS
        sub = _spec_slice(df, spec, baseline, all_keys)

        numcol = f"param_{key}__num"
        valcol = f"param_{key}"
        numeric = numcol in sub.columns and sub[numcol].notna().all() and not sub.empty

        if numeric:
            sub = sub.sort_values(numcol)
            x = sub[numcol].astype(float).tolist()
            tickvals = x
            ticktext = sub[valcol].astype(str).tolist()
            # Reference = highest-fidelity value (largest numeric x).
            ref_mask = sub[numcol] == sub[numcol].max()
        else:
            # Categorical: order by the spec's declared value order.
            order = {str(v): i for i, v in enumerate(spec.values)}
            sub["_ord"] = sub[valcol].astype(str).map(order).fillna(len(order))
            sub = sub.sort_values("_ord")
            x = list(range(len(sub)))
            tickvals = x
            ticktext = sub[valcol].astype(str).tolist()
            ref_mask = sub["_ord"] == sub["_ord"].max()

        # Convergence: |E - E_ref| per atom, in meV/atom.
        epa = pd.to_numeric(sub["energy_per_atom_eV"], errors="coerce")
        ref_epa = epa[ref_mask.values].iloc[0] if ref_mask.any() and not epa.empty else None
        delta = (epa - ref_epa).abs() * 1000.0 if ref_epa is not None else epa * float("nan")

        cost = pd.to_numeric(sub["loop_real_mean_s"], errors="coerce")

        unit = "meV/atom"
        x_title = f"total k-points ({key} grid)" if is_kpoints else key

        fig.add_trace(
            go.Scatter(
                x=x, y=delta.tolist(),
                mode="markers+lines",
                line=dict(color=CONV_COLOR, width=2.5),
                marker=dict(size=9, color=CONV_COLOR, line=dict(width=1.5, color="white")),
                text=ticktext, customdata=sub["config"],
                hovertemplate=(
                    f"{key} = %{{text}}<br>|&Delta;E| = %{{y:.4g}} {unit}"
                    "<br>%{customdata}<extra></extra>"
                ),
                name="convergence", showlegend=False, visible=(si == 0),
            ),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=x, y=cost.tolist(),
                mode="markers+lines",
                line=dict(color=COST_COLOR, width=2.5),
                marker=dict(size=9, color=COST_COLOR, line=dict(width=1.5, color="white")),
                text=ticktext, customdata=sub["config"],
                hovertemplate=(
                    f"{key} = %{{text}}<br>time/step = %{{y:.4g}} s"
                    "<br>%{customdata}<extra></extra>"
                ),
                name="cost", showlegend=False, visible=(si == 0),
            ),
            row=1, col=2,
        )

        trace_visible.append(si == 0)
        axis_settings.append(
            dict(title=x_title, tickvals=tickvals, ticktext=ticktext)
        )

    # Dropdown: show one spec's pair of traces and relabel the shared x-axes.
    buttons = []
    for si, spec in enumerate(specs):
        visible = [False] * (2 * len(specs))
        visible[2 * si] = True
        visible[2 * si + 1] = True
        s = axis_settings[si]
        buttons.append(
            dict(
                label=spec.key,
                method="update",
                args=[
                    {"visible": visible},
                    {
                        "xaxis.title.text": s["title"],
                        "xaxis.tickvals": s["tickvals"],
                        "xaxis.ticktext": s["ticktext"],
                        "xaxis2.title.text": s["title"],
                        "xaxis2.tickvals": s["tickvals"],
                        "xaxis2.ticktext": s["ticktext"],
                    },
                ],
            )
        )

    # Initial axis labels (first spec).
    first = axis_settings[0]
    fig.update_xaxes(
        title_text=first["title"], tickvals=first["tickvals"], ticktext=first["ticktext"],
        row=1, col=1,
    )
    fig.update_xaxes(
        title_text=first["title"], tickvals=first["tickvals"], ticktext=first["ticktext"],
        row=1, col=2,
    )
    fig.update_yaxes(title_text="|&Delta;E| from best (meV/atom)", rangemode="tozero", row=1, col=1)
    fig.update_yaxes(title_text="time / electronic step (s)", rangemode="tozero", row=1, col=2)

    # 1 meV/atom convergence guide on the convergence panel.
    fig.add_hline(
        y=CONV_GUIDE_MEV, line=dict(dash="dot", color="rgba(120,120,120,0.8)", width=1.5),
        annotation_text=f"{CONV_GUIDE_MEV:g} meV/atom", annotation_position="top left",
        row=1, col=1,
    )

    fig.update_layout(
        updatemenus=[
            dict(
                type="dropdown", direction="down", showactive=True, active=0,
                x=0.0, xanchor="left", y=1.16, yanchor="top",
                buttons=buttons, bgcolor="white", bordercolor="rgba(0,0,0,0.2)",
                borderwidth=1, font=dict(size=12, family=FONT_FAMILY),
                pad=dict(t=4, b=4, l=6, r=6),
            )
        ],
        annotations=list(fig.layout.annotations)
        + [
            dict(
                text="Parameter:", x=-0.0, xref="paper", y=1.20, yref="paper",
                xanchor="right", showarrow=False,
                font=dict(size=12, family=FONT_FAMILY, color="#444"),
            )
        ],
        title=dict(
            text="VASP parameter benchmarking &#8226; convergence vs cost",
            x=0.5, xanchor="center", font=dict(size=20, family=FONT_FAMILY, color="#222"),
        ),
        template="plotly_white", height=560, margin=dict(t=130, b=60, l=70, r=40),
        font=dict(family=FONT_FAMILY, size=12, color="#333"),
        paper_bgcolor="white", plot_bgcolor="white", hovermode="closest",
        hoverlabel=dict(bgcolor="white", bordercolor="black", font=dict(family=FONT_FAMILY)),
    )
    return fig


def write_html(df: pd.DataFrame, specs: list[ParamSpec], out_path: Path) -> None:
    """Write the report HTML (self-contained, plotly.js embedded)."""
    _build_figure(df, specs).write_html(str(out_path), include_plotlyjs=True)


def _config_dirs(root_dir: Path) -> list[Path]:
    """Config directories under ``root`` - the immediate subdirs holding an INCAR."""
    return sorted(p for p in root_dir.iterdir() if p.is_dir() and (p / "INCAR").is_file())


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
    run_dirs = _config_dirs(root_dir)
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

    if skipped:
        (out_dir / "skipped.txt").write_text("\n".join(skipped) + "\n")
        print(f"Wrote list of skipped directories -> {out_dir / 'skipped.txt'}")

    print(
        f"Done: {len(df)} run(s) reported, {len(skipped)} skipped. "
        f"Open {html_path} to view."
    )
    return df
