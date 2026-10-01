# quant-signal-research

[![CI](https://github.com/nisgemML/quant-signal-research/actions/workflows/ci.yml/badge.svg)](https://github.com/nisgemML/quant-signal-research/actions/workflows/ci.yml)

**Question:** using only what is known at the close of a 1-minute BTCUSDT bar, can the direction
of the next 1, 5 or 15 minutes be predicted out of sample, and does anything survive one bar of
latency and trading fees?

The study is built so that the answer is allowed to be "no". Every number below is written by
the pipeline, not typed by hand. Every feature is checked for lookahead in CI. The verdict for
each configuration is assigned by a fixed rule rather than by interpretation.

## Result

<!-- RESULTS:START -->
_Auto-generated 2026-10-01 16:16 UTC from `results/RESULTS.md`; do not edit by hand._

**Data:** BTCUSDT 1m, 2024-07-01 to 2025-06-30, 525,600 bars, 0.0% missing. **Holdout:** 2025-04-19 to 2025-06-30.

**Primary configuration** (`logit_all`, 5-minute horizon, delay 1): AUC 0.521 [0.512, 0.529], gross edge 0.15 bps [-0.03, 0.33] vs 40 bps round-trip fee -> **detectable, below cost hurdle**.

**All configurations:** Of 18 model/horizon/delay configurations: 0 detectable, above cost hurdle; 15 detectable, below cost hurdle; 3 no detectable signal.

Holdout, 1-bar delay (the realistic case). Intervals are Bonferroni-adjusted.

| model | h | delay | AUC [CI] | acc / majority | LL skill | rank IC [CI] | edge bps [CI] | verdict |
|---|---|---|---|---|---|---|---|---|
| base_rate | 1 | 1 | 0.500 [0.500, 0.500] | 0.499 / 0.501 | 0.0000 | n/a [n/a, n/a] | -0.02 [-0.06, 0.02] | baseline |
| logit_ret1 | 1 | 1 | 0.506 [0.502, 0.511] | 0.504 / 0.501 | 0.0000 | 0.013 [0.006, 0.022] | 0.03 [-0.01, 0.07] | detectable, below cost hurdle |
| logit_all | 1 | 1 | 0.511 [0.505, 0.516] | 0.508 / 0.501 | 0.0002 | 0.022 [0.012, 0.028] | 0.04 [-0.01, 0.08] | detectable, below cost hurdle |
| hgb_all | 1 | 1 | 0.506 [0.501, 0.510] | 0.504 / 0.501 | -0.0002 | 0.011 [0.002, 0.019] | 0.01 [-0.03, 0.05] | detectable, below cost hurdle |
| base_rate | 5 | 1 | 0.500 [0.500, 0.500] | 0.499 / 0.501 | 0.0000 | n/a [n/a, n/a] | -0.11 [-0.32, 0.11] | baseline |
| logit_ret1 | 5 | 1 | 0.502 [0.498, 0.507] | 0.502 / 0.501 | 0.0000 | 0.011 [0.002, 0.019] | 0.06 [-0.03, 0.15] | no detectable signal |
| logit_all | 5 | 1 | 0.521 [0.512, 0.529] | 0.513 / 0.501 | 0.0009 | 0.040 [0.025, 0.056] | 0.15 [-0.03, 0.33] | detectable, below cost hurdle |
| hgb_all | 5 | 1 | 0.518 [0.510, 0.526] | 0.512 / 0.501 | 0.0003 | 0.037 [0.020, 0.052] | 0.16 [-0.03, 0.34] | detectable, below cost hurdle |
| base_rate | 15 | 1 | 0.500 [0.500, 0.500] | 0.503 / 0.503 | 0.0000 | n/a [n/a, n/a] | 0.33 [-0.25, 0.80] | baseline |
| logit_ret1 | 15 | 1 | 0.507 [0.503, 0.511] | 0.505 / 0.503 | 0.0001 | 0.013 [0.004, 0.021] | 0.28 [-0.05, 0.59] | detectable, below cost hurdle |
| logit_all | 15 | 1 | 0.530 [0.518, 0.543] | 0.521 / 0.503 | 0.0019 | 0.052 [0.029, 0.078] | 0.46 [0.02, 0.94] | detectable, below cost hurdle |
| hgb_all | 15 | 1 | 0.523 [0.511, 0.534] | 0.516 / 0.503 | 0.0003 | 0.041 [0.017, 0.066] | 0.39 [-0.08, 0.85] | detectable, below cost hurdle |

Delay-0 results, walk-forward stability and feature diagnostics: [results/RESULTS.md](results/RESULTS.md).
<!-- RESULTS:END -->

The primary configuration (`logit_all`, 5-minute horizon, 1-bar delay) was fixed in
[docs/METHODOLOGY.md](docs/METHODOLOGY.md) before the holdout was evaluated.

## Why it is built this way

In an earlier project I measured an information coefficient with a t-stat of 3.76. It fell to
1.08 once a lookahead bug was fixed. This repo is designed so that class of mistake cannot pass
silently:

| risk | control | enforced by |
|---|---|---|
| feature uses future data | truncation test on every registered feature, plus 4 known-leaky features it must catch | `tests/test_features.py` |
| train/test leakage through overlapping labels | time-based purge of `horizon + delay` minutes before each test period | `tests/test_validation.py` |
| gaps silently turned into returns | regular time grid, missing bars never filled, shifts in time not rows | `tests/test_features.py` |
| corrupted or changed inputs | SHA-256 verified on download and on every load, digests in manifest | `tests/test_data.py` |
| ms/us timestamp switch (2025-01) | per-value unit detection, implausible dates rejected | `tests/test_data.py` |
| overconfident intervals | moving-block bootstrap; Bonferroni across 18 configurations | `src/qsr/evaluate.py` |
| results overstated in the write-up | mechanical verdict rule; README block generated from results | `src/qsr/report.py` |
| a broken test harness passing noise | size check (noise is not detected) and power check (planted signal is) | `tests/test_evaluate.py`, `tests/test_report.py` |
| tuning on the holdout | fixed hyperparameters; holdout evaluated once | `src/qsr/models.py` |

## What is measured

- **Models:** base rate, logistic regression on the last return, logistic regression on all
  15 features, shallow gradient boosting.
- **Horizons and delays:** horizons of 1, 5 and 15 minutes; delays of 0 bars (act on the close
  just observed) and 1 bar (one bar of latency).
- **Metrics:** AUC with bootstrap intervals, accuracy against the test-period majority rate,
  log-loss skill against the base rate, rank IC, and gross edge per prediction.
- **Cost hurdle:** gross edge is compared with a 20 bps round-trip fee. This is a hurdle check,
  **not a backtest**. There are no fills, queue position, impact or netting; see
  [METHODOLOGY §10](docs/METHODOLOGY.md#10-what-this-study-is-not).

## Reproduce

```bash
make install                         # Python >= 3.11
make data START=2024-07 END=2025-06  # ~12 monthly archives from data.binance.vision, checksum-verified
make test                            # offline; synthetic data only
make report                          # writes results/ and updates the Result section above
make notebook                        # executes notebooks/01_signal_study.ipynb in place
```

`results/run_manifest.json` records the git commit, the SHA-256 of every input archive, the
package versions and the full configuration. The full report takes roughly 15 to 25 minutes on a
laptop for 12 months of data; most of that time goes to bootstrap intervals and gradient
boosting.

## Layout

```
src/qsr/
  data.py         download, checksum verification, loading, quality report, regular grid
  features.py     feature registry, labels, dataset assembly, lookahead checker
  validation.py   walk-forward and holdout splits with time-based purging
  models.py       fixed model specifications
  evaluate.py     metrics, block bootstrap, verdict rule
  report.py       end-to-end run; writes results/, figures and the README result block
  synthetic.py    Binance-format synthetic archives for tests and CI only
tests/            data, lookahead, splits, statistics (size and power), end-to-end
notebooks/        narrative walkthrough built on the same package
docs/             METHODOLOGY.md: every analytical decision and its reason
results/          generated outputs (committed)
```

## Limitations

This is one asset on one venue over one period, using 1-minute bars rather than trades or order
book data. A positive verdict would justify building a realistic execution simulation; it would
not show profitability. See [docs/METHODOLOGY.md](docs/METHODOLOGY.md).

## License

MIT
