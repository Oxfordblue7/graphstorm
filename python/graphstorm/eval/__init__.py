"""
    Copyright 2023 Contributors

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

    Package initialization to load evaluation funcitons and classes
"""
from .eval_func import labels_to_one_hot, compute_acc, compute_acc_lp, compute_rmse, compute_mse
from .eval_func import compute_roc_auc, compute_precision_recall_auc
from .eval_func import ClassificationMetrics, RegressionMetrics, LinkPredictionMetrics

from .eval_func import SUPPORTED_CLASSIFICATION_METRICS
from .eval_func import SUPPORTED_REGRESSION_METRICS
from .eval_func import SUPPORTED_LINK_PREDICTION_METRICS
from .eval_func import SUPPORTED_HIT_AT_METRICS
from .eval_func import SUPPORTED_FSCORE_AT_METRICS

from .evaluator import (GSgnnBaseEvaluator,
                        GSgnnPredictionEvalInterface,
                        GSgnnLPRankingEvalInterface,
                        GSgnnLPEvaluator,
                        GSgnnPerEtypeLPEvaluator,
                        GSgnnMrrLPEvaluator,
                        GSgnnPerEtypeMrrLPEvaluator,
                        GSgnnHitsLPEvaluator,
                        GSgnnPerEtypeHitsLPEvaluator,
                        GSgnnClassificationEvaluator,
                        GSgnnRegressionEvaluator,
                        GSgnnRconstructFeatRegScoreEvaluator,
                        GSgnnMultiTaskEvaluator)

from .utils import is_float
