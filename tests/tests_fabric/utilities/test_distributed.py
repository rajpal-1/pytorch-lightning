import functools
import os
from functools import partial
from pathlib import Path

import pytest
import torch
from lightning.fabric.accelerators import CPUAccelerator, CUDAAccelerator, MPSAccelerator
from lightning.fabric.plugins.environments import LightningEnvironment
from lightning.fabric.strategies import DDPStrategy, SingleDeviceStrategy
from lightning.fabric.strategies.launchers.multiprocessing import _MultiProcessingLauncher
from lightning.fabric.utilities.distributed import _gather_all_tensors, _sync_ddp, is_shared_filesystem
from tests_fabric.helpers.runif import RunIf


def wrap_launch_function(fn, strategy, *args, **kwargs):
    # the launcher does not manage this automatically. explanation available in:
    # https://github.com/Lightning-AI/lightning/pull/14926#discussion_r982976718
    strategy.setup_environment()
    return fn(*args, **kwargs)


def spawn_launch(fn, parallel_devices):
    """Copied from ``tests_pytorch.core.test_results.spawn_launch``"""
    # TODO: the accelerator and cluster_environment should be optional to just launch processes, but this requires lazy
    # initialization to be implemented
    device_to_accelerator = {"cuda": CUDAAccelerator, "mps": MPSAccelerator, "cpu": CPUAccelerator}
    accelerator_cls = device_to_accelerator[parallel_devices[0].type]
    strategy = DDPStrategy(
        accelerator=accelerator_cls(),
        parallel_devices=parallel_devices,
        cluster_environment=LightningEnvironment(),
        start_method="spawn",
    )
    launcher = _MultiProcessingLauncher(strategy=strategy)
    wrapped = partial(wrap_launch_function, fn, strategy)
    return launcher.launch(wrapped, strategy)


def _test_all_gather_uneven_tensors(strategy):
    rank = strategy.local_rank
    device = strategy.root_device
    world_size = strategy.num_processes

    tensor = torch.ones(rank, device=device)
    result = _gather_all_tensors(tensor)
    assert len(result) == world_size
    for idx in range(world_size):
        assert len(result[idx]) == idx
        assert (result[idx] == torch.ones_like(result[idx])).all()


def _test_all_gather_uneven_tensors_multidim(strategy):
    rank = strategy.local_rank
    device = strategy.root_device
    world_size = strategy.num_processes

    tensor = torch.ones(rank + 1, 2 - rank, device=device)
    result = _gather_all_tensors(tensor)
    assert len(result) == world_size
    for idx in range(world_size):
        val = result[idx]
        assert val.shape == (idx + 1, 2 - idx)
        assert (val == torch.ones_like(val)).all()


def _test_all_reduce(strategy):
    rank = strategy.local_rank
    device = strategy.root_device
    world_size = strategy.num_processes

    for dtype in (torch.long, torch.int, torch.float, torch.half):
        # max
        tensor = torch.tensor(rank + 1, device=device, dtype=dtype)
        expected = torch.tensor(2, device=device, dtype=dtype)
        result = _sync_ddp(tensor, reduce_op="max")
        assert torch.equal(result, expected)
        assert result is tensor  # inplace
        # sum
        tensor = torch.tensor(rank + 1, device=device, dtype=dtype)
        expected = torch.tensor(sum(range(1, world_size + 1)), device=device, dtype=dtype)
        result = _sync_ddp(tensor, reduce_op="sum")
        assert torch.equal(result, expected)
        assert result is tensor  # inplace
        # average
        tensor = torch.tensor(rank + 1, device=device, dtype=dtype)
        expected = torch.tensor(sum(range(1, world_size + 1)) / 2, device=device, dtype=dtype)
        result = _sync_ddp(tensor, reduce_op="avg")
        assert torch.equal(result, expected)
        assert result is tensor  # inplace


@RunIf(skip_windows=True)
@pytest.mark.parametrize(
    "process",
    [
        _test_all_gather_uneven_tensors_multidim,
        _test_all_gather_uneven_tensors,
        _test_all_reduce,
    ],
)
@pytest.mark.parametrize(
    "devices",
    [
        pytest.param([torch.device("cuda:0"), torch.device("cuda:1")], marks=RunIf(min_cuda_gpus=2)),
        [torch.device("cpu"), torch.device("cpu")],
    ],
)
def test_collective_operations(devices, process):
    spawn_launch(process, devices)


def test_is_shared_filesystem(tmp_path, monkeypatch):
    # In the non-distributed case, every location is interpreted as 'shared'
    assert is_shared_filesystem(SingleDeviceStrategy(torch.device("cpu")))

    test_fn = functools.partial(_test_is_shared_filesystem, tmp_path=tmp_path, monkeypatch=monkeypatch)
    spawn_launch(test_fn, [torch.device("cpu"), torch.device("cpu")])


def _test_is_shared_filesystem(strategy, tmp_path, monkeypatch):
    # Path doesn't exist
    with pytest.raises(FileNotFoundError, match="Unable to determine if the path belongs to a shared filesystem"):
        is_shared_filesystem(strategy, path="not/exist")

    # Path exists but not the same on all ranks
    file = tmp_path / f"file-rank-{strategy.global_rank}"
    file.touch()
    folder = tmp_path / f"folder-rank-{strategy.global_rank}"
    folder.mkdir()
    assert not is_shared_filesystem(strategy, path=file)
    assert not is_shared_filesystem(strategy, path=folder)

    # Path exists
    folder = tmp_path / "folder"
    file = folder / "file"
    if strategy.global_rank == 0:
        folder.mkdir()
        file.touch()
    strategy.barrier()
    assert folder.exists()
    assert is_shared_filesystem(strategy, path=folder)
    assert is_shared_filesystem(strategy, path=file)
    assert os.listdir(folder) == ["file"]  # rank test files got cleaned up

    # Path defaults to CWD
    monkeypatch.chdir(tmp_path)
    assert Path.cwd() == tmp_path
    assert is_shared_filesystem(strategy)
    monkeypatch.undo()

    # Path is a symlink
    linked = Path(tmp_path / "linked")
    if strategy.global_rank == 0:
        linked.symlink_to(tmp_path / "folder", target_is_directory=True)
    assert is_shared_filesystem(strategy, path=folder)

    # Remote path is considered shared
    assert is_shared_filesystem(strategy, path="s3://my-bucket/data")
