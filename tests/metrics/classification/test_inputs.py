import pytest
import torch

from pytorch_lightning.metrics.utils import to_onehot, select_topk
from pytorch_lightning.metrics.classification.utils import _input_format_classification
from tests.metrics.classification.inputs import (
    Input,
    _binary_inputs as _bin,
    _binary_prob_inputs as _bin_prob,
    _multiclass_inputs as _mc,
    _multiclass_prob_inputs as _mc_prob,
    _multidim_multiclass_inputs as _mdmc,
    _multidim_multiclass_prob_inputs as _mdmc_prob,
    _multidim_multiclass_prob_inputs1 as _mdmc_prob1,
    _multilabel_inputs as _ml,
    _multilabel_prob_inputs as _ml_prob,
    _multilabel_multidim_inputs as _mlmd,
    _multilabel_multidim_prob_inputs as _mlmd_prob,
)
from tests.metrics.utils import NUM_BATCHES, BATCH_SIZE, NUM_CLASSES, EXTRA_DIM, THRESHOLD

# Some additional inputs to test on
_mc_prob_2cls = Input(torch.rand(NUM_BATCHES, BATCH_SIZE, 2), torch.randint(high=2, size=(NUM_BATCHES, BATCH_SIZE)))
_mdmc_prob_2cls = Input(
    torch.rand(NUM_BATCHES, BATCH_SIZE, 2, EXTRA_DIM), torch.randint(high=2, size=(NUM_BATCHES, BATCH_SIZE, EXTRA_DIM))
)
_mdmc_prob_2cls1 = Input(
    torch.rand(NUM_BATCHES, BATCH_SIZE, EXTRA_DIM, 2), torch.randint(high=2, size=(NUM_BATCHES, BATCH_SIZE, EXTRA_DIM))
)

# Some utils
T = torch.Tensor
I = lambda x: x
usq = lambda x: x.unsqueeze(-1)
toint = lambda x: x.to(torch.int)
thrs = lambda x: x >= THRESHOLD
rshp1 = lambda x: x.reshape(x.shape[0], -1)
onehot = lambda x: to_onehot(x, NUM_CLASSES)
onehot2 = lambda x: to_onehot(x, 2)
top1 = lambda x: select_topk(x, 1)
top2 = lambda x: select_topk(x, 2)
mvdim = lambda x: torch.movedim(x, 1, -1)

# To avoid ugly black line wrapping
ml_preds_tr = lambda x: rshp1(toint(thrs(x)))
mdmc1_top1_tr = lambda x: top1(mvdim(x))
mdmc1_top2_tr = lambda x: top2(mvdim(x))
probs_to_mc_preds_tr = lambda x: toint(onehot2(thrs(x)))
mdml_to_mc_tr = lambda x: onehot2(rshp1(x))
mlmd_prob_to_mc_preds_tr = lambda x: onehot2(rshp1(toint(thrs(x))))
mdmc_prob_to_ml_preds_tr = lambda x: top1(mvdim(x))[:, 1]

########################
# Test correct inputs
########################


@pytest.mark.parametrize(
    "inputs, threshold, logits, num_classes, is_multiclass, top_k, exp_mode, post_preds, post_target",
    [
        #############################
        # Test usual expected cases
        (_bin, THRESHOLD, False, None, False, 1, "multi-class", usq, usq),
        (_bin_prob, THRESHOLD, False, None, None, 1, "binary", lambda x: usq(toint(thrs(x))), usq),
        (_ml_prob, THRESHOLD, False, None, None, 1, "multi-label", lambda x: toint(thrs(x)), I),
        (_ml, THRESHOLD, False, None, False, 1, "multi-dim multi-class", I, I),
        (_ml_prob, THRESHOLD, False, None, None, 1, "multi-label", ml_preds_tr, rshp1),
        (_mlmd, THRESHOLD, False, None, False, 1, "multi-dim multi-class", rshp1, rshp1),
        (_mc, THRESHOLD, False, None, None, 1, "multi-class", onehot, onehot),
        (_mc_prob, THRESHOLD, False, None, None, 1, "multi-class", top1, onehot),
        (_mc_prob, THRESHOLD, False, None, None, 2, "multi-class", top2, onehot),
        (_mdmc, THRESHOLD, False, None, None, 1, "multi-dim multi-class", onehot, onehot),
        (_mdmc_prob, THRESHOLD, False, None, None, 1, "multi-dim multi-class", top1, onehot),
        (_mdmc_prob, THRESHOLD, False, None, None, 2, "multi-dim multi-class", top2, onehot),
        # Test with C dim in last place
        (_mdmc_prob1, THRESHOLD, False, None, None, 1, "multi-dim multi-class", mdmc1_top1_tr, onehot),
        (_mdmc_prob1, THRESHOLD, False, None, None, 2, "multi-dim multi-class", mdmc1_top2_tr, onehot),
        ###########################
        # Test some special cases
        # Binary as multiclass
        (_bin, THRESHOLD, False, None, None, 1, "multi-class", onehot2, onehot2),
        # Binary probs as multiclass
        (_bin_prob, THRESHOLD, False, None, True, 1, "binary", probs_to_mc_preds_tr, onehot2),
        # Multilabel as multiclass
        (_ml, THRESHOLD, False, None, True, 1, "multi-dim multi-class", onehot2, onehot2),
        # Multilabel probs as multiclass
        (_ml_prob, THRESHOLD, False, None, True, 1, "multi-label", probs_to_mc_preds_tr, onehot2),
        # Multidim multilabel as multiclass
        (_mlmd, THRESHOLD, False, None, True, 1, "multi-dim multi-class", mdml_to_mc_tr, mdml_to_mc_tr),
        # Multidim multilabel probs as multiclass
        (_mlmd_prob, THRESHOLD, False, None, True, 1, "multi-label", mlmd_prob_to_mc_preds_tr, mdml_to_mc_tr),
        # Multiclass prob with 2 classes as binary
        (_mc_prob_2cls, THRESHOLD, False, None, False, 1, "multi-class", lambda x: top1(x)[:, [1]], usq),
        # Multi-dim multi-class with 2 classes as multi-label
        (_mdmc_prob_2cls, THRESHOLD, False, None, False, 1, "multi-dim multi-class", lambda x: top1(x)[:, 1], I),
        (_mdmc_prob_2cls1, THRESHOLD, False, None, False, 1, "multi-dim multi-class", mdmc_prob_to_ml_preds_tr, I),
    ],
)
def test_usual_cases(inputs, threshold, logits, num_classes, is_multiclass, top_k, exp_mode, post_preds, post_target):
    preds_out, target_out, mode = _input_format_classification(
        preds=inputs.preds[0],
        target=inputs.target[0],
        threshold=threshold,
        logits=logits,
        num_classes=num_classes,
        is_multiclass=is_multiclass,
        top_k=top_k,
    )

    assert mode == exp_mode
    assert torch.equal(preds_out, post_preds(inputs.preds[0]))
    assert torch.equal(target_out, post_target(inputs.target[0]))

    # Test that things work when batch_size = 1
    preds_out, target_out, mode = _input_format_classification(
        preds=inputs.preds[0][[0], ...],
        target=inputs.target[0][[0], ...],
        threshold=threshold,
        logits=logits,
        num_classes=num_classes,
        is_multiclass=is_multiclass,
        top_k=top_k,
    )

    assert mode == exp_mode
    assert torch.equal(preds_out, post_preds(inputs.preds[0][[0], ...]))
    assert torch.equal(target_out, post_target(inputs.target[0][[0], ...]))


########################################################################
# Test incorrect inputs
########################################################################


@pytest.mark.parametrize(
    "preds, target, threshold, logits, num_classes, is_multiclass",
    [
        # Target not integer
        (torch.randint(high=2, size=(7,)), torch.randint(high=2, size=(7,)).to(torch.float), 0.5, False, None, None),
        # Target negative
        (torch.randint(high=2, size=(7,)), -torch.randint(high=2, size=(7,)), 0.5, False, None, None),
        # Preds negative integers
        (-torch.randint(high=2, size=(7,)), torch.randint(high=2, size=(7,)), 0.5, False, None, None),
        # Negative probabilities
        (-torch.rand(size=(7,)), torch.randint(high=2, size=(7,)), 0.5, False, None, None),
        # Threshold outside of [0,1]
        (torch.rand(size=(7,)), torch.randint(high=2, size=(7,)), 1.5, False, None, None),
        # is_multiclass=False and target > 1
        (torch.rand(size=(7,)), torch.randint(low=2, high=4, size=(7,)), 0.5, False, None, False),
        # is_multiclass=False and preds integers with > 1
        (torch.randint(low=2, high=4, size=(7,)), torch.randint(high=2, size=(7,)), 0.5, False, None, False),
        # Wrong batch size
        (torch.randint(high=2, size=(8,)), torch.randint(high=2, size=(7,)), 0.5, False, None, None),
        # Completely wrong shape
        (torch.randint(high=2, size=(7,)), torch.randint(high=2, size=(7, 4)), 0.5, False, None, None),
        # Same #dims, different shape
        (torch.randint(high=2, size=(7, 3)), torch.randint(high=2, size=(7, 4)), 0.5, False, None, None),
        # Same shape and preds floats, target not binary
        (torch.rand(size=(7, 3)), torch.randint(low=2, high=4, size=(7, 3)), 0.5, False, None, None),
    ],
)
def test_incorrect_inputs(preds, target, threshold, logits, num_classes, is_multiclass):
    with pytest.raises(ValueError):
        _input_format_classification(
            preds=preds,
            target=target,
            threshold=threshold,
            logits=logits,
            num_classes=num_classes,
            is_multiclass=is_multiclass,
        )
