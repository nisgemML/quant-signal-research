"""Builds notebooks/01_signal_study.ipynb from source cells (keeps the notebook diffable)."""

from pathlib import Path

import nbformat as nbf

md, code = nbf.v4.new_markdown_cell, nbf.v4.new_code_cell

cells = [
    md("""# Short-horizon direction signal in BTCUSDT 1-minute bars

**Question.** Using only information available at the close of a 1-minute bar, can the sign of the
next 1, 5 or 15 minutes of return be predicted out of sample - and does anything survive one bar of
latency and trading fees?

**How to read this notebook.** It is the narrative layer. All logic lives in the tested `qsr`
package, and the full model grid is produced by `make report` (see `results/RESULTS.md`). The
primary configuration was fixed before any holdout evaluation (see `docs/METHODOLOGY.md`):
**`logit_all`, horizon 5 minutes, delay 1 bar**."""),
    code("""import os
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from qsr import data as qd
from qsr import evaluate as ev
from qsr.features import FEATURES, build_features, check_no_lookahead, make_dataset
from qsr.report import Config, _fit_predict, feature_ic_table
from qsr.validation import holdout, purge_width

ROOT = Path(os.environ.get("QSR_ROOT", "..")).resolve()
cfg = Config(
    start=os.environ.get("QSR_START", Config.start),
    end=os.environ.get("QSR_END", Config.end),
    data_dir=os.environ.get("QSR_DATA_DIR", str(ROOT / "data" / "raw")),
)
N_BOOT = int(os.environ.get("QSR_N_BOOT", "300"))
pd.set_option("display.precision", 4)
print(f"{cfg.symbol} {cfg.interval}  {cfg.start} .. {cfg.end}  from {cfg.data_dir}")"""),
    md("""## 1. Data

Monthly archives from data.binance.vision, each verified against its published SHA-256 on load.
Missing bars are **not** filled: features and labels are computed on a regular 1-minute grid, so
any window touching a gap becomes NaN and is dropped."""),
    code("""data_dir = Path(cfg.data_dir)
paths = qd.archive_paths(data_dir, cfg.symbol, cfg.interval, cfg.start, cfg.end)
raw, digests = qd.load_klines(paths)
quality = qd.validate_klines(raw)
grid = qd.regularize(raw)
summary = {k: v for k, v in quality.to_dict().items() if k != "largest_gaps"}
pd.Series(summary, name="value").to_frame()"""),
    code("""fig, axes = plt.subplots(2, 1, figsize=(10, 5), sharex=True)
grid["close"].resample("1h").last().plot(ax=axes[0], lw=0.8)
axes[0].set_ylabel("close (USDT)")
grid["close"].isna().resample("1D").sum().plot(ax=axes[1], kind="line", color="firebrick")
axes[1].set_ylabel("missing bars / day")
fig.tight_layout()"""),
    md("""## 2. Every feature is backward-looking - checked, not asserted

For several cut points `c`, each feature is recomputed on data truncated at `c`. If any value at or
before `c` changes, the feature used the future. The same check is run in CI over the whole feature
registry, together with deliberately leaky features that it must catch."""),
    code("""sample = grid.iloc[: min(len(grid), 20_000)]
cuts = list(np.linspace(3_000, len(sample) - 2, 6).astype(int))
leaky = {
    "LEAKY negative shift": lambda df: np.log(df["close"]).shift(-1),
    "LEAKY centered rolling mean": lambda df: df["close"].rolling(21, center=True).mean(),
    "LEAKY full-sample z-score": lambda df: (df["close"] - df["close"].mean()) / df["close"].std(),
}
rows = [(n, len(check_no_lookahead(f, sample, cuts))) for n, f in {**FEATURES, **leaky}.items()]
pd.DataFrame(rows, columns=["feature", "cut points with lookahead"]).set_index("feature")"""),
    md("""## 3. Univariate diagnostics (development period only)

Rank IC of each feature against the 5-minute forward return, with Bonferroni-adjusted
moving-block bootstrap intervals. This is descriptive; no feature or model choice was made from it,
and the holdout period is excluded."""),
    code("""feats = build_features(grid)
cfg_diag = Config(**{**cfg.__dict__, "n_boot": N_BOOT})
fic = feature_ic_table(grid, feats, cfg_diag, ev.bonferroni_level(len(FEATURES)))
fic.set_index("feature")"""),
    md("""## 4. Primary configuration on the holdout

`logit_all`, horizon 5, evaluated with delay 0 (act on the close you just saw) and delay 1
(one bar of latency). The holdout is the final 20% of the sample; the model is trained on
everything before it, minus a purge gap equal to the label span."""),
    code("""n_tests = Config().n_tests
level = ev.bonferroni_level(n_tests)
out, calib = [], {}
for delay in (0, 1):
    ds = make_dataset(grid, feats, horizon=5, delay=delay)
    fold = holdout(pd.DatetimeIndex(ds.X.index), cfg.holdout_frac, cfg.min_train_frac,
                   purge_width(5, delay))
    p, base = _fit_predict("logit_all", ds.X, ds.y, fold.train, fold.test)
    y, fwd = ds.y.to_numpy()[fold.test], ds.fwd.to_numpy()[fold.test]
    m = ev.point_metrics(y, p, fwd, base)
    ci = ev.bootstrap_intervals(y, p, fwd, ev.default_block_len(5, delay), N_BOOT, level)
    m.update(auc_ci=(round(ci["auc"].lo, 4), round(ci["auc"].hi, 4)),
             edge_ci_bps=(round(ci["edge_bps"].lo, 2), round(ci["edge_bps"].hi, 2)),
             verdict=ev.verdict(ci["auc"], ci["edge_bps"], cfg.round_trip_cost_bps, False))
    out.append(pd.Series(m, name=f"delay {delay}"))
    calib[delay] = (p, y)
print(f"holdout {fold.test_start} .. {fold.test_end};  CI level {level:.4f} ({n_tests} tests)")
pd.concat(out, axis=1)"""),
    code("""fig, ax = plt.subplots(figsize=(4.8, 4.4))
for delay, (p, y) in calib.items():
    m = ~np.isnan(y)
    q = pd.qcut(p[m], 10, duplicates="drop")
    g = pd.DataFrame({"p": p[m], "y": y[m], "q": q}).groupby("q", observed=True).mean()
    ax.plot(g["p"], g["y"], marker="o", label=f"delay {delay}")
lo, hi = ax.get_xlim()
ax.plot([lo, hi], [lo, hi], color="grey", ls="--", lw=1)
ax.set_xlabel("predicted P(up), decile mean")
ax.set_ylabel("realized up-frequency")
ax.legend()
ax.set_title("Holdout calibration, logit_all h=5")
fig.tight_layout()"""),
    md("""## 5. Full grid

All models x horizons {1, 5, 15} x delays {0, 1}, produced by `make report`. Verdicts are assigned
mechanically: *detectable* requires the Bonferroni-adjusted AUC interval to exclude 0.5; *above
cost hurdle* additionally requires the lower bound of gross edge per prediction to exceed the
round-trip fee."""),
    code("""res = ROOT / "results" / "metrics_holdout.csv"
if res.exists():
    ho = pd.read_csv(res)
    display(ho.verdict.value_counts().to_frame("configurations"))
    cols = ["model", "horizon", "delay", "auc", "auc_lo", "auc_hi", "edge_bps", "edge_lo",
            "edge_hi", "verdict"]
    display(ho[cols])
else:
    print("results/metrics_holdout.csv not found - run `make report` first.")"""),
    md("""## 6. Conclusion

_Write this after running the study, in plain language: state the verdict for the primary
configuration, what the delay-1 comparison shows, how the result compares with the fee hurdle, and
what evidence would change your mind. Do not describe any result more strongly than the verdict
column does._"""),
]

nb = nbf.v4.new_notebook(
    cells=cells,
    metadata={
        "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
        "language_info": {"name": "python"},
    },
)
out = Path(__file__).resolve().parents[1] / "notebooks" / "01_signal_study.ipynb"
nbf.write(nb, out)
print(out)
