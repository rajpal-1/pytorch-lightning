# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from pytorch_lightning.metrics.functional.average_precision import average_precision
from pytorch_lightning.metrics.functional.classification import (
    auc,
    auroc,
    dice_score,
    multiclass_auroc,
    precision,
    precision_recall,
    recall,
    stat_scores,
    stat_scores_multiple_classes,
    iou,
)
from pytorch_lightning.metrics.functional.confusion_matrix import confusion_matrix
# TODO: unify metrics between class and functional, add below
from pytorch_lightning.metrics.functional.explained_variance import explained_variance
from pytorch_lightning.metrics.functional.f_beta import fbeta, f1
from pytorch_lightning.metrics.functional.mean_absolute_error import mean_absolute_error
from pytorch_lightning.metrics.functional.mean_squared_error import mean_squared_error
from pytorch_lightning.metrics.functional.mean_squared_log_error import mean_squared_log_error
from pytorch_lightning.metrics.functional.nlp import bleu_score
from pytorch_lightning.metrics.functional.precision_recall_curve import precision_recall_curve
from pytorch_lightning.metrics.functional.psnr import psnr
from pytorch_lightning.metrics.functional.roc import roc
from pytorch_lightning.metrics.functional.self_supervised import embedding_similarity
from pytorch_lightning.metrics.functional.ssim import ssim

from pytorch_lightning.metrics.functional.accuracy import accuracy
from pytorch_lightning.metrics.functional.hamming_loss import hamming_loss
from pytorch_lightning.metrics.functional.confusion_matrix import confusion_matrix
from pytorch_lightning.metrics.functional.f_beta import fbeta, f1
