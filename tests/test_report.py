"""End to end on synthetic archives: the pipeline writes every artifact, with provenance."""

from __future__ import annotations

import json

import pandas as pd

from qsr.report import README_END, README_START, Config, run
from qsr.synthetic import synthetic_klines, write_monthly_archives


def test_pipeline_end_to_end(tmp_path):
    # Two months spanning the ms -> us timestamp change, with a planted signal.
    df = synthetic_klines(start="2024-12-01", periods=62 * 1440, seed=5, signal=0.3)
    df = df.drop(df.index[40_000:40_090])  # a 90-minute outage
    raw = tmp_path / "raw"
    write_monthly_archives(df, raw, us_from="2025-01")

    readme = tmp_path / "README.md"
    readme.write_text(f"# t\n\n{README_START}\nSTALE-PLACEHOLDER\n{README_END}\n\nfooter\n")

    cfg = Config(
        start="2024-12",
        end="2025-01",
        data_dir=str(raw),
        out_dir=str(tmp_path / "out"),
        horizons=(1, 5),
        delays=(0, 1),
        models=("base_rate", "logit_all"),
        n_folds=3,
        n_boot=40,
    )
    res = run(cfg, readme=readme, log=lambda *_: None)
    out = tmp_path / "out"

    for f in [
        "RESULTS.md",
        "metrics_holdout.csv",
        "metrics_walkforward.csv",
        "feature_ic.csv",
        "run_manifest.json",
        "figures/fold_auc.png",
        "figures/holdout_calibration.png",
        "figures/feature_ic.png",
    ]:
        assert (out / f).exists(), f

    manifest = json.loads((out / "run_manifest.json").read_text())
    assert set(manifest["inputs_sha256"]) == {"BTCUSDT-1m-2024-12.zip", "BTCUSDT-1m-2025-01.zip"}
    assert manifest["data_quality"]["missing_bars"] == 90
    assert manifest["config"]["n_tests"] == 4

    ho = pd.read_csv(out / "metrics_holdout.csv")
    assert len(ho) == 2 * 2 * 2
    planted = ho[(ho.model == "logit_all") & (ho.horizon == 1) & (ho.delay == 0)].iloc[0]
    assert planted.auc_lo > 0.5  # the planted 1-bar signal is found...
    delayed = ho[(ho.model == "logit_all") & (ho.horizon == 1) & (ho.delay == 1)].iloc[0]
    assert delayed.auc < planted.auc  # ...and decays when you cannot act on it immediately

    text = readme.read_text()
    assert "STALE-PLACEHOLDER" not in text and "footer" in text and "Primary configuration" in text
    assert res["holdout"].shape[0] == 8
    assert res["feature_ic"].iloc[0]["feature"] == "taker_imb_1"  # planted feature ranks first
