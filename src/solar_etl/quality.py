"""Per-run data-quality report: Markdown, JSON summary and two PNG charts."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import Date, Engine, bindparam, select, text  # noqa: E402

from solar_etl import db  # noqa: E402
from solar_etl.config import DAYLIGHT_GHI_WM2  # noqa: E402
from solar_etl.validate import RULES  # noqa: E402

SERIES = "#2a78d6"
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def latest_run_id(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            select(db.pipeline_runs.c.run_id).order_by(db.pipeline_runs.c.started_at.desc()).limit(1)
        ).scalar()


def summarise(engine: Engine, run_id: str) -> dict:
    with engine.connect() as conn:
        run = conn.execute(select(db.pipeline_runs).where(db.pipeline_runs.c.run_id == run_id)).mappings().one()
        query = text("""
            SELECT d.*, s.name, s.capacity_kw, s.dc_capacity_kw FROM v_daily_system d
            JOIN dim_system s ON s.system_id = d.system_id
            WHERE d.local_date BETWEEN :start AND :end
        """).bindparams(bindparam("start", type_=Date), bindparam("end", type_=Date))
        daily = pd.read_sql(query, conn, params={"start": run["window_start"], "end": run["window_end"]})
        rules = pd.read_sql(
            select(db.dq_results.c.system_id, db.dq_results.c.rule, db.dq_results.c.rows_affected)
            .where(db.dq_results.c.run_id == run_id), conn)

    daily["local_date"] = pd.to_datetime(daily["local_date"])
    systems = []
    for sid, g in daily.groupby("system_id"):
        daylight = int(g["daylight_hours"].sum())
        covered = int(g["daylight_hours_with_energy"].sum())
        systems.append({
            "system_id": int(sid),
            "name": g["name"].iloc[0],
            "capacity_kw": float(g["capacity_kw"].iloc[0]),
            "dc_capacity_kw": float(g["dc_capacity_kw"].iloc[0]),
            "days": int(len(g)),
            "days_with_energy": int((g["energy_kwh"].fillna(0) > 0).sum()),
            "energy_mwh": round(float(g["energy_kwh"].sum()) / 1000, 2),
            "daylight_hours": daylight,
            "daylight_coverage_pct": round(100 * covered / daylight, 1) if daylight else None,
            "median_daily_pr": (round(float(g["performance_ratio"].median()), 3)
                                if g["performance_ratio"].notna().any() else None),
        })
    by_rule = rules.groupby("rule")["rows_affected"].sum().to_dict() if not rules.empty else {}
    by_rule_system = (rules.pivot_table(index="rule", columns="system_id", values="rows_affected",
                                        aggfunc="sum", fill_value=0) if not rules.empty else pd.DataFrame())
    return {
        "run": {k: (str(v) if v is not None else None) for k, v in dict(run).items()},
        "systems": systems,
        "rules": {r.name: int(by_rule.get(r.name, 0)) for r in RULES},
        "_daily": daily,
        "_by_rule_system": by_rule_system,
    }


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _energy_chart(daily: pd.DataFrame, path: Path) -> None:
    ids = sorted(daily["system_id"].unique())
    fig, axes = plt.subplots(len(ids), 1, figsize=(10, 2.2 * len(ids)), sharex=True, sharey=True,
                             facecolor=SURFACE, squeeze=False)
    for ax, sid in zip(axes[:, 0], ids, strict=True):
        g = daily[daily["system_id"] == sid].sort_values("local_date")
        _style(ax)
        ax.plot(g["local_date"], g["energy_kwh"], color=SERIES, linewidth=1.0)
        ax.set_title(f"{sid}  {g['name'].iloc[0]}  ({g['capacity_kw'].iloc[0]:g} kW)",
                     loc="left", fontsize=9, color=INK)
        ax.set_ylabel("kWh / day", fontsize=8, color=INK_2)
    fig.suptitle("Daily AC energy per system (gaps = no usable data)", x=0.01, ha="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _rules_chart(rules: dict[str, int], path: Path) -> None:
    hits = sorted(((n, r) for r, n in rules.items() if n), reverse=False)
    fig, ax = plt.subplots(figsize=(8, 0.35 * max(len(hits), 1) + 1.2), facecolor=SURFACE)
    _style(ax)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.grid(axis="y", visible=False)
    if hits:
        counts, names = zip(*hits, strict=True)
        bars = ax.barh(names, counts, color=SERIES, height=0.6)
        ax.bar_label(bars, labels=[f"{c:,}" for c in counts], padding=3, fontsize=8, color=INK_2)
        ax.set_xscale("log")
    ax.set_title("Rows affected per quality rule (log scale)", loc="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _markdown(summary: dict, figures: dict[str, str]) -> str:
    run = summary["run"]
    lines = [
        "# Data quality report",
        "",
        "Source: NREL PVDAQ (historical public monitoring data, CC BY 4.0) and the Open-Meteo "
        "historical weather archive (CC BY 4.0). This is archived open data, not a live feed.",
        "",
        f"- Run: `{run['run_id']}` ({run['status']})",
        f"- Window (local dates): {run['window_start']} to {run['window_end']}",
        f"- Started / finished (UTC): {run['started_at']} / {run['finished_at']}",
        f"- Rows upserted: {run['rows_upserted']}",
        f"- Code version: {run['code_version']}",
    ]
    if run.get("error_message"):
        lines.append(f"- Errors: {run['error_message']}")
    lines += [
        "",
        "## Systems",
        "",
        "| System | Rated kW | DC kWp | Days | Days with energy | Energy (MWh) | Daylight hours | "
        "Daylight coverage | Median daily PR |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summary["systems"]:
        cov = f"{s['daylight_coverage_pct']}%" if s["daylight_coverage_pct"] is not None else "n/a"
        pr = f"{s['median_daily_pr']:.3f}" if s["median_daily_pr"] is not None else "n/a"
        lines.append(f"| {s['system_id']} {s['name']} | {s['capacity_kw']:g} | {s['dc_capacity_kw']:g} | {s['days']} | "
                     f"{s['days_with_energy']} | {s['energy_mwh']:,} | {s['daylight_hours']:,} | {cov} | {pr} |")
    lines += [
        "",
        f"Daylight coverage = share of hours with modelled GHI of at least {DAYLIGHT_GHI_WM2:g} W/m² that have a "
        "trusted hourly energy value. PR (performance ratio) = AC energy / (DC kWp x plane-of-array "
        "insolation), using only hours with both values. DC kWp is module count x module rating from the "
        "PVDAQ module list, so PR is indicative, not a certified figure. Systems without an irradiance "
        "sensor show n/a.",
        "",
        "## Rules",
        "",
    ]
    by_sys: pd.DataFrame = summary["_by_rule_system"]
    sys_ids = list(by_sys.columns) if not by_sys.empty else []
    sys_cols = "".join(f"{s} | " for s in sys_ids)
    header = f"| Rule | Severity | Action | {sys_cols}Total |"
    lines += [header, "|---|---|---|" + "---:|" * (len(sys_ids) + 1)]
    for rule in RULES:
        per = [f"{int(by_sys.loc[rule.name, s]):,}" if rule.name in by_sys.index else "0" for s in sys_ids]
        lines.append(f"| `{rule.name}` | {rule.severity} | {rule.action} | " + " | ".join(per)
                     + (" | " if per else "") + f"{summary['rules'][rule.name]:,} |")
    lines += ["", "Rule definitions:", ""]
    lines += [f"- `{r.name}`: {r.description}" for r in RULES]
    lines += ["", "## Charts", ""]
    lines += [f"![{alt}]({src})" for alt, src in figures.items()]
    lines.append("")
    return "\n".join(lines)


def write_report(engine: Engine, out_dir: Path, run_id: str | None = None) -> Path:
    run_id = run_id or latest_run_id(engine)
    if run_id is None:
        raise RuntimeError("no pipeline runs in the database")
    summary = summarise(engine, run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(exist_ok=True)
    figures = {}
    if not summary["_daily"].empty:
        _energy_chart(summary["_daily"], fig_dir / "daily_energy.png")
        figures["Daily energy per system"] = "figures/daily_energy.png"
    _rules_chart(summary["rules"], fig_dir / "rule_counts.png")
    figures["Rows affected per rule"] = "figures/rule_counts.png"

    report = out_dir / "quality_report.md"
    report.write_text(_markdown(summary, figures), encoding="utf-8")
    public = {k: v for k, v in summary.items() if not k.startswith("_")}
    (out_dir / "quality_summary.json").write_text(json.dumps(public, indent=2), encoding="utf-8")
    return report
