# Methodology

This document fixes every analytical choice. Anything decided here was decided before the
holdout was evaluated, and the code enforces it rather than relying on discipline.

## 1. Question

Using only information available at the close of a 1-minute BTCUSDT bar, can the sign of the
forward return over the next 1, 5 or 15 minutes be predicted out of sample? If so, does the
prediction survive (a) one bar of execution latency and (b) exchange fees?

**Primary configuration (pre-registered):** model `logit_all`, horizon 5 minutes, delay 1 bar.
It is the configuration closest to something tradable. All other configurations are reported,
but only this one is the headline.

## 2. Data

- **Source:** Binance spot monthly kline archives, `data.binance.vision`, symbol BTCUSDT,
  interval 1m. Default period 2024-07 to 2025-06 (12 months), which deliberately spans the
  2025-01 change from millisecond to microsecond timestamps.
- **Integrity:** every archive is verified against its published SHA-256 on download and again
  on every load. Digests are written to `results/run_manifest.json`.
- **Sanity checks:** timestamps before Binance's launch raise (this catches epoch-unit errors
  that would otherwise build a decades-long empty grid). Exact duplicate bars are dropped;
  duplicates with conflicting values raise.
- **Missing bars:** reported, never filled. All features and labels are computed on a regular
  1-minute grid. Any rolling window or forward return touching a missing bar is NaN, and the row
  is dropped. Forward-filling prices would fabricate zero returns and bias both features and
  labels.
- **Reported quality metrics:** missing bars and largest gaps, zero-volume bars, OHLC
  inconsistencies, taker volume exceeding total volume.

## 3. Timing convention

- A feature at bar `t` may use data up to and including the **close of bar `t`**.
- The label at `t` is `log(close[t+d+h] / close[t+d])` with horizon `h` and delay `d`.
  - `d = 0` assumes you can transact at the close you just observed. This is optimistic.
  - `d = 1` assumes one bar of latency. The gap between the two shows how much of any apparent
    edge is really just the last observed tick.
- **Enforcement:** `check_no_lookahead` recomputes each feature on data truncated at several cut
  points and fails if any value at or before the cut changes. CI runs it on every registered
  feature. CI also runs it on four deliberately leaky features (negative shift, centered
  rolling window, full-sample z-score, backward fill), all of which it must catch.

## 4. Features

All windows are trailing and require a complete window (`min_periods = window`).

| name | definition |
|---|---|
| `ret_k`, k in {1, 5, 15, 60} | `log(close_t / close_{t-k})` |
| `rv_w`, w in {15, 60} | sqrt of the sum of squared 1-min log returns over the last w bars |
| `parkinson_60` | Parkinson range volatility over 60 bars |
| `taker_imb_w`, w in {1, 15, 60} | (taker buy - taker sell) / volume, summed over w bars; in [-1, 1] |
| `volume_z_1440` | z-score of log(1 + volume) against the trailing 24 hours |
| `count_z_1440` | z-score of log(1 + trade count) against the trailing 24 hours |
| `range_pos_60` | position of the close within the trailing 60-bar high-low range |
| `tod_sin`, `tod_cos` | minute-of-day seasonality (deterministic, known in advance) |

The feature set was fixed before evaluation and is intentionally standard. The study tests
whether *common* short-horizon information is predictive, not how far a search over features
can be pushed.

## 5. Labels

Direction is `1` if the forward log return is positive and `0` if negative. **Flat** outcomes
(exactly zero, common at 1 minute) carry no direction, so they are excluded from training and
from classification metrics. They are kept for return-based metrics (rank IC, gross edge),
where they correctly contribute zero.

## 6. Validation

```
|<------------------ development (80%) ------------------>|<-- holdout (20%) -->|
|  initial train (30%) | wf1 | wf2 | wf3 | wf4 | wf5       |   evaluated once    |
```

- **Walk-forward:** expanding training window with 5 contiguous test folds inside the
  development period. These folds are a stability diagnostic. With fixed hyperparameters,
  nothing is selected from them.
- **Holdout:** the final 20% of the sample. The model is trained on all development data and
  evaluated once.
- **Purging:** the label of a training row at `t` depends on prices up to `t + d + h`. Training
  rows with `t >= test_start - (d + h) minutes` are dropped, so no test-period price enters
  training. Purging is time-based, which keeps it correct across data gaps.
- **No embargo:** training always precedes testing in this design. López de Prado's embargo
  protects training rows that come *after* a test block, and there are none here.

## 7. Models

Hyperparameters are fixed in `src/qsr/models.py` using common defaults and are never tuned.

| model | purpose |
|---|---|
| `base_rate` | training up-frequency as a constant; its edge equals the drift of always long/short |
| `logit_ret1` | logistic regression on the last return only: the textbook reversal/momentum check |
| `logit_all` | standardized logistic regression on all features (scaler fit on train only) |
| `hgb_all` | histogram gradient boosting, shallow and regularized, as a nonlinearity check |

## 8. Metrics and inference

- **AUC** (non-flat outcomes) is the primary detection statistic. It is threshold-free and
  insensitive to the up/down base rate.
- **Accuracy** is always shown next to the test-period majority rate. On its own it is
  misleading whenever the base rate drifts from 50%.
- **Log-loss skill** is `1 - LL(model) / LL(train base rate)`. It is positive only if the
  probabilities beat the naive prior.
- **Rank IC** is the Spearman correlation between predicted probability and forward return.
- **Gross edge (bps)** is `mean(sign(p - 0.5) * forward log return)`. It is the average gross
  return per prediction if each were a separate round trip at the label's entry and exit
  prices.
- **Intervals:** moving-block bootstrap with block length `max(60, 20 * (h + d))` bars. Minute
  returns show volatility clustering and overlapping labels are autocorrelated by construction,
  so i.i.d. resampling or naive t-stats would overstate confidence. The interval is the point
  estimate +/- z times the bootstrap standard deviation (see "Interval construction" below).
- **Multiple testing:** 3 models x 3 horizons x 2 delays = 18 configurations. Intervals are
  computed at the Bonferroni level `1 - 0.05 / 18`. Feature ICs form a separate family,
  adjusted for the number of features.

### Interval construction (and a correction)

The first published run used **percentile** intervals: the 0.14% and 99.86% quantiles of 300
bootstrap draws, the two-sided tails at the Bonferroni level 0.9972. That leaves about 0.4 draws
per tail, so each endpoint was essentially the minimum or maximum of the draws. Measured on a
planted weak signal (AR(1)-smoothed feature, 30,000 bars, block 120, AUC 0.507):

| method | AUC lower bound, 6 seeds | spread |
|---|---|---|
| percentile, 300 draws (old) | 0.4982 - 0.5005 | 0.0023 |
| percentile, 2,000 draws (reference) | 0.4975 - 0.4987 | 0.0013 |
| normal, 300 draws (current) | 0.4977 - 0.4982 | 0.0005 |

Two problems with the old method. The verdict for a borderline configuration depended on the
random seed, because the lower bound straddled 0.5. And the 300-draw intervals were **too
narrow**: their lower bounds sat above the 2,000-draw reference. That makes the test
anti-conservative, the error this study is designed to avoid. The current method uses the point
estimate +/- z * SD(draws). An SD is estimated well from a few hundred draws, an extreme quantile
is not, and AUC, rank IC and mean edge are averages over ~10^5 bars, so their sampling
distributions are close to normal. It is about 5x more stable at the same cost, and it agrees
with the expensive reference. `tests/test_evaluate.py` pins the stability property. The
percentile method remains available (`method="percentile"`) for comparison.

**Effect on the published results.** Re-running the study with the corrected intervals moved
one configuration: `logit_ret1`, 15-minute horizon, delay 0, went from AUC 0.505 [0.501, 0.510],
"detectable", to 0.505 [0.500, 0.511], "no detectable signal". The tally went from 15 to 14
detectable configurations, and from 3 to 4 with no detectable signal. The point estimate did not
move. Only the interval widened, enough to touch 0.5. Nothing crossed the cost hurdle under
either method. The primary configuration is detectable under both: AUC 0.521 [0.512, 0.529]
before, [0.511, 0.530] after.

## 9. Verdict rule

The verdict column is computed mechanically, so no result can be described more strongly than
the data allow:

| verdict | condition |
|---|---|
| no detectable signal | adjusted AUC interval includes 0.5 (or lies below it) |
| detectable, below cost hurdle | AUC interval above 0.5, but edge lower bound <= round-trip fee |
| detectable, above cost hurdle | AUC interval above 0.5 and edge lower bound > round-trip fee |

An AUC interval entirely below 0.5 is classed as "no detectable signal". A relationship that
reverses between training and test is instability, not a usable signal.

**Fee assumption:** 10 bps per side (20 bps round trip), configurable with `--fee-bps`. This is
intended as Binance's standard spot taker rate. Check the current fee schedule before relying
on it.

## 10. What this study is not

- **Not a backtest.** There is no order book, queue position, partial fill, latency
  distribution, market impact, position netting or inventory. "Above cost hurdle" would be a
  reason to build a proper simulation, not evidence of profit.
- **One asset, one venue, one period.** Results may not transfer across regimes, venues or
  instruments.
- **Bar data only.** Taker imbalance is aggregated per minute. Trade-level or order-book data
  could carry information that bars cannot.

## 11. Size and power checks

The pipeline is validated in CI on synthetic data in Binance's exact archive format:

- **Size:** on a pure random walk, holdout AUC intervals contain 0.5.
- **Power:** with a planted signal (taker imbalance predicting the next return), the signal is
  detected, it ranks first in the univariate IC table, and it weakens when one bar of delay is
  imposed.

These checks establish that a null result on real data means "no signal found by a procedure
that can find one", not "a broken procedure".
