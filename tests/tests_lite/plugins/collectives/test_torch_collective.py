import contextlib
import datetime
from unittest import mock

import pytest
import torch
from tests_lite.helpers.runif import RunIf

if torch.distributed.is_available():

    from torch.distributed import ReduceOp
else:
    ReduceOp = mock.Mock()

from lightning_lite.plugins.collectives import TorchCollective

PASSED_TENSOR = mock.Mock()
PASSED_OBJECT = mock.Mock()


@pytest.mark.parametrize(
    ["fn_name", "kwargs", "return_key"],
    [
        pytest.param("send", {"tensor": PASSED_TENSOR, "dst": 0, "tag": 0}, None),
        pytest.param("recv", {"tensor": PASSED_TENSOR, "src": 0, "tag": 0}, "tensor"),
        pytest.param("broadcast", {"tensor": PASSED_TENSOR, "src": 0}, "tensor"),
        pytest.param("all_reduce", {"tensor": PASSED_TENSOR, "op": ReduceOp.SUM}, "tensor"),
        pytest.param("reduce", {"tensor": PASSED_TENSOR, "dst": 0, "op": ReduceOp.SUM}, "tensor"),
        pytest.param(
            "all_gather",
            {"tensor_list": [PASSED_TENSOR], "tensor": PASSED_TENSOR},
            "tensor_list",
        ),
        pytest.param(
            "gather",
            {"tensor": PASSED_TENSOR, "gather_list": [PASSED_TENSOR], "dst": 0},
            "gather_list",
        ),
        pytest.param(
            "scatter",
            {"tensor": PASSED_TENSOR, "scatter_list": [PASSED_TENSOR], "src": 0},
            "tensor",
        ),
        pytest.param(
            "reduce_scatter",
            {"output": PASSED_TENSOR, "input_list": [PASSED_TENSOR], "op": ReduceOp.SUM},
            "output",
        ),
        pytest.param(
            "all_to_all",
            {"output_tensor_list": [PASSED_TENSOR], "input_tensor_list": [PASSED_TENSOR]},
            "output_tensor_list",
        ),
        pytest.param("barrier", {"device_ids": [0]}, None),
        pytest.param(
            "all_gather_object",
            {"object_list": [PASSED_OBJECT], "obj": PASSED_OBJECT},
            "object_list",
        ),
        pytest.param(
            "broadcast_object_list",
            {"object_list": [PASSED_OBJECT], "src": 0},
            "object_list",
            marks=RunIf(max_torch="1.10"),
        ),
        pytest.param(
            "broadcast_object_list",
            {"object_list": [PASSED_OBJECT], "src": 0, "device": torch.device("cpu")},
            "object_list",
            marks=RunIf(min_torch="1.10"),
        ),
        pytest.param(
            "gather_object",
            {"obj": PASSED_OBJECT, "object_gather_list": [PASSED_OBJECT], "dst": 0},
            "object_gather_list",
        ),
        pytest.param(
            "scatter_object_list",
            {"scatter_object_output_list": [PASSED_OBJECT], "scatter_object_input_list": [PASSED_OBJECT], "src": 0},
            "scatter_object_output_list",
        ),
        pytest.param(
            "monitored_barrier",
            {"timeout": datetime.timedelta(seconds=1), "wait_all_ranks": False},
            None,
        ),
    ],
)
@RunIf(distributed=True)
def test_collective_calls_with_created_group(fn_name, kwargs, return_key):
    with mock.patch("torch.distributed.is_available", return_value=True), mock.patch(
        "torch.distributed.init_process_group"
    ):
        collective = TorchCollective(instantiate_group=True)
    fn = getattr(collective, fn_name)
    with mock.patch(f"torch.distributed.{fn_name}", autospec=True) as mock_call:
        result = fn(**kwargs)
    mock_call.assert_called_once_with(**kwargs, group=collective.group)
    if return_key is not None:
        assert result == kwargs[return_key]


@RunIf(distributed=True)
def test_convert_ops():
    if torch.distributed.is_available():
        cm = contextlib.nullcontext()
    else:
        cm = mock.patch("torch.distributed.ReduceOp")
    with cm:
        assert TorchCollective._convert_to_native_op("avg") == ReduceOp.AVG
        assert TorchCollective._convert_to_native_op("Avg") == ReduceOp.AVG
        assert TorchCollective._convert_to_native_op("AVG") == ReduceOp.AVG
        assert TorchCollective._convert_to_native_op("band") == ReduceOp.BAND
        assert TorchCollective._convert_to_native_op("Band") == ReduceOp.BAND
        assert TorchCollective._convert_to_native_op("BAND") == ReduceOp.BAND
        assert TorchCollective._convert_to_native_op("bor") == ReduceOp.BOR
        assert TorchCollective._convert_to_native_op("Bor") == ReduceOp.BOR
        assert TorchCollective._convert_to_native_op("BOR") == ReduceOp.BOR
        assert TorchCollective._convert_to_native_op("bxor") == ReduceOp.BXOR
        assert TorchCollective._convert_to_native_op("Bxor") == ReduceOp.BXOR
        assert TorchCollective._convert_to_native_op("BXOR") == ReduceOp.BXOR
        assert TorchCollective._convert_to_native_op("max") == ReduceOp.MAX
        assert TorchCollective._convert_to_native_op("Max") == ReduceOp.MAX
        assert TorchCollective._convert_to_native_op("MAX") == ReduceOp.MAX
        assert TorchCollective._convert_to_native_op("min") == ReduceOp.MIN
        assert TorchCollective._convert_to_native_op("Min") == ReduceOp.MIN
        assert TorchCollective._convert_to_native_op("MIN") == ReduceOp.MIN
        assert TorchCollective._convert_to_native_op("product") == ReduceOp.PRODUCT
        assert TorchCollective._convert_to_native_op("Product") == ReduceOp.PRODUCT
        assert TorchCollective._convert_to_native_op("PRODUCT") == ReduceOp.PRODUCT
        assert TorchCollective._convert_to_native_op("sum") == ReduceOp.SUM
        assert TorchCollective._convert_to_native_op("Sum") == ReduceOp.SUM
        assert TorchCollective._convert_to_native_op("SUM") == ReduceOp.SUM

    with pytest.raises(ValueError, match="op 1 should be a `str` or `ReduceOp`"):
        TorchCollective._convert_to_native_op(1)

    if torch.distributed.is_available():
        with pytest.raises(ValueError, match="op 'INVALID' is not a member of `ReduceOp`"):
            TorchCollective._convert_to_native_op("invalid")
