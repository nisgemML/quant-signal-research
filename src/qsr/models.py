"""Model specifications.

Hyperparameters are fixed here, chosen from common defaults before any evaluation, and
never tuned on walk-forward or holdout results. That keeps the holdout honest: it is
evaluated once, and nothing about it fed back into the models.

Models, from simplest to most flexible:

* ``base_rate``   - predicts the training up-frequency. AUC is 0.5 by construction; its
                    gross edge is simply the drift (always long or always short).
* ``logit_ret1``  - logistic regression on the last 1-minute return only
                    (the classic short-horizon reversal/momentum question).
* ``logit_all``   - logistic regression on all features, standardized on train only.
* ``hgb_all``     - gradient-boosted trees on all features (nonlinear check).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sklearn.base import ClassifierMixin
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


@dataclass(frozen=True)
class ModelSpec:
    name: str
    features: tuple[str, ...] | None  # None = all features
    factory: Callable[[], ClassifierMixin]
    is_baseline: bool = False


def _logit() -> ClassifierMixin:
    return make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=2000))


def _hgb() -> ClassifierMixin:
    return HistGradientBoostingClassifier(
        max_iter=200,
        learning_rate=0.05,
        max_leaf_nodes=15,
        min_samples_leaf=500,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=0,
    )


MODELS: dict[str, ModelSpec] = {
    "base_rate": ModelSpec(
        "base_rate", ("ret_1",), lambda: DummyClassifier(strategy="prior"), True
    ),
    "logit_ret1": ModelSpec("logit_ret1", ("ret_1",), _logit),
    "logit_all": ModelSpec("logit_all", None, _logit),
    "hgb_all": ModelSpec("hgb_all", None, _hgb),
}
