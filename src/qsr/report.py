"""End-to-end study: data -> features -> walk-forward + holdout -> results.

Everything under ``results/`` and the block between the RESULTS markers in README.md is
written by this module. No number in the write-up is typed by hand.

    python -m qsr.report --start 2024-07 --end 2025-06 --update-readme
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from qsr import data as qdata
from qsr import evaluate as ev
from qsr.features import FEATURES, build_features, forward_log_return, make_dataset
from qsr.models import MODELS
from qsr.validation import dev_positions, holdout, purge_width, walk_forward

README_START = "<!-- RESULTS:START -->"
README_END = "<!-- RESULTS:END -->"


@dataclass
class Config:
    symbol: str = "BTCUSDT"
    interval: str = "1m"
    start: str = "2024-07"
    end: str = "2025-06"
    data_dir: str = "data/raw"
    out_dir: str = "results"
    horizons: tuple[int, ...] = (1, 5, 15)
    delays: tuple[int, ...] = (0, 1)
    models: tuple[str, ...] = tuple(MODELS)
    n_folds: int = 5
    holdout_frac: float = 0.2
    min_train_frac: float = 0.3
    fee_bps_per_side: float = 10.0
    n_boot: int = 300
    diagnostic_horizon: int = 5
    primary: tuple[str, int, int] = ("logit_all", 5, 1)  # pre-registered: model, horizon, delay
    seed: int = 0
    feature_names: tuple[str, ...] = field(default_factory=lambda: tuple(FEATURES))

    @property
    def round_trip_cost_bps(self) -> float:
        return 2.0 * self.fee_bps_per_side

    @property
    def n_tests(self) -> int:
        n_models = sum(not MODELS[m].is_baseline for m in self.models)
        return n_models * len(self.horizons) * len(self.delays)


# --------------------------------------------------------------------------- core loop


def _fit_predict(
    model_name: str, X: pd.DataFrame, y: pd.Series, train, test
) -> tuple[np.ndarray, float]:
    spec = MODELS[model_name]
    cols = list(spec.features) if spec.features else list(X.columns)
    tr = train[~np.isnan(y.to_numpy()[train])]  # flat outcomes carry no direction to learn
    model = spec.factory()
    model.fit(X.iloc[tr][cols].to_numpy(), y.iloc[tr].to_numpy().astype(int))
    p = model.predict_proba(X.iloc[test][cols].to_numpy())[:, 1]
    return p, float(y.iloc[tr].mean())


def evaluate_config(ds, cfg: Config, level: float) -> tuple[list[dict], list[dict], dict]:
    purge = purge_width(ds.horizon, ds.delay)
    idx = pd.DatetimeIndex(ds.X.index)
    folds = walk_forward(idx, cfg.n_folds, cfg.holdout_frac, cfg.min_train_frac, purge)
    ho = holdout(idx, cfg.holdout_frac, cfg.min_train_frac, purge)
    y, fwd = ds.y.to_numpy(), ds.fwd.to_numpy()
    block = ev.default_block_len(ds.horizon, ds.delay)

    wf_rows, ho_rows, ho_preds = [], [], {}
    for name in cfg.models:
        for fold in folds:
            p, base = _fit_predict(name, ds.X, ds.y, fold.train, fold.test)
            m = ev.point_metrics(y[fold.test], p, fwd[fold.test], base)
            wf_rows.append(
                {
                    "model": name,
                    "horizon": ds.horizon,
                    "delay": ds.delay,
                    "fold": fold.name,
                    "test_start": str(fold.test_start),
                    "test_end": str(fold.test_end),
                    "n_train": len(fold.train),
                    **m,
                }
            )
        p, base = _fit_predict(name, ds.X, ds.y, ho.train, ho.test)
        ho_preds[name] = (p, y[ho.test])
        m = ev.point_metrics(y[ho.test], p, fwd[ho.test], base)
        ci = ev.bootstrap_intervals(y[ho.test], p, fwd[ho.test], block, cfg.n_boot, level, cfg.seed)
        ho_rows.append(
            {
                "model": name,
                "horizon": ds.horizon,
                "delay": ds.delay,
                "test_start": str(ho.test_start),
                "test_end": str(ho.test_end),
                "n_train": len(ho.train),
                **m,
                "auc_lo": ci["auc"].lo,
                "auc_hi": ci["auc"].hi,
                "ic_lo": ci["rank_ic"].lo,
                "ic_hi": ci["rank_ic"].hi,
                "edge_lo": ci["edge_bps"].lo,
                "edge_hi": ci["edge_bps"].hi,
                "ci_level": level,
                "block_len": block,
                "verdict": ev.verdict(
                    ci["auc"], ci["edge_bps"], cfg.round_trip_cost_bps, MODELS[name].is_baseline
                ),
            }
        )
    return wf_rows, ho_rows, ho_preds


def feature_ic_table(
    grid: pd.DataFrame, feats: pd.DataFrame, cfg: Config, level: float
) -> pd.DataFrame:
    """Univariate rank IC of each feature vs the forward return, development period only."""
    fwd = forward_log_return(grid["close"], cfg.diagnostic_horizon, 0)
    ok = feats.notna().all(axis=1) & fwd.notna()
    X, f = feats[ok], fwd[ok].to_numpy()
    dev = dev_positions(len(X), cfg.holdout_frac)
    X, f = X.iloc[dev], f[dev]
    rng = np.random.default_rng(cfg.seed)
    boot = ev.block_indices(
        len(f), ev.default_block_len(cfg.diagnostic_horizon, 0), cfg.n_boot, rng
    )
    rows = []
    # Same interval construction as the model metrics: point +/- z * bootstrap SD. Percentile
    # tails of a few hundred draws at a Bonferroni level are unreliable (METHODOLOGY.md,
    # "Interval construction").
    from statistics import NormalDist

    z = NormalDist().inv_cdf(1 - (1 - level) / 2)
    for col in X.columns:
        v = X[col].to_numpy()
        ic = ev._rank_ic(v, f)
        bs = np.array([ev._rank_ic(v[b], f[b]) for b in boot])
        bs = bs[~np.isnan(bs)]
        if bs.size >= 2 and not np.isnan(ic):
            half = z * float(np.std(bs, ddof=1))
            lo, hi = ic - half, ic + half
        else:
            lo, hi = np.nan, np.nan
        rows.append({"feature": col, "rank_ic": ic, "ic_lo": lo, "ic_hi": hi})
    return pd.DataFrame(rows).sort_values("rank_ic", key=np.abs, ascending=False)


# --------------------------------------------------------------------------- figures


def _figures(
    wf: pd.DataFrame,
    ho: pd.DataFrame,
    fic: pd.DataFrame,
    cfg: Config,
    out: Path,
    preds_for_calibration: dict[str, tuple[np.ndarray, np.ndarray]],
) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figdir = out / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    written = []

    # 1. AUC by walk-forward fold (delay = 0)
    hs = list(cfg.horizons)
    fig, axes = plt.subplots(1, len(hs), figsize=(4.2 * len(hs), 3.4), sharey=True, squeeze=False)
    for ax, h in zip(axes[0], hs, strict=True):
        sub = wf[(wf.horizon == h) & (wf.delay == 0)]
        for name, g in sub.groupby("model", sort=False):
            if MODELS[name].is_baseline:
                continue
            ax.plot(g["fold"], g["auc"], marker="o", label=name)
        ax.axhline(0.5, color="grey", lw=1, ls="--")
        ax.set_title(f"horizon {h} min")
        ax.set_xlabel("walk-forward fold")
    axes[0][0].set_ylabel("AUC")
    axes[0][-1].legend(fontsize=8)
    fig.suptitle("Out-of-sample AUC by fold (delay 0)")
    fig.tight_layout()
    fig.savefig(figdir / "fold_auc.png", dpi=130)
    plt.close(fig)
    written.append("figures/fold_auc.png")

    # 2. Holdout calibration at the diagnostic horizon
    if not preds_for_calibration:
        return written + _feature_ic_figure(fic, cfg, figdir, plt)
    fig, ax = plt.subplots(figsize=(4.6, 4.2))
    for name, (p, y) in preds_for_calibration.items():
        m = ~np.isnan(y)
        p, y = p[m], y[m]
        if np.ptp(p) == 0:
            continue
        edges = np.unique(np.quantile(p, np.linspace(0, 1, 11)))
        b = np.clip(np.digitize(p, edges[1:-1]), 0, len(edges) - 2)
        xs = [p[b == i].mean() for i in range(len(edges) - 1) if (b == i).any()]
        ys = [y[b == i].mean() for i in range(len(edges) - 1) if (b == i).any()]
        ax.plot(xs, ys, marker="o", label=name)
    lims = ax.get_xlim()
    ax.plot(lims, lims, color="grey", lw=1, ls="--")
    ax.set_xlabel("predicted P(up), decile mean")
    ax.set_ylabel("realized up-frequency")
    ax.set_title(f"Holdout calibration (h={cfg.diagnostic_horizon}, delay 0)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(figdir / "holdout_calibration.png", dpi=130)
    plt.close(fig)
    written.append("figures/holdout_calibration.png")
    return written + _feature_ic_figure(fic, cfg, figdir, plt)


def _feature_ic_figure(fic: pd.DataFrame, cfg: Config, figdir: Path, plt) -> list[str]:
    f = fic.iloc[::-1]
    fig, ax = plt.subplots(figsize=(6, 0.32 * len(f) + 1.2))
    err = np.clip(np.vstack([f.rank_ic - f.ic_lo, f.ic_hi - f.rank_ic]), 0, None)
    ax.barh(f.feature, f.rank_ic, xerr=err, color="#4a7fb5", ecolor="black", capsize=2)
    ax.axvline(0, color="grey", lw=1)
    ax.set_xlabel(f"rank IC vs {cfg.diagnostic_horizon}-min forward return")
    ax.set_title("Univariate feature IC, development period")
    fig.tight_layout()
    fig.savefig(figdir / "feature_ic.png", dpi=130)
    plt.close(fig)
    return ["figures/feature_ic.png"]


# --------------------------------------------------------------------------- writing


def _git_state() -> dict:
    def run(*args):
        try:
            return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    commit = run("git", "rev-parse", "HEAD")
    status = run("git", "status", "--porcelain")
    return {"commit": commit, "dirty": bool(status) if status is not None else None}


def _versions() -> dict:
    import matplotlib
    import scipy
    import sklearn

    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "scipy": scipy.__version__,
        "scikit-learn": sklearn.__version__,
        "matplotlib": matplotlib.__version__,
        "platform": platform.platform(),
    }


def _fmt(x: float, nd: int = 3) -> str:
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.{nd}f}"


def _ci(lo: float, hi: float, nd: int = 3) -> str:
    return f"[{_fmt(lo, nd)}, {_fmt(hi, nd)}]"


def _holdout_table(ho: pd.DataFrame, delays: tuple[int, ...] | None = None) -> str:
    rows = ho if delays is None else ho[ho.delay.isin(delays)]
    lines = [
        "| model | h | delay | AUC [CI] | acc / majority | LL skill "
        "| rank IC [CI] | edge bps [CI] | verdict |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows.itertuples():
        lines.append(
            f"| {r.model} | {r.horizon} | {r.delay} | {_fmt(r.auc)} {_ci(r.auc_lo, r.auc_hi)} "
            f"| {_fmt(r.accuracy)} / {_fmt(r.test_majority_acc)} | {_fmt(r.ll_skill, 4)} "
            f"| {_fmt(r.rank_ic)} {_ci(r.ic_lo, r.ic_hi)} "
            f"| {_fmt(r.edge_bps, 2)} {_ci(r.edge_lo, r.edge_hi, 2)} | {r.verdict} |"
        )
    return "\n".join(lines)


def _verdict_summary(ho: pd.DataFrame) -> str:
    tested = ho[ho.verdict != ev.BASELINE]
    counts = tested.verdict.value_counts()
    parts = [f"{int(counts.get(v, 0))} {v}" for v in (ev.ABOVE_COST, ev.BELOW_COST, ev.NO_SIGNAL)]
    return f"Of {len(tested)} model/horizon/delay configurations: " + "; ".join(parts) + "."


def write_results_md(cfg, quality, digests, wf, ho, fic, figs, manifest, out: Path) -> None:
    level = ho.ci_level.iloc[0]
    wf_sum = (
        wf.groupby(["model", "horizon", "delay"], sort=False)
        .agg(
            auc_mean=("auc", "mean"),
            auc_std=("auc", "std"),
            auc_min=("auc", "min"),
            edge_mean=("edge_bps", "mean"),
        )
        .reset_index()
    )
    q = quality
    lines = [
        f"# Results: {cfg.symbol} {cfg.interval}, {cfg.start} to {cfg.end}",
        "",
        f"_Generated {manifest['generated_utc']} by `python -m qsr.report` "
        f"(commit `{manifest['git']['commit'] or 'unknown'}`"
        f"{', dirty working tree' if manifest['git']['dirty'] else ''}). "
        "Nothing in this file is hand-edited._",
        "",
        "## Verdict",
        "",
        _primary_line(ho, cfg),
        "",
        _verdict_summary(ho),
        "",
        f"A configuration is **detectable** only if its holdout AUC interval lies entirely "
        f"above 0.5 at "
        f"the Bonferroni-adjusted level {level:.4f} ({cfg.n_tests} configurations tested, "
        f"family-wise alpha 0.05). It is **above the cost hurdle** only if, in addition, the "
        f"lower bound of gross edge per prediction exceeds the assumed round-trip cost of "
        f"{cfg.round_trip_cost_bps:.1f} bps ({cfg.fee_bps_per_side:.1f} bps per side). "
        "Edge is a hurdle check, not a backtest; see docs/METHODOLOGY.md.",
        "",
        "## Data",
        "",
        "| item | value |",
        "|---|---|",
        f"| bars loaded | {q.n_rows:,} |",
        f"| span (UTC) | {q.start} to {q.end} |",
        f"| missing bars | {q.missing_bars:,} ({q.missing_pct}%) |",
        f"| duplicate bars dropped | {q.duplicates_dropped} |",
        f"| zero-volume bars | {q.zero_volume_bars:,} |",
        f"| OHLC violations | {q.ohlc_violations} |",
        f"| taker volume > volume | {q.taker_volume_violations} |",
        f"| archives (sha256 verified) | {len(digests)} |",
        "",
        "Largest gaps (start, missing bars): "
        + (", ".join(f"{ts} ({n})" for ts, n in q.largest_gaps) or "none"),
        "",
        "## Holdout (evaluated once)",
        "",
        f"Holdout period: {ho.test_start.iloc[0]} to {ho.test_end.iloc[0]}. "
        f"Intervals: {level:.4f}, normal approximation with a moving-block-bootstrap "
        f"standard error "
        f'({cfg.n_boot} draws); see METHODOLOGY.md, "Interval construction".',
        "",
        _holdout_table(ho),
        "",
        "## Walk-forward stability (development period)",
        "",
        "| model | h | delay | AUC mean | AUC sd | AUC worst fold | edge bps mean |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in wf_sum.itertuples():
        lines.append(
            f"| {r.model} | {r.horizon} | {r.delay} | {_fmt(r.auc_mean)} | {_fmt(r.auc_std)} "
            f"| {_fmt(r.auc_min)} | {_fmt(r.edge_mean, 2)} |"
        )
    lines += [
        "",
        f"## Univariate feature IC (development period, h={cfg.diagnostic_horizon}, delay 0)",
        "",
        f"Intervals at the Bonferroni level for {len(fic)} features. Diagnostic only: "
        "no model or feature choice was made from this table.",
        "",
        "| feature | rank IC | CI |",
        "|---|---|---|",
    ]
    for r in fic.itertuples():
        lines.append(f"| {r.feature} | {_fmt(r.rank_ic, 4)} | {_ci(r.ic_lo, r.ic_hi, 4)} |")
    lines += (
        ["", "## Figures", ""]
        + [f"![{f}]({f})" for f in figs]
        + [
            "",
            "## Reproduce",
            "",
            "```",
            f"make data START={cfg.start} END={cfg.end}",
            "make report",
            "```",
            "",
            "Input archive digests and package versions are in `run_manifest.json`.",
            "",
        ]
    )
    (out / "RESULTS.md").write_text("\n".join(lines))


def _primary_line(ho: pd.DataFrame, cfg: Config) -> str:
    model, h, d = cfg.primary
    sel = ho[(ho.model == model) & (ho.horizon == h) & (ho.delay == d)]
    if sel.empty:
        return "Primary configuration was not part of this run."
    r = sel.iloc[0]
    return (
        f"**Primary configuration** (`{model}`, {h}-minute horizon, delay {d}): "
        f"AUC {_fmt(r.auc)} {_ci(r.auc_lo, r.auc_hi)}, gross edge {_fmt(r.edge_bps, 2)} bps "
        f"{_ci(r.edge_lo, r.edge_hi, 2)} vs {cfg.round_trip_cost_bps:.0f} bps round-trip fee "
        f"-> **{r.verdict}**."
    )


def update_readme(readme: Path, cfg: Config, quality, ho: pd.DataFrame, manifest: dict) -> None:
    text = readme.read_text()
    if README_START not in text or README_END not in text:
        raise ValueError("README is missing the RESULTS markers")
    block = "\n".join(
        [
            README_START,
            f"_Auto-generated {manifest['generated_utc']} from `results/RESULTS.md`; "
            "do not edit by hand._",
            "",
            f"**Data:** {cfg.symbol} {cfg.interval}, {quality.start[:10]} to {quality.end[:10]}, "
            f"{quality.n_rows:,} bars, {quality.missing_pct}% missing. "
            f"**Holdout:** {ho.test_start.iloc[0][:10]} to {ho.test_end.iloc[0][:10]}.",
            "",
            _primary_line(ho, cfg),
            "",
            f"**All configurations:** {_verdict_summary(ho)}",
            "",
            "Holdout, 1-bar delay (the realistic case). Intervals are Bonferroni-adjusted.",
            "",
            _holdout_table(ho, delays=(1,)),
            "",
            "Delay-0 results, walk-forward stability and feature diagnostics: "
            "[results/RESULTS.md](results/RESULTS.md).",
            README_END,
        ]
    )
    pre = text.split(README_START)[0]
    post = text.split(README_END)[1]
    readme.write_text(pre + block + post)


# --------------------------------------------------------------------------- entry point


def run(cfg: Config, readme: Path | None = None, log=print) -> dict:
    t0 = time.time()
    out = Path(cfg.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    paths = qdata.archive_paths(Path(cfg.data_dir), cfg.symbol, cfg.interval, cfg.start, cfg.end)
    raw, digests = qdata.load_klines(paths, verify=True)
    quality = qdata.validate_klines(raw)
    log(f"loaded {quality.n_rows:,} bars, {quality.missing_bars:,} missing")
    grid = qdata.regularize(raw)
    feats = build_features(grid, list(cfg.feature_names))

    level = ev.bonferroni_level(cfg.n_tests)
    wf_rows, ho_rows, calib = [], [], {}
    for h in cfg.horizons:
        for d in cfg.delays:
            ds = make_dataset(grid, feats, h, d)
            log(f"h={h} delay={d}: {len(ds.X):,} rows ({ds.n_flat:,} flat outcomes)")
            w, o, preds = evaluate_config(ds, cfg, level)
            wf_rows += w
            ho_rows += o
            if h == cfg.diagnostic_horizon and d == 0:
                calib = {k: v for k, v in preds.items() if not MODELS[k].is_baseline}

    wf, ho = pd.DataFrame(wf_rows), pd.DataFrame(ho_rows)
    fic = feature_ic_table(grid, feats, cfg, ev.bonferroni_level(len(cfg.feature_names)))

    manifest = {
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "git": _git_state(),
        "config": {
            **asdict(cfg),
            "round_trip_cost_bps": cfg.round_trip_cost_bps,
            "n_tests": cfg.n_tests,
            "ci_level": level,
        },
        "inputs_sha256": digests,
        "data_quality": quality.to_dict(),
        "versions": _versions(),
    }

    figs = _figures(wf, ho, fic, cfg, out, calib)
    wf.to_csv(out / "metrics_walkforward.csv", index=False)
    ho.to_csv(out / "metrics_holdout.csv", index=False)
    fic.to_csv(out / "feature_ic.csv", index=False)
    manifest["runtime_seconds"] = round(time.time() - t0, 1)
    (out / "run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    write_results_md(cfg, quality, digests, wf, ho, fic, figs, manifest, out)
    if readme is not None:
        update_readme(readme, cfg, quality, ho, manifest)
    log(f"done in {manifest['runtime_seconds']}s -> {out / 'RESULTS.md'}")
    return {"walkforward": wf, "holdout": ho, "feature_ic": fic, "manifest": manifest}


def _cli() -> None:
    p = argparse.ArgumentParser(description="Run the full signal study.")
    p.add_argument("--symbol", default=Config.symbol)
    p.add_argument("--start", default=Config.start)
    p.add_argument("--end", default=Config.end)
    p.add_argument("--data-dir", default=Config.data_dir)
    p.add_argument("--out-dir", default=Config.out_dir)
    p.add_argument("--fee-bps", type=float, default=Config.fee_bps_per_side)
    p.add_argument("--n-boot", type=int, default=Config.n_boot)
    p.add_argument("--update-readme", action="store_true")
    a = p.parse_args()
    cfg = Config(
        symbol=a.symbol,
        start=a.start,
        end=a.end,
        data_dir=a.data_dir,
        out_dir=a.out_dir,
        fee_bps_per_side=a.fee_bps,
        n_boot=a.n_boot,
    )
    run(cfg, readme=Path("README.md") if a.update_readme else None)


if __name__ == "__main__":
    _cli()
